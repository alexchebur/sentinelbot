import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

PASSWORD = "123"  # Замените на ваше кодовое слово
pending_verification = {}

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик новых участников группы"""
    for user in update.message.new_chat_members:
        if user.is_bot:
            continue
        
        user_id = user.id
        chat_id = update.effective_chat.id
        
        # Отправляем приветственное сообщение
        await update.effective_chat.send_message(
            f"👋 Привет, {user.full_name}! У тебя есть 60 секунд чтобы отправить кодовое слово в чат."
        )
        
        # Создаем задание для проверки
        job = context.job_queue.run_once(
            kick_user_callback, 
            60, 
            data=(chat_id, user_id),
            name=str(user_id)
        )
        
        pending_verification[user_id] = {
            "job": job,
            "chat_id": chat_id
        }

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик входящих сообщений"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id not in pending_verification:
        return
    
    if update.message.text.strip() == PASSWORD:
        # Удаляем задание при успешной проверке
        job = pending_verification[user_id]["job"]
        job.schedule_removal()
        del pending_verification[user_id]
        
        await update.message.reply_text("✅ Верно! Добро пожаловать в группу!")
    else:
        await update.message.reply_text("❌ Неверное кодовое слово! Попробуй еще.")

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Коллбэк для удаления пользователя"""
    job = context.job
    chat_id, user_id = job.data
    
    if user_id not in pending_verification:
        return
    
    try:
        # Блокируем и сразу разблокируем пользователя
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(seconds=30)
        
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Время вышло! Пользователь был удален."
        )
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя: {e}")
    finally:
        if user_id in pending_verification:
            del pending_verification[user_id]

def main():
    application = Application.builder().token("7931308034:AAGoN08BoCi4eQl7fI-KFbgIvMYRwsVITAE").build()
    
    # Добавляем обработчики
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    application.run_polling()

if __name__ == "__main__":
    main()
