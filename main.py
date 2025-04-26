import logging
import asyncio
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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

PASSWORD = "SECRET123"  # Замените на ваш пароль
MAX_ATTEMPTS = 3        # Максимальное количество попыток
pending_verification = {}

async def delete_message_safe(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Безопасное удаление сообщения с обработкой ошибок"""
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения {message_id}: {e}")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых участников группы"""
    try:
        await update.message.delete()  # Удаляем системное сообщение
        
        for user in update.message.new_chat_members:
            if user.is_bot:
                continue

            user_id = user.id
            chat_id = update.effective_chat.id
            
            # Отправка временной инструкции
            instructions = await update.effective_chat.send_message(
                f"👋 {user.mention_markdown()}, отправьте кодовое слово в чат в течение 60 секунд!",
                parse_mode="Markdown"
            )
            
            # Настройка задания на удаление инструкции
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(chat_id, instructions.message_id, ctx),
                15,
                data=instructions.message_id
            )

            # Регистрация пользователя
            job = context.job_queue.run_once(
                kick_user_callback,
                60,
                data=(chat_id, user_id, instructions.message_id),
                name=f"kick_{user_id}"
            )
            
            pending_verification[user_id] = {
                "chat_id": chat_id,
                "job": job,
                "instructions_msg": instructions.message_id,
                "messages": [],
                "attempts": 0
            }

    except Exception as e:
        logger.error(f"Ошибка в welcome_new_member: {e}")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений от новых пользователей"""
    user_id = update.effective_user.id
    if user_id not in pending_verification:
        return

    try:
        user_data = pending_verification[user_id]
        user_data["attempts"] += 1
        
        # Удаление сообщения пользователя
        await delete_message_safe(update.effective_chat.id, update.message.message_id, context)
        user_data["messages"].append(update.message.message_id)

        # Проверка кодового слова
        if update.message.text and update.message.text.strip() == PASSWORD:
            # Успешная верификация
            user_data["job"].schedule_removal()
            
            # Удаление инструкции
            await delete_message_safe(user_data["chat_id"], user_data["instructions_msg"], context)
            
            # Временное подтверждение
            confirmation = await update.effective_chat.send_message(
                f"✅ {update.effective_user.mention_markdown()} верифицирован!",
                parse_mode="Markdown"
            )
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(confirmation.chat.id, confirmation.message_id, ctx),
                10,
                data=confirmation.message_id
            )
            
            del pending_verification[user_id]
        elif user_data["attempts"] >= MAX_ATTEMPTS:
            # Превышено количество попыток
            await execute_kick(user_id, context, reason="исчерпаны попытки")
        else:
            # Уведомление о неверной попытке
            warning = await update.effective_chat.send_message(
                f"❌ Неверное кодовое слово! Осталось попыток: {MAX_ATTEMPTS - user_data['attempts']}"
            )
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(warning.chat.id, warning.message_id, ctx),
                5,
                data=warning.message_id
            )

    except Exception as e:
        logger.error(f"Ошибка в handle_user_message: {e}")

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Кик пользователя по таймауту"""
    job = context.job
    chat_id, user_id, instructions_msg = job.data
    
    if user_id in pending_verification:
        await execute_kick(user_id, context, reason="таймаут")

async def execute_kick(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    """Выполнение кика пользователя"""
    try:
        user_data = pending_verification.get(user_id)
        if not user_data:
            return

        # Удаление сообщений
        for msg_id in user_data["messages"]:
            await delete_message_safe(user_data["chat_id"], msg_id, context)
        
        # Удаление инструкции
        await delete_message_safe(user_data["chat_id"], user_data["instructions_msg"], context)
        
        # Кик пользователя
        await context.bot.ban_chat_member(
            chat_id=user_data["chat_id"],
            user_id=user_id,
            until_date=datetime.now() + timedelta(seconds=30)
        
        # Уведомление о кике
        notification = await context.bot.send_message(
            user_data["chat_id"],
            f"🚫 Пользователь удалён ({reason})"
        )
        context.job_queue.run_once(
            lambda ctx: delete_message_safe(notification.chat.id, notification.message_id, ctx),
            10,
            data=notification.message_id
        )

    except Exception as e:
        logger.error(f"Ошибка при кике пользователя {user_id}: {e}")
    finally:
        if user_id in pending_verification:
            del pending_verification[user_id]

def main():
    application = Application.builder().token("7931308034:AAGoN08BoCi4eQl7fI-KFbgIvMYRwsVITAE").build()
    
    # Регистрация обработчиков
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message)
    )
    
    application.run_polling()

if __name__ == "__main__":
    main()
