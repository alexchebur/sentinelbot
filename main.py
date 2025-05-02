
import os
import faiss
import numpy as np
import aiohttp
import pickle
import logging
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
    AIORateLimiter,
    JobQueue
)
from typing import List, Dict, Optional

# Конфигурация
VENDOR_API_KEY = "sk-or-vv-a8d6e009e2bbe09474b0679fbba83b015ff1c4f255ed76f33b48ccb1632bdc32"
INDEX_PATH = "/data/faiss_index.bin"
METADATA_PATH = "/data/metadata.pkl"
QA_PAIRS_PATH = "/data/qa_pairs.xml"
MODEL_ID = "google/gemini-flash-1.5"
API_URL = "https://api.vsegpt.ru/v1/chat/completions"
EMBEDDING_URL = "https://api.vsegpt.ru/v1/embeddings"

# Настройки
MAX_CONTEXT_TOKENS = 7000
MAX_RESPONSE_LENGTH = 10000
REQUEST_DELAY = 12
MAX_RETRIES = 3
USER_RATE_LIMIT = 8
BROADCAST_INTERVAL = 3600  # 1 час в секундах
BROADCAST_INITIAL_DELAY = 10  # Задержка перед первой рассылкой в секундах

SYSTEM_PROMPT = """Ты - ассистент, анализирующий документы. Отвечай точно и информативно,
используя только предоставленные фрагменты текста и делая оговорку: "согласно имеющейся информации". 
- Форматируй ответ простым текстом БЕЗ использования Markdown разметки
- Делай ссылки на номера пунктов, если они указаны
- Если информации недостаточно, сообщи об этом
- Запрещено указывать расширение файла (например, txt или doc)"""

class AnticorruptionBot:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.index, self.metadata = self._load_faiss_index()
        self.qa_pairs = self._load_qa_pairs()
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limits: Dict[int, List[float]] = {}
        self.bot_info = None
        self.active_chats: set[int] = set()
        self.broadcast_lock = asyncio.Lock()

    def _load_faiss_index(self) -> tuple:
        if not all(os.path.exists(p) for p in [INDEX_PATH, METADATA_PATH]):
            raise FileNotFoundError("Отсутствуют файлы индекса")
        
        index = faiss.read_index(INDEX_PATH)
        with open(METADATA_PATH, "rb") as f:
            metadata = pickle.load(f)
        
        return index, metadata

    def _load_qa_pairs(self) -> List[Dict[str, str]]:
        if not os.path.exists(QA_PAIRS_PATH):
            raise FileNotFoundError(f"Файл QA пар не найден по пути: {QA_PAIRS_PATH}")
        
        try:
            tree = ET.parse(QA_PAIRS_PATH)
            root = tree.getroot()
            return [
                {
                    "question": pair.find("question").text.strip(),
                    "answer": pair.find("answer").text.strip()
                }
                for pair in root.findall("pair")
                if pair.find("question") is not None and pair.find("answer") is not None
            ]
        except Exception as e:
            raise ValueError(f"Ошибка парсинга QA пар: {str(e)}")

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        self.bot_info = await self.bot.get_me()
        logging.info(f"Бот инициализирован: @{self.bot_info.username}")

    async def _track_chat(self, chat_id: int):
        self.active_chats.add(chat_id)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._track_chat(update.effective_chat.id)
        
        if self.session is None or self.session.closed or self.bot_info is None:
            await self.initialize()
        
        is_private = update.effective_chat.type == "private"
        
        if not is_private:
            message_text = update.message.text
            bot_username = self.bot_info.username
            
            mention_pattern = rf'@?{re.escape(bot_username)}\b'
            if not re.search(mention_pattern, message_text, re.IGNORECASE):
                return
            
            message_text = re.sub(mention_pattern, '', message_text, flags=re.IGNORECASE).strip()
            
            if not message_text:
                await self._safe_send(update, "Какой у вас вопрос?")
                return
                
            update.message.text = message_text

        user_id = update.effective_user.id
        if not self._check_rate_limit(user_id):
            await self._safe_send(update, "⚠️ Превышен лимит запросов. Попробуйте через минуту.")
            return

        try:
            query = update.message.text.strip()
            if not query:
                return

            chunks = await self._search_chunks(query)
            if not chunks:
                await self._safe_send(update, "К сожалению, не удалось найти релевантную информацию по вашему запросу.")
                return
                
            await asyncio.sleep(REQUEST_DELAY)

            answer = await self._generate_response(query, chunks)
            
            if not is_private:
                user_name = update.effective_user.first_name
                answer = f"{user_name}, {answer}"
                
            await self._safe_send(update, answer)

        except Exception as e:
            logging.error(f"Ошибка обработки: {str(e)}", exc_info=True)
            await self._safe_send(update, "⚠️ Произошла ошибка при обработке запроса")

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._track_chat(update.effective_chat.id)
        await self._safe_send(update, "Привет! Задайте мне вопрос по антикоррупционному законодательству.")

    def _check_rate_limit(self, user_id: int) -> bool:
        now = time.time()
        if user_id not in self.rate_limits:
            self.rate_limits[user_id] = []
        
        timestamps = [ts for ts in self.rate_limits[user_id] if now - ts < 60]
        if len(timestamps) >= USER_RATE_LIMIT:
            return False
        
        self.rate_limits[user_id] = timestamps + [now]
        return True

    async def _search_chunks(self, query: str) -> List[str]:
        embedding = await self._get_embedding(query)
        distances, indices = self.index.search(np.array([embedding]), 5)
        
        results = []
        for idx in indices[0]:
            if idx < len(self.metadata):
                text = self.metadata[idx].get("text", "")
                if text:
                    results.append(text[:1000])
        return results

    async def _get_embedding(self, text: str) -> List[float]:
        headers = {
            "Authorization": f"Bearer {VENDOR_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "text-embedding-3-small",
            "input": text
        }

        async with self.session.post(EMBEDDING_URL, json=payload, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()
            return data['data'][0]['embedding']

    async def _generate_response(self, query: str, chunks: List[str]) -> str:
        headers = {
            "Authorization": f"Bearer {VENDOR_API_KEY}",
            "Content-Type": "application/json"
        }
        
        context = "\n".join([f"[Пункт {i+1}] {chunk}" for i, chunk in enumerate(chunks)])
        prompt = f"Вопрос: {query}\nКонтекст:\n{context}"

        payload = {
            "model": MODEL_ID,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": MAX_RESPONSE_LENGTH
        }

        async with self.session.post(API_URL, json=payload, headers=headers) as response:
            response_data = await response.json()
            raw_answer = response_data['choices'][0]['message']['content']
            return self._clean_markdown(raw_answer)

    def _clean_markdown(self, text: str) -> str:
        markdown_patterns = [
            r'\*\*(.*?)\*\*',    # Жирный текст
            r'\*(.*?)\*',         # Курсив
            r'~~(.*?)~~',         # Зачеркивание
            r'\[(.*?)\]\(.*?\)',  # Ссылки
            r'`{3}.*?\n',         # Блоки кода
            r'`',                 # Инлайн код
            r'^#+\s*',           # Заголовки
        ]

        for pattern in markdown_patterns:
            text = re.sub(pattern, r'\1', text, flags=re.MULTILINE|re.DOTALL)
        
        return text.strip()

    async def _safe_send(self, update: Update, text: str):
        try:
            await update.message.reply_text(text[:MAX_RESPONSE_LENGTH])
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения: {str(e)}")

    async def broadcast_random_qa(self, context: ContextTypes.DEFAULT_TYPE):
        if not self.qa_pairs:
            logging.error("Нет доступных QA пар для рассылки")
            return

        async with self.broadcast_lock:
            pair = np.random.choice(self.qa_pairs)
            message = f"❓ Вопрос дня:\n{pair['question']}\n\n💡 Ответ:\n{pair['answer']}"

            errors = []
            for chat_id in list(self.active_chats):
                try:
                    await self.bot.send_message(chat_id=chat_id, text=message)
                    await asyncio.sleep(0.1)  # Небольшая задержка между сообщениями
                except Exception as e:
                    errors.append((chat_id, str(e)))
                    logging.error(f"Ошибка рассылки в чат {chat_id}: {str(e)}")

            if errors:
                error_msg = "⚠️ Ошибки при рассылке:\n" + "\n".join(
                    f"Чат {cid}: {err}" for cid, err in errors[:5]  # Ограничиваем количество выводимых ошибок
                )
                if len(errors) > 5:
                    error_msg += f"\n...и ещё {len(errors) - 5} ошибок"
                
                # Отправляем ошибки в первый доступный чат (например, админу)
                if self.active_chats:
                    await self._safe_send_to_chat(next(iter(self.active_chats)), error_msg[:4000])

    async def _safe_send_to_chat(self, chat_id: int, text: str):
        try:
            await self.bot.send_message(chat_id=chat_id, text=text[:MAX_RESPONSE_LENGTH])
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения в чат {chat_id}: {str(e)}")

    async def shutdown(self):
        if self.session:
            await self.session.close()

async def startup(application):
    bot_instance = application.bot_data.get("bot_instance")
    if bot_instance:
        await bot_instance.initialize()
        # Запускаем периодическую рассылку
        application.job_queue.run_repeating(
            bot_instance.broadcast_random_qa,
            interval=BROADCAST_INTERVAL,
            first=BROADCAST_INITIAL_DELAY
        )

async def shutdown(application):
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
        .job_queue(JobQueue()) \
        .build()

    bot = AnticorruptionBot(TOKEN)
    application.bot_data["bot_instance"] = bot
    
    application.add_handler(CommandHandler("start", bot.handle_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    application.post_init = startup
    application.post_shutdown = shutdown
    
    logging.info("Бот запускается...")
    application.run_polling()

if __name__ == "__main__":
    main()
