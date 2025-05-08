import os
import faiss
import numpy as np
import aiohttp
import pickle
import logging
import asyncio
import re
import time
import random
import json
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple, Any
from whoosh import index, highlight
from whoosh.analysis import Filter, RegexTokenizer, LowercaseFilter, StopFilter
from whoosh.fields import Schema, TEXT, ID
from whoosh.qparser import QueryParser
from whoosh.scoring import BM25F
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
    AIORateLimiter
)

# Конфигурация
VENDOR_API_KEY = "sk-or-vv-a8d6e009e2bbe09474b0679fbba83b015ff1c4f255ed76f33b48ccb1632bdc32"
INDEX_PATH = "/data/faiss_index.bin"
METADATA_PATH = "/data/metadata.pkl"
WHOOSH_INDEX_DIR = "/data/index"
EMBEDDING_MODEL = "text-embedding-3-small"  # Указываем модель явно
MODEL_ID = "google/gemini-flash-1.5"
API_URL = "https://api.vsegpt.ru/v1/chat/completions"
EMBEDDING_URL = "https://api.vsegpt.ru/v1/embeddings"

# Настройки
MAX_CONTEXT_TOKENS = 7000
MAX_RESPONSE_LENGTH = 4096 # Уменьшено для снижения вероятности ошибок
REQUEST_DELAY = 3  # Уменьшено время ожидания между запросами
MAX_RETRIES = 1  # Увеличено количество повторных попыток
USER_RATE_LIMIT = 8
MESSAGE_BROADCAST_INTERVAL = 60

SYSTEM_PROMPT = """Ты - ассистент, анализирующий документы в сфере противодействия коррупции. Отвечай точно и информативно,
используя только предоставленные фрагменты текста и делая оговорку: "согласно имеющейся информации". 
- Форматируй ответ простым текстом БЕЗ использования Markdown разметки
- Делай ссылки на названия документов и номера пунктов, если они указаны. Если номера пунктов не указаны, то не пиши об этом.
- Запрещено указывать расширение файла (например, txt или doc).
- Тебе известен перечень основных ЛНА Т Плюс в сфере противодействия коррупции: 
1. Антикоррупционная политика (утв. Приказом № 215 от 11.07.2022).
2. Кодекс делового поведения (утв. Решением Совета директоров 27.12.2023)
3. Положение о Комитете по этике и комплаенсу (утв. Правлением 02.03.2020)
4. Положение о подарках (утв. Приказом № 9 от 18.01.2021)
5. Положение и регламент приёма, обработки и рассмотрения обращений, поступающих на "Горячую линию" ПАО "Т Плюс" (утв. Приказом № 449 от 21.10.2024)
6. Положение о предотвращении и урегулировании конфликта интересов (утв. Приказом № 435 от 26.11.2021)
7. О ежегодном предоставлении декларации (утв. Приказом № 314 от 31.08.2023)"""

# Настройка логирования в файл
LOG_FILE_PATH = "/data/bot.log"
os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
file_handler = logging.FileHandler(LOG_FILE_PATH, mode='a', encoding='utf-8')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logging.getLogger('').addHandler(file_handler)

# 2. Кастомный анализатор
class CutEndingsFilter(Filter):
    def __call__(self, tokens):
        for token in tokens:
            text = token.text
            while len(text) > 3 and text[-1] in "аеёиоуыэюяй":
                text = text[:-1]
            if len(text) >= 4:
                token.text = text
                yield token

stopwords_list = frozenset({
    "на", "в", "под", "к", "над", "после", "до", "посреди", 
    "среди", "между", "тем", "самым", "это", "то", "эти", 
    "эта", "тот", "та", "оно", "они", "она", "он"
})

my_analyzer = (
    RegexTokenizer() | 
    LowercaseFilter() | 
    CutEndingsFilter() | 
    StopFilter(stoplist=stopwords_list)
)

class APIError(Exception):
    """Исключение для ошибок API"""
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(f"API Error {status_code}: {message}")

class AnticorruptionBot:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.index, self.metadata = self._load_faiss_index()
        self.whoosh_index = self._load_whoosh_index()
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limits: Dict[int, List[float]] = {}
        self.bot_info = None
        self.qa_pairs = self._load_qa_pairs()
        self.chat_ids = set()
        self.embedding_lock = asyncio.Lock()
        self.llm_lock = asyncio.Lock()
        # Кэши для уменьшения количества запросов
        self.embedding_cache = {}
        self.response_cache = {}
        # Параметры для прямого API запроса
        self.api_params = {
            "embedding": {
                "url": EMBEDDING_URL,
                "model": EMBEDDING_MODEL
            },
            "llm": {
                "url": API_URL,
                "model": MODEL_ID
            }
        }

    def _load_faiss_index(self) -> tuple:
        try:
            if not all(os.path.exists(p) for p in [INDEX_PATH, METADATA_PATH]):
                logging.error("Отсутствуют файлы индекса Faiss")
                # Создаем пустой индекс и метаданные для избежания сбоя
                empty_index = faiss.IndexFlatL2(1536)  # Стандартная размерность для эмбеддингов
                empty_metadata = []
                return empty_index, empty_metadata
            
            index = faiss.read_index(INDEX_PATH)
            with open(METADATA_PATH, "rb") as f:
                metadata = pickle.load(f)
            
            logging.info(f"Загружен Faiss индекс с {index.ntotal} векторами и {len(metadata)} метаданными")
            return index, metadata
        except Exception as e:
            logging.error(f"Ошибка загрузки Faiss индекса: {str(e)}", exc_info=True)
            # Создаем пустой индекс для избежания сбоя
            empty_index = faiss.IndexFlatL2(1536)
            empty_metadata = []
            return empty_index, empty_metadata

    def _load_whoosh_index(self):
        try:
            if not index.exists_in(WHOOSH_INDEX_DIR):
                logging.error(f"Индекс Whoosh не найден: {WHOOSH_INDEX_DIR}")
                return None
            
            return index.open_dir(WHOOSH_INDEX_DIR)
        except Exception as e:
            logging.error(f"Ошибка загрузки Whoosh индекса: {str(e)}", exc_info=True)
            return None

    def _load_qa_pairs(self) -> List[tuple]:
        qa_pairs = []
        try:
            file_path = "/data/qa_pairs.xml"
            if not os.path.exists(file_path):
                logging.warning(f"Файл qa_pairs.xml не найден: {file_path}")
                return []
                
            tree = ET.parse(file_path)
            root = tree.getroot()
            for pair in root.findall("pair"):
                question = pair.find("question").text if pair.find("question") is not None else ""
                answer = pair.find("answer").text if pair.find("answer") is not None else ""
                if question and answer:
                    qa_pairs.append((question, answer))
            logging.info(f"Загружено {len(qa_pairs)} пар вопрос-ответ из файла.")
        except Exception as e:
            logging.error(f"Ошибка загрузки qa_pairs.xml: {str(e)}", exc_info=True)
        return qa_pairs

    async def initialize(self):
        try:
            if self.session is None or self.session.closed:
                # Увеличиваем таймауты для избежания ошибок соединения
                timeout = aiohttp.ClientTimeout(total=60, connect=30, sock_connect=30, sock_read=30)
                self.session = aiohttp.ClientSession(timeout=timeout)
                logging.info("Создана новая HTTP сессия")
            
            if self.bot_info is None:
                self.bot_info = await self.bot.get_me()
                logging.info(f"Бот инициализирован: @{self.bot_info.username}")
                # Запускаем задачу для периодической рассылки сообщений
                asyncio.create_task(self._broadcast_qa_pairs())
                logging.info("Задача периодической рассылки сообщений запущена.")
        except Exception as e:
            logging.error(f"Ошибка инициализации бота: {str(e)}", exc_info=True)
            # Пересоздаем сессию в случае ошибки
            if self.session:
                try:
                    await self.session.close()
                except:
                    pass
                self.session = None

    async def _broadcast_qa_pairs(self):
        """Периодически рассылает случайные пары вопрос-ответ во все чаты."""
        while True:
            try:
                await asyncio.sleep(MESSAGE_BROADCAST_INTERVAL)
                if not self.qa_pairs or not self.chat_ids:
                    continue
                
                question, answer = random.choice(self.qa_pairs)
                message = f"Вопрос: {question}\n\nОтвет: {answer}"
                
                for chat_id in list(self.chat_ids):  # Копируем список, чтобы избежать изменений во время итерации
                    try:
                        await self.bot.send_message(chat_id=chat_id, text=message)
                        await asyncio.sleep(1)  # Пауза между сообщениями
                    except Exception as e:
                        logging.error(f"Ошибка отправки сообщения в чат {chat_id}: {str(e)}")
                        # Если чат недоступен, удаляем его из списка рассылки
                        if "blocked" in str(e).lower() or "not found" in str(e).lower():
                            self.chat_ids.discard(chat_id)
                            logging.info(f"Чат {chat_id} удален из списка рассылки")
            except Exception as e:
                logging.error(f"Ошибка в задаче рассылки: {str(e)}", exc_info=True)
                await asyncio.sleep(60)  # В случае ошибки ждем минуту перед повторной попыткой

    async def _update_chat_ids(self, update: Update):
        """Обновляет список ID чатов, где состоит бот."""
        chat_id = update.effective_chat.id
        if chat_id not in self.chat_ids:
            self.chat_ids.add(chat_id)
            chat_type = update.effective_chat.type
            chat_title = getattr(update.effective_chat, 'title', 'Личный чат')
            logging.info(f"Добавлен новый чат для рассылки: ID={chat_id}, Тип={chat_type}, Название={chat_title}")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Основной обработчик сообщений"""
        try:
            # Инициализация бота при необходимости
            if self.session is None or self.session.closed or self.bot_info is None:
                await self.initialize()
            
            await self._update_chat_ids(update)
            
            # Проверка на приватный чат или упоминание в групповом чате
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

            # Проверка ограничений по частоте
            user_id = update.effective_user.id
            if not self._check_rate_limit(user_id):
                await self._safe_send(update, "⚠️ Превышен лимит запросов. Попробуйте через минуту.")
                return

            # Получение и проверка запроса
            query = update.message.text.strip()
            if not query:
                return

            # Начинаем набор текста
            await self.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

            # Логируем запрос пользователя
            user_name = update.effective_user.first_name
            chat_type = update.effective_chat.type
            logging.info(f"Запрос: '{query}' от пользователя {user_name} (ID: {user_id}, чат: {chat_type})")

            # Ищем релевантные фрагменты для запроса
            combined_chunks = []
            try:
                # Поиск в Faiss
                faiss_chunks = await self._search_faiss_chunks(query)    	
                if faiss_chunks:
                    combined_chunks.extend(faiss_chunks)
                    logging.info(f"Найдено {len(faiss_chunks)} фрагментов в Faiss")
                
                # Поиск в Whoosh, если доступен
                if self.whoosh_index is not None:
                    whoosh_chunks = await self._search_whoosh_chunks(query)
                    if whoosh_chunks:
                        combined_chunks.extend(whoosh_chunks)
                        logging.info(f"Найдено {len(whoosh_chunks)} фрагментов в Whoosh")
            except Exception as e:
                logging.error(f"Ошибка при поиске фрагментов: {str(e)}", exc_info=True)
            
            if not combined_chunks:
                await self._safe_send(update, "К сожалению, не удалось найти релевантную информацию по вашему запросу.")
                return
            
            # Продолжаем набор текста
            await self.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

            # Генерация ответа с обработкой ошибок
            try:
                await asyncio.sleep(3) 
                # Прямая генерация ответа без использования функции-обертки для более четкой диагностики
                answer = await self._direct_llm_call(query, combined_chunks)
                
                if not answer:
                    logging.error("Получен пустой ответ от LLM после всех попыток")
                    await self._safe_send(update, "⚠️ Не удалось сгенерировать ответ, попробуйте позже.")
                    return
                
                # Форматирование ответа для группового чата
                if not is_private:
                    user_name = update.effective_user.first_name
                    answer = f"{user_name}, {answer}"
                
                # Отправка ответа
                await self._safe_send(update, answer)
                logging.info(f"Успешно отправлен ответ пользователю {user_id}, длина ответа: {len(answer)} символов")
            
            except Exception as e:
                logging.error(f"Ошибка при генерации/отправке ответа: {str(e)}", exc_info=True)
                await self._safe_send(update, "⚠️ Произошла ошибка при формировании ответа. Попробуйте упростить вопрос.")

        except Exception as e:
            logging.error(f"Общая ошибка обработки сообщения: {str(e)}", exc_info=True)
            # Только если сообщение не было еще отправлено
            try:
                await self._safe_send(update, "⚠️ Произошла ошибка при обработке запроса")
            except:
                pass

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._update_chat_ids(update)
        await self._safe_send(
            update,
            "Привет! Задайте мне вопрос по антикоррупционному законодательству."
        )

    def _check_rate_limit(self, user_id: int) -> bool:
        now = time.time()
        if user_id not in self.rate_limits:
            self.rate_limits[user_id] = []
        
        timestamps = [ts for ts in self.rate_limits[user_id] if now - ts < 60]
        if len(timestamps) >= USER_RATE_LIMIT:
            return False
        
        self.rate_limits[user_id] = timestamps + [now]
        return True

    async def _search_faiss_chunks(self, query: str) -> List[str]:
        """Поиск релевантных фрагментов в Faiss индексе"""
        try:
            # Проверяем кэш для повторных запросов
            if query in self.embedding_cache:
                embedding = self.embedding_cache[query]
            else:
                embedding = await self._get_embedding(query)
                if embedding is not None:
                    self.embedding_cache[query] = embedding
            
            if embedding is None:
                logging.warning("Не удалось получить эмбеддинг для запроса")
                return []
                
            if self.index.ntotal == 0:
                logging.warning("Faiss индекс пуст")
                return []
                
            # Поиск в индексе
            distances, indices = self.index.search(np.array([embedding]), min(5, self.index.ntotal))
            
            results = []
            for idx in indices[0]:
                if idx >= 0 and idx < len(self.metadata):
                    text = self.metadata[idx].get("text", "")
                    if text:
                        # Ограничиваем длину фрагментов
                        results.append(text[:2000])
            
            return results
        except Exception as e:
            logging.error(f"Ошибка при поиске в Faiss: {str(e)}", exc_info=True)
            return []

    async def _search_whoosh_chunks(self, query: str) -> List[str]:
        """Поиск релевантных фрагментов в Whoosh индексе"""
        try:
            if self.whoosh_index is None:
                return []
                
            chunks = []
            with self.whoosh_index.searcher(weighting=BM25F()) as searcher:
                qp = QueryParser("content", self.whoosh_index.schema)
                
                # Экранируем специальные символы в запросе для избежания ошибок парсера
                safe_query = re.sub(r'[^\w\s]', ' ', query)
                query_obj = qp.parse(safe_query)
                
                results = searcher.search(query_obj, limit=5)
                if not results:
                    return []
                    
                results.fragmenter = highlight.SentenceFragmenter(maxchars=2000)
                
                for hit in results:
                    # Пытаемся получить подсвеченные фрагменты
                    try:
                        highlights = hit.highlights("content")
                        if highlights:
                            fragments = highlights.split("...")
                            for frag in fragments:
                                if frag.strip():
                                    chunks.append(frag.strip()[:2000])
                                    break
                        else:
                            # Если подсветка не удалась, берем начало документа
                            content = hit.get("content", "")
                            if content:
                                chunks.append(content[:2000])
                    except Exception as e:
                        logging.warning(f"Ошибка при обработке результата Whoosh: {str(e)}")
            
            return chunks
        except Exception as e:
            logging.error(f"Ошибка при поиске в Whoosh: {str(e)}", exc_info=True)
            return []

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Получение эмбеддинга с обработкой ошибок и повторными попытками"""
        async with self.embedding_lock:
            # Проверяем кэш
            if text in self.embedding_cache:
                return self.embedding_cache[text]
                
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    headers = {
                        "Authorization": f"Bearer {VENDOR_API_KEY}", 
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": self.api_params["embedding"]["model"],
                        "input": text
                    }
                    
                    # Логируем запрос эмбеддингов
                    logging.info(f"Запрос эмбеддингов (попытка {attempt}/{MAX_RETRIES})")
                    
                    # Отправляем запрос
                    async with self.session.post(
                        self.api_params["embedding"]["url"], 
                        json=payload, 
                        headers=headers
                    ) as response:
                        # Проверяем статус ответа
                        if response.status != 200:
                            error_text = await response.text()
                            logging.error(
                                f"Ошибка API эмбеддингов (попытка {attempt}/{MAX_RETRIES}): " 
                                f"Статус {response.status}, ответ: {error_text}"
                            )
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(1 * attempt)
                                continue
                            return None
                        
                        # Читаем и валидируем данные
                        data = await response.json()
                        if 'data' not in data or not data['data']:
                            logging.error(f"Некорректный формат ответа API эмбеддингов (отсутствует data): {data}")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(1 * attempt)
                                continue
                            return None
                            
                        if 'embedding' not in data['data'][0]:
                            logging.error(f"Некорректный формат ответа API эмбеддингов (отсутствует embedding): {data}")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(1 * attempt)
                                continue
                            return None
                        
                        # Успешно получили эмбеддинг
                        embedding = data['data'][0]['embedding']
                        self.embedding_cache[text] = embedding  # Сохраняем в кэш
                        return embedding
                        
                except aiohttp.ClientError as e:
                    logging.error(
                        f"Сетевая ошибка при запросе эмбеддингов (попытка {attempt}/{MAX_RETRIES}): {str(e)}"
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(1 * attempt)
                    else:
                        return None
                except Exception as e:
                    logging.error(f"Неожиданная ошибка при получении эмбеддингов: {str(e)}", exc_info=True)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(1 * attempt)
                    else:
                        return None
            
            # Если все попытки исчерпаны
            return None

    async def _direct_llm_call(self, query: str, chunks: List[str]) -> Optional[str]:
        """Прямой вызов LLM API с подробной обработкой ошибок"""
        cache_key = f"{query}_{hash(tuple(chunks))}"  # Ключ кэша на основе запроса и контекста
        await asyncio.sleep(2) 
        # Проверяем кэш
        if cache_key in self.response_cache:
            logging.info("Ответ взят из кэша")
            return self.response_cache[cache_key]
        
        async with self.llm_lock:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    headers = {
                        "Authorization": f"Bearer {VENDOR_API_KEY}", 
                        "Content-Type": "application/json"
                    }
                    
                    # Ограничиваем количество фрагментов для предотвращения переполнения контекста
                    limited_chunks = chunks[:5]
                    # Ограничиваем длину каждого фрагмента
                    limited_chunks = [chunk[:1500] for chunk in limited_chunks]
                    
                    # Форматируем контекст
                    context = "\n".join([f"[Пункт {i+1}] {chunk}" for i, chunk in enumerate(limited_chunks)])
                    
                    # Формируем сообщения для API
                    messages = [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Вопрос: {query}\n\nКонтекст:\n{context}"}
                    ]
                    
                    # Формируем полный запрос
                    payload = {
                        "model": self.api_params["llm"]["model"],
                        "messages": messages,
                        "max_tokens": MAX_RESPONSE_LENGTH,
                        "temperature": 0.3  # Снижаем температуру для более стабильных ответов
                    }
                    
                    # Логируем запрос к LLM
                    logging.info(f"Запрос к LLM API (попытка {attempt}/{MAX_RETRIES})")
                    
                    # Отправляем запрос
                    async with self.session.post(
                        self.api_params["llm"]["url"], 
                        json=payload, 
                        headers=headers
                    ) as response:
                        # Проверяем статус
                        if response.status != 200:
                            error_text = await response.text()
                            logging.error(
                                f"Ошибка LLM API (попытка {attempt}/{MAX_RETRIES}): "
                                f"Статус {response.status}, ответ: {error_text}"
                            )
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось получить ответ от сервиса. Пожалуйста, попробуйте позже."
                        
                        # Получаем и проверяем содержимое ответа
                        try:
                            response_data = await response.json()
                        except json.JSONDecodeError as e:
                            text_response = await response.text()
                            logging.error(
                                f"Ошибка декодирования JSON (попытка {attempt}/{MAX_RETRIES}): {str(e)}, "
                                f"полученный текст: {text_response[:200]}..."
                            )
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось обработать ответ от сервиса. Пожалуйста, попробуйте позже."
                        
                        # Проверка структуры ответа
                        if 'choices' not in response_data or not response_data['choices']:
                            logging.error(f"Отсутствуют choices в ответе LLM: {response_data}")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось сформировать ответ. Попробуйте задать вопрос иначе."
                        
                        if 'message' not in response_data['choices'][0]:
                            logging.error(f"Отсутствует message в ответе LLM: {response_data}")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось сформировать ответ. Попробуйте задать вопрос иначе."
                        
                        if 'content' not in response_data['choices'][0]['message']:
                            logging.error(f"Отсутствует content в ответе LLM: {response_data}")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось сформировать ответ. Попробуйте задать вопрос иначе."
                        
                        # Получаем и обрабатываем текст ответа
                        raw_answer = response_data['choices'][0]['message']['content']
                        if not raw_answer:
                            logging.error("Пустой ответ от LLM")
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(2 * attempt)
                                continue
                            return "Не удалось сформировать ответ. Попробуйте задать вопрос иначе."
                        
                        # Очищаем и форматируем ответ
                        cleaned_answer = self._clean_markdown(raw_answer)
                        
                        # Сохраняем в кэш
                        self.response_cache[cache_key] = cleaned_answer
                        
                        # Логируем успешный запрос
                        logging.info(f"Успешно получен ответ от LLM API, длина: {len(cleaned_answer)}")
                        
                        return cleaned_answer
                
                except aiohttp.ClientError as e:
                    logging.error(f"Сетевая ошибка при запросе к LLM API (попытка {attempt}/{MAX_RETRIES}): {str(e)}")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(2 * attempt)
                    else:
                        return "Ошибка соединения с сервером. Пожалуйста, попробуйте позже."
                except Exception as e:
                    logging.error(f"Неожиданная ошибка при генерации ответа (попытка {attempt}/{MAX_RETRIES}): {str(e)}", exc_info=True)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(2 * attempt)
                    else:
                        return "Произошла ошибка при обработке запроса. Пожалуйста, попробуйте позже."
            
            # Если все попытки исчерпаны и не увенчались успехом
            return "К сожалению, сервис временно недоступен. Пожалуйста, попробуйте позже."

    def _clean_markdown(self, text: str) -> str:
        """Очищает текст от markdown-разметки"""
        if not text:
            return ""
        
        # Паттерны для удаления markdown-разметки    
        markdown_patterns = [
            (r'\*\*(.*?)\*\*', r'\1'),  # Bold
            (r'\*(.*?)\*', r'\1'),      # Italic
            (r'__(.*?)__', r'\1'),      # Bold
            (r'_(.*?)_', r'\1'),        # Italic
            (r'`{3}(.*?)`{3}', r'\1'),  # Code block
            (r'`(.*?)`', r'\1'),        # Inline code
            (r'~~(.*?)~~', r'\1'),      # Strikethrough
            (r'\[(.*?)\]\((.*?)\)', r'\1'), # Links
            (r'^#{1,6}\s+(.*)$', r'\1', re.MULTILINE), # Headers
        ]
        
        cleaned_text = text
        for pattern in markdown_patterns:
            cleaned_text = re.sub(pattern, r'\1', cleaned_text, flags=re.MULTILINE|re.DOTALL)
        
        # Дополнительная очистка
        cleaned_text = re.sub(r'\\([\\`*_{}[\]()#+\-.!])', r'\1', cleaned_text)  # Удаление экранирования
        
        return cleaned_text.strip()

    async def _safe_send(self, update: Update, text: str):
        """Безопасно отправляет сообщение пользователю с обработкой ошибок"""
        if not text:
            text = "К сожалению, не удалось сформировать ответ."
            
        try:
            # Ограничиваем размер сообщения, чтобы не превысить лимиты Telegram
            truncated_text = text[:MAX_RESPONSE_LENGTH]
            await update.message.reply_text(truncated_text, parse_mode=None)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения: {str(e)}", exc_info=True)
            try:
                # Пробуем отправить более короткое сообщение
                await update.message.reply_text("Произошла ошибка при отправке полного ответа.")
            except Exception as e2:
                logging.error(f"Критическая ошибка отправки запасного сообщения: {str(e2)}")

async def shutdown(application):
    """Корректно закрывает ресурсы при завершении работы бота"""
    bot_instance = application.bot_data.get("bot_instance")
    if bot_instance and bot_instance.session:
        await bot_instance.session.close()
        logging.info("Сессия HTTP закрыта")

def main():
    # Базовая настройка логирования (консольный вывод)
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Токен бота не найден")

    application = ApplicationBuilder() \
        .token(TOKEN) \
        .rate_limiter(AIORateLimiter(overall_max_rate=40, overall_time_period=60)) \
        .build()

    bot = AnticorruptionBot(TOKEN)
    application.bot_data["bot_instance"] = bot
    
    application.add_handler(CommandHandler("start", bot.handle_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    application.post_shutdown = shutdown
    
    logging.info("Бот запускается...")
    application.run_polling()

if __name__ == "__main__":
    main()
