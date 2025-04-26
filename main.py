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
    level=logging.DEBUG,  # Включен режим отладки
    filename='bot.log'
)
logger = logging.getLogger(__name__)

PASSWORD = "SECRET123"
MAX_ATTEMPTS = 1
pending_verification = {}

async def delete_message_safe(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Безопасное удаление сообщения"""
    try:
        await context.bot.delete_message(chat_id, message_id)
        logger.debug(f"Сообщение {message_id} удалено")
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения {message_id}: {e}")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых участников"""
    try:
        logger.debug("Новый пользователь обнаружен")
        await update.message.delete()
        
        for user in update.message.new_chat_members:
            if user.is_bot:
                continue

            user_id = user.id
            chat_id = update.effective_chat.id
            
            # Отправка инструкции
            instructions = await update.effective_chat.send_message(
                f"👋 {user.mention_markdown()}, у вас 60 секунд для ввода кода!",
                parse_mode="Markdown"
            )
            logger.debug(f"Инструкция отправлена: {instructions.message_id}")
            
            # Задание на удаление инструкции
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(chat_id, instructions.message_id, ctx),
                15,
                name=f"del_instr_{user_id}"
            )

            # Регистрация задания на кик
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
            logger.info(f"Пользователь {user_id} зарегистрирован")

    except Exception as e:
        logger.error(f"Ошибка в welcome_new_member: {e}")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений"""
    user_id = update.effective_user.id
    logger.debug(f"Получено сообщение от {user_id}")
    
    if user_id not in pending_verification:
        logger.debug("Пользователь не в списке проверки")
        return

    try:
        user_data = pending_verification[user_id]
        user_data["attempts"] += 1
        logger.debug(f"Попытка {user_data['attempts']}/{MAX_ATTEMPTS}")
        
        # Удаление сообщения
        await delete_message_safe(update.effective_chat.id, update.message.message_id, context)
        user_data["messages"].append(update.message.message_id)

        # Проверка пароля
        if update.message.text and update.message.text.strip() == PASSWORD:
            logger.info(f"Пользователь {user_id} ввел верный пароль")
            user_data["job"].schedule_removal()
            await delete_message_safe(user_data["chat_id"], user_data["instructions_msg"], context)
            
            # Отправка подтверждения
            confirmation = await update.effective_chat.send_message(
                f"✅ {update.effective_user.mention_markdown()} верифицирован!",
                parse_mode="Markdown"
            )
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(confirmation.chat.id, confirmation.message_id, ctx),
                10,
                name=f"del_conf_{user_id}"
            )
            
            del pending_verification[user_id]
            return

        # Проверка попыток
        if user_data["attempts"] >= MAX_ATTEMPTS:
            logger.info(f"Превышены попытки для {user_id}")
            await execute_kick(user_id, context, "исчерпаны попытки")
            return

        # Уведомление об ошибке
        warning = await update.effective_chat.send_message(
            f"❌ Неверно! Осталось попыток: {MAX_ATTEMPTS - user_data['attempts']}"
        )
        context.job_queue.run_once(
            lambda ctx: delete_message_safe(warning.chat.id, warning.message_id, ctx),
            5,
            name=f"del_warn_{user_id}"
        )

    except Exception as e:
        logger.error(f"Ошибка в handle_user_message: {e}")

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Обработка таймаута"""
    job = context.job
    logger.debug(f"Сработал таймер кика: {job.name}")
    chat_id, user_id, instr_msg = job.data
    await execute_kick(user_id, context, "таймаут")

async def execute_kick(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    """Выполнение кика"""
    try:
        logger.info(f"Попытка кика {user_id}, причина: {reason}")
        
        if user_id not in pending_verification:
            logger.warning(f"Пользователь {user_id} не найден")
            return

        user_data = pending_verification[user_id]
        
        # Удаление сообщений
        for msg_id in user_data["messages"]:
            await delete_message_safe(user_data["chat_id"], msg_id, context)
        
        # Удаление инструкции
        await delete_message_safe(user_data["chat_id"], user_data["instructions_msg"], context)
        
        # Кик пользователя
        await context.bot.ban_chat_member(  # <-- Исправлено здесь
            chat_id=user_data["chat_id"],
            user_id=user_id,
            until_date=datetime.now() + timedelta(seconds=30)
        )  # Закрывающая скобка добавлена
        
        logger.info(f"Пользователь {user_id} забанен")  # Теперь это отдельная строка
        
        # Уведомление
        notification = await context.bot.send_message(
            user_data["chat_id"],
            f"🚫 Пользователь удалён ({reason})"
        )
        context.job_queue.run_once(
            lambda ctx: delete_message_safe(notification.chat.id, notification.message_id, ctx),
            10,
            name=f"del_notif_{user_id}"
        )

    except Exception as e:
        logger.error(f"Ошибка кика {user_id}: {e}")
    finally:
        if user_id in pending_verification:
            del pending_verification[user_id]
            logger.info(f"Пользователь {user_id} удален из очереди")
def main():
    application = Application.builder().token("7931308034:AAGoN08BoCi4eQl7fI-KFbgIvMYRwsVITAE").build()
    
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
