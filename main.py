import os
import faiss
import numpy as np
import aiohttp
import pickle
import logging
import asyncio
import re
import time  # Added import for time module
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
    AIORateLimiter
)
from typing import List, Dict

# Конфигурация
VENDOR_API_KEY = "sk-or-vv-a8d6e009e2bbe09474b0679fbba83b015ff1c4f255ed76f33b48ccb1632bdc32"
INDEX_PATH = "/data/faiss_index.bin"
METADATA_PATH = "/data/metadata.pkl"
MODEL_ID = "google/gemini-flash-1.5"
API_URL = "https://api.vsegpt.ru/v1/chat/completions"
EMBEDDING_URL = "https://api.vsegpt.ru/v1/embeddings"

# Настройки
MAX_CONTEXT_TOKENS = 7000
MAX_RESPONSE_LENGTH = 3800
REQUEST_DELAY = 12  # Задержка между запросами
MAX_RETRIES = 3
USER_RATE_LIMIT = 8  # Запросов в минуту на пользователя

SYSTEM_PROMPT = """Ты - ассистент, анализирующий документы. Отвечай точно и информативно,
используя только предоставленные фрагменты текста и делая оговорку: "согласно имеющейся информации". Делай ссылки на номера пунктов, если они указаны. Если информации недостаточно,
сообщи об этом. Запрещено указывать расширение файла (например, txt или doc)"""

class AnticorruptionBot:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.index, self.metadata = self._load_faiss_index()
        self.session = None  # Инициализируем в async методе
        self.rate_limits = {}

    def _load_faiss_index(self) -> tuple:
        """Загрузка FAISS индекса с проверкой ошибок"""
        if not all(os.path.exists(p) for p in [INDEX_PATH, METADATA_PATH]):
            raise FileNotFoundError("Отсутствуют файлы индекса")
        
        index = faiss.read_index(INDEX_PATH)
        with open(METADATA_PATH, "rb") as f:
            metadata = pickle.load(f)
        
        return index, metadata

    async def initialize(self):
        """Инициализация aiohttp сессии"""
        self.session = aiohttp.ClientSession()

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Основной обработчик сообщений"""
        # Ленивая инициализация сессии
        if self.session is None or self.session.closed:
            await self.initialize()
            
        user_id = update.effective_user.id
        if not self._check_rate_limit(user_id):
            await self._safe_send(update, "⚠️ Превышен лимит запросов. Попробуйте через минуту.")
            return

        try:
            query = update.message.text.strip()
            if not query:
                return

            # Этап 1: Поиск релевантных фрагментов
            chunks = await self._search_chunks(query)
            
            # Проверка на результаты поиска
            if not chunks:
                await self._safe_send(update, "К сожалению, не удалось найти релевантную информацию по вашему запросу.")
                return
                
            await asyncio.sleep(REQUEST_DELAY)

            # Этап 2: Генерация ответа
            answer = await self._generate_response(query, chunks)
            await self._safe_send(update, answer)

        except Exception as e:
            logging.error(f"Ошибка обработки: {str(e)}", exc_info=True)
            await self._safe_send(update, "⚠️ Произошла ошибка при обработке запроса")

    def _check_rate_limit(self, user_id: int) -> bool:
        """Проверка лимита запросов пользователя"""
        now = time.time()
        user_requests = self.rate_limits.get(user_id, [])
        
        # Удаляем старые запросы
        user_requests = [t for t in user_requests if now - t < 60]
        
        if len(user_requests) >= USER_RATE_LIMIT:
            return False
        
        user_requests.append(now)
        self.rate_limits[user_id] = user_requests
        return True

    async def _search_chunks(self, query: str) -> List[Dict]:
        """Поиск релевантных фрагментов"""
        try:
            embedding = await self._get_embedding(query)
            if not embedding:
                logging.error("Не удалось получить эмбеддинг")
                return []
                
            embedding_np = np.array([embedding]).astype('float32')
            faiss.normalize_L2(embedding_np)
            
            # Запускаем поиск в отдельном потоке, чтобы не блокировать
            D, indices = await asyncio.to_thread(
                lambda: self.index.search(embedding_np, 10)
            )
            
            results = []
            for idx in indices[0]:
                if 0 <= idx < len(self.metadata):
                    results.append({
                        "text": self.metadata[idx]["text"][:3000],
                        "source": self.metadata[idx].get("source", "")
                    })
            
            return results
        except Exception as e:
            logging.error(f"Ошибка поиска: {str(e)}", exc_info=True)
            return []

    async def _get_embedding(self, text: str) -> List[float]:
        """Получение эмбеддингов с повторными попытками"""
        for attempt in range(MAX_RETRIES + 1):  # +1 для корректного счета попыток
            try:
                # Ленивая инициализация сессии
                if self.session is None or self.session.closed:
                    await self.initialize()
                    
                async with self.session.post(
                    EMBEDDING_URL,
                    headers={"Authorization": f"Bearer {VENDOR_API_KEY}"},
                    json={"model": "emb-openai/text-embedding-3-small", "input": [text]},
                    timeout=30  # Добавляем таймаут
                ) as response:
                    if response.status == 429:
                        await self._handle_rate_limit(response)
                        continue
                        
                    response.raise_for_status()
                    data = await response.json()
                    
                    # Проверяем валидность ответа
                    if 'data' in data and len(data['data']) > 0 and 'embedding' in data['data'][0]:
                        return data['data'][0]['embedding']
                    else:
                        logging.error(f"Некорректный формат ответа: {data}")
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(5 * (attempt + 1))
                            continue
                        return None
                        
            except asyncio.TimeoutError:
                logging.warning(f"Таймаут API (попытка {attempt+1}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                return None
            except Exception as e:
                logging.error(f"Ошибка получения эмбеддинга (попытка {attempt+1}/{MAX_RETRIES}): {str(e)}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                return None

    async def _generate_response(self, query: str, chunks: List[Dict]) -> str:
        """Генерация ответа с обработкой ошибок"""
        context = "\n".join(f"[{i+1}] {chunk['text']}" for i, chunk in enumerate(chunks))
        
        # Обрезаем контекст, если он слишком большой
        if len(context) > MAX_CONTEXT_TOKENS * 4:  # примерно 4 байта на токен
            context = context[:MAX_CONTEXT_TOKENS * 4]
        
        for attempt in range(MAX_RETRIES + 1):  # +1 для корректного счета попыток
            try:
                # Ленивая инициализация сессии
                if self.session is None or self.session.closed:
                    await self.initialize()
                    
                async with self.session.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {VENDOR_API_KEY}"},
                    json={
                        "model": MODEL_ID,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": f"{context}\n\nВопрос: {query}"}
                        ],
                        "temperature": 0.3
                    },
                    timeout=60  # Увеличенный таймаут для генерации ответа
                ) as response:
                    if response.status == 429:
                        await self._handle_rate_limit(response)
                        continue
                        
                    response.raise_for_status()
                    
                    # Проверяем тип ответа
                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' not in content_type:
                        logging.error(f"Неправильный Content-Type: {content_type}")
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(8 * (attempt + 1))
                            continue
                        return "⚠️ Ошибка формата ответа от API"
                    
                    data = await response.json()
                    return self._process_response(data)
                    
            except asyncio.TimeoutError:
                logging.warning(f"Таймаут генерации (попытка {attempt+1}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(8 * (attempt + 1))
                    continue
                return "⚠️ Превышено время ожидания ответа от сервера"
                
            except Exception as e:
                logging.error(f"Ошибка генерации (попытка {attempt+1}/{MAX_RETRIES}): {str(e)}", exc_info=True)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(8 * (attempt + 1))
                    continue
                return "⚠️ Произошла ошибка при обработке запроса"

    def _process_response(self, data: dict) -> str:
        """Обработка и очистка ответа"""
        try:
            # Более детальная проверка структуры ответа
            if not isinstance(data, dict):
                logging.error(f"Неверный формат JSON: {type(data)}")
                return "⚠️ Получен некорректный ответ от API"
                
            if 'choices' not in data or not data['choices']:
                logging.error(f"Отсутствует поле 'choices': {data}")
                return "⚠️ В ответе API отсутствуют данные"
                
            if not isinstance(data['choices'], list) or not data['choices'][0]:
                logging.error(f"Неверный формат 'choices': {data['choices']}")
                return "⚠️ Некорректный формат данных в ответе API"
                
            if 'message' not in data['choices'][0]:
                logging.error(f"Отсутствует поле 'message': {data['choices'][0]}")
                return "⚠️ В ответе API отсутствуют необходимые данные"
                
            if 'content' not in data['choices'][0]['message']:
                logging.error(f"Отсутствует поле 'content': {data['choices'][0]['message']}")
                return "⚠️ В ответе API отсутствует содержимое"
            
            content = data['choices'][0]['message']['content']
            return self._sanitize_text(content)
            
        except Exception as e:
            logging.error(f"Ошибка обработки ответа: {str(e)}", exc_info=True)
            raise ValueError("Invalid API response")

    def _sanitize_text(self, text: str) -> str:
        """Очистка текста для Telegram"""
        if not text:
            return "Ответ пуст"
            
        # Удаление спецсимволов для корректной отправки
        # Используем более мягкую обработку
        try:
            # Удаление непечатаемых символов
            text = text.encode('utf-8', 'ignore').decode('utf-8')
            
            # Безопасное усечение
            text = text[:MAX_RESPONSE_LENGTH].strip()
            
            # Если в тексте нет спецсимволов markdown, то просто возвращаем
            if not any(c in text for c in '_*[]()~`>#+-=|{}.!'):
                return text
                
            # Иначе экранируем спецсимволы
            pattern = r'([_*\[\]()~`>#+\-=|{}.!])'
            return re.sub(pattern, r'\\\1', text)
        except Exception as e:
            logging.error(f"Ошибка при очистке текста: {str(e)}")
            return text[:MAX_RESPONSE_LENGTH].strip()

    async def _handle_rate_limit(self, response: aiohttp.ClientResponse):
        """Обработка 429 ошибки"""
        retry_after = int(response.headers.get('Retry-After', 20))
        logging.warning(f"Достигнут лимит API. Повтор через {retry_after} сек.")
        await asyncio.sleep(retry_after)

    async def _safe_send(self, update: Update, text: str):
        """Безопасная отправка сообщения"""
        try:
            # Разбиваем длинные сообщения на части
            if len(text) > 4096:  # Максимальная длина сообщения в Telegram
                chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
                for chunk in chunks:
                    await update.message.reply_text(
                        text=chunk,
                        parse_mode=None,
                        disable_web_page_preview=True
                    )
                    await asyncio.sleep(0.5)  # Небольшая задержка между частями
            else:
                await update.message.reply_text(
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logging.error(f"Ошибка отправки: {str(e)}")
            try:
                await update.message.reply_text(
                    text="⚠️ Ответ не может быть отображен",
                    parse_mode=None
                )
            except Exception as fallback_error:
                logging.error(f"Критическая ошибка отправки: {str(fallback_error)}")

    async def shutdown(self):
        """Корректное завершение работы"""
        if self.session and not self.session.closed:
            await self.session.close()

async def startup(application):
    """Функция инициализации для application"""
    bot_instance = application.bot_data.get("bot_instance")
    if bot_instance:
        await bot_instance.initialize()

async def shutdown(application):
    """Функция завершения для application"""
    bot_instance = application.bot_data.get("bot_instance")
    if bot_instance:
        await bot_instance.shutdown()

def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Токен бота не найден")

    application = ApplicationBuilder() \
        .token(TOKEN) \
        .rate_limiter(AIORateLimiter(
            overall_max_rate=40,
            overall_time_period=60
        )) \
        .build()

    bot = AnticorruptionBot(TOKEN)
    application.bot_data["bot_instance"] = bot  # Сохраняем экземпляр для доступа
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    # Добавляем обработчики запуска и завершения
    application.post_init = startup
    application.post_shutdown = shutdown
    
    application.run_polling()

if __name__ == "__main__":
    main()
