
import os
import faiss
import numpy as np
import aiohttp
import pickle
import logging
import asyncio
import re
import time
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
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
REQUEST_DELAY = 12
MAX_RETRIES = 3
USER_RATE_LIMIT = 8

SYSTEM_PROMPT = """Ты - ассистент, анализирующий документы. Отвечай точно и информативно,
используя только предоставленные фрагменты текста и делая оговорку: "согласно имеющейся информации". Делай ссылки на номера пунктов, если они указаны. Если информации недостаточно,
сообщи об этом. Запрещено указывать расширение файла (например, txt или doc)"""

class AnticorruptionBot:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.index, self.metadata = self._load_faiss_index()
        self.session = None
        self.rate_limits = {}
        self.bot_info = None

    def _load_faiss_index(self) -> tuple:
        if not all(os.path.exists(p) for p in [INDEX_PATH, METADATA_PATH]):
            raise FileNotFoundError("Отсутствуют файлы индекса")
        
        index = faiss.read_index(INDEX_PATH)
        with open(METADATA_PATH, "rb") as f:
            metadata = pickle.load(f)
        
        return index, metadata

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        self.bot_info = await self.bot.get_me()
        logging.info(f"Бот инициализирован: @{self.bot_info.username}")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.session is None or self.session.closed or self.bot_info is None:
            await self.initialize()
        
        is_private = update.effective_chat.type == "private"
        
        if not is_private:
            message_text = update.message.text
            bot_username = self.bot_info.username
            
            # Точное совпадение @username с границами слова
            mention_pattern = rf'@?{re.escape(bot_username)}\b'
            if not re.search(mention_pattern, message_text, re.IGNORECASE):
                return
            
            # Логирование для отладки
            logging.info(f"Original text: {message_text}")
            message_text = re.sub(mention_pattern, '', message_text, flags=re.IGNORECASE).strip()
            logging.info(f"Cleaned text: {message_text}")
            
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

    # Остальные методы класса остаются без изменений (handle_start, _check_rate_limit, 
    # _search_chunks, _get_embedding, _generate_response, _process_response и т.д.)

async def startup(application):
    bot_instance = application.bot_data.get("bot_instance")
    if bot_instance:
        await bot_instance.initialize()

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
