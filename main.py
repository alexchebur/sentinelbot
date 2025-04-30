import os
import chromadb
import requests
import logging
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters
)
from typing import List

# ========== КОНФИГУРАЦИЯ ==========
CHROMA_DB_PATH = "/data/chroma_db/"
VENDOR_API_KEY = "sk-or-vv-a8d6e009e2bbe09474b0679fbba83b015ff1c4f255ed76f33b48ccb1632bdc32"

# Модели и параметры
EMBEDDING_MODEL = "emb-openai/text-embedding-3-small"
LLM_MODEL = "google/gemini-flash-1.5"
TEMPERATURE = 0.3
SYSTEM_PROMPT = """Ты - ассистент, анализирующий документы. Отвечай точно и информативно,
используя только предоставленные фрагменты текста и делая оговорку: "согласно имеющейся информации". Делай ссылки на номера пунктов, если они указаны. Если информации недостаточно,
сообщи об этом. Запрещено указывать расширение файла (например, txt или doc)"""

# API эндпойнты
EMBEDDING_API_URL = "https://api.vsegpt.ru/v1/embeddings"
CHAT_API_URL = "https://api.vsegpt.ru/v1/chat/completions"

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
class CustomEmbedder:
    def __init__(self, api_key: str, model_name: str):
        self.api_key = api_key
        self.model_name = model_name

    def __call__(self, input: List[str]) -> List[List[float]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {"model": self.model_name, "input": input}
        try:
            response = requests.post(EMBEDDING_API_URL, headers=headers, json=data)
            response.raise_for_status()
            return [item['embedding'] for item in response.json()['data']]
        except Exception as e:
            logging.error(f"Embedding error: {str(e)}")
            raise

def initialize_chroma():
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    embedder = CustomEmbedder(VENDOR_API_KEY, EMBEDDING_MODEL)
    try:
        return client.get_collection(
            name="documents_collection",
            embedding_function=embedder
        )
    except Exception as e:
        logging.info("Creating new collection...")
        return client.create_collection(
            name="documents_collection",
            embedding_function=embedder
        )

# ========== ОСНОВНОЙ КЛАСС БОТА ==========
class AnticorruptionBot:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.collection = initialize_chroma()
        
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик входящих сообщений"""
        user_query = update.message.text
        chat_id = update.effective_chat.id
        
        try:
            chunks = self.search_chunks(user_query)
            answer = self.generate_answer(user_query, chunks)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=answer,
                parse_mode='Markdown'
            )
        except Exception as e:
            logging.error(f"Error processing message: {str(e)}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="Произошла ошибка при обработке запроса"
            )

    def search_chunks(self, query: str, n_results: int = 5) -> List[dict]:
        """Поиск в ChromaDB"""
        results = self.collection.query(
            query_texts=[query],
            n_results=n_results
        )
        return [{
            "text": doc[:5000],
            "source": metadata["source"]
        } for doc, metadata in zip(results['documents'][0], results['metadatas'][0])]

    def generate_answer(self, query: str, chunks: List[dict]) -> str:
        """Генерация ответа через LLM"""
        if not chunks:
            return "Не найдено релевантной информации"

        context = "\n\n".join([f"Источник: {chunk['source']}\nТекст: {chunk['text']}" 
                              for chunk in chunks])

        headers = {
            "Authorization": f"Bearer {VENDOR_API_KEY}",
            "Content-Type": "application/json"
        }

        data = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "assistant", "content": context},
                {"role": "user", "content": query}
            ],
            "temperature": TEMPERATURE
        }

        try:
            response = requests.post(CHAT_API_URL, headers=headers, json=data)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            logging.error(f"LLM API error: {str(e)}")
            return "Ошибка при генерации ответа"

# ========== ЗАПУСК И НАСТРОЙКА ==========
def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения")
    
    application = ApplicationBuilder().token(TOKEN).build()
    bot = AnticorruptionBot(TOKEN)
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    main()
