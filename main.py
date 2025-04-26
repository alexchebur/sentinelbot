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
    level=logging.DEBUG,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

PASSWORD = "123"
MAX_ATTEMPTS = 1
pending_verification = {}

async def debug_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, delay: int = 5):
    """Отправка и автоматическое удаление отладочных сообщений"""
    try:
        msg = await context.bot.send_message(chat_id, f"DEBUG: {text}")
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id, msg.message_id),
            delay
        )
    except Exception as e:
        logger.error(f"Debug message error: {e}")

async def delete_message_safe(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Безопасное удаление сообщения"""
    try:
        await context.bot.delete_message(chat_id, message_id)
        await debug_message(context, chat_id, f"Сообщение {message_id} удалено")
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения {message_id}: {e}")
        await debug_message(context, chat_id, f"Ошибка удаления: {e}")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых участников"""
    try:
        chat_id = update.effective_chat.id
        await debug_message(context, chat_id, "Новый пользователь обнаружен")
        
        # Удаление системного сообщения
        await update.message.delete()
        await debug_message(context, chat_id, "Системное сообщение удалено")

        for user in update.message.new_chat_members:
            if user.is_bot:
                continue

            user_id = user.id
            await debug_message(context, chat_id, f"Начата обработка пользователя {user_id}")

            # Отправка инструкции
            instructions = await context.bot.send_message(
                chat_id,
                f"👋 {user.mention_markdown()}, у вас 60 секунд для ввода кода!",
                parse_mode="Markdown"
            )
            await debug_message(context, chat_id, f"Инструкция отправлена: {instructions.message_id}")

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
            await debug_message(context, chat_id, f"Пользователь {user_id} зарегистрирован")

    except Exception as e:
        logger.error(f"Ошибка в welcome_new_member: {e}")
        await debug_message(context, chat_id, f"Ошибка: {str(e)}")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений пользователей"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await debug_message(context, chat_id, f"Получено сообщение от {user_id}")
    
    if user_id not in pending_verification:
        await debug_message(context, chat_id, "Пользователь не в списке проверки")
        return

    try:
        user_data = pending_verification[user_id]
        user_data["attempts"] += 1
        await debug_message(context, chat_id, f"Попытка {user_data['attempts']}/{MAX_ATTEMPTS}")
        
        # Удаление сообщения
        await delete_message_safe(chat_id, update.message.message_id, context)
        user_data["messages"].append(update.message.message_id)

        # Проверка пароля
        if update.message.text and update.message.text.strip() == PASSWORD:
            await debug_message(context, chat_id, "Верный пароль!")
            user_data["job"].schedule_removal()
            await delete_message_safe(chat_id, user_data["instructions_msg"], context)
            
            # Отправка подтверждения
            confirmation = await context.bot.send_message(
                chat_id,
                f"✅ {update.effective_user.mention_markdown()} верифицирован!",
                parse_mode="Markdown"
            )
            context.job_queue.run_once(
                lambda ctx: delete_message_safe(chat_id, confirmation.message_id, ctx),
                10,
                name=f"del_conf_{user_id}"
            )
            
            del pending_verification[user_id]
            return

        # Проверка попыток
        if user_data["attempts"] >= MAX_ATTEMPTS:
            await debug_message(context, chat_id, "Превышено количество попыток!")
            await execute_kick(user_id, context, "исчерпаны попытки")
            return

        # Уведомление об ошибке
        warning = await context.bot.send_message(
            chat_id,
            f"❌ Неверно! Осталось попыток: {MAX_ATTEMPTS - user_data['attempts']}"
        )
        context.job_queue.run_once(
            partial(delete_message_safe, chat_id, instructions.message_id),
                15,
                name=f"del_instr_{user_id}"
        )

    except Exception as e:
        logger.error(f"Ошибка в handle_user_message: {e}")
        await debug_message(context, chat_id, f"Ошибка обработки: {str(e)}")

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Обработка таймаута"""
    job = context.job
    chat_id, user_id, instr_msg = job.data
    await debug_message(context, chat_id, f"Сработал таймер кика для {user_id}")
    await execute_kick(user_id, context, "таймаут")

async def execute_kick(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    """Выполнение кика"""
    try:
        if user_id not in pending_verification:
            await debug_message(context, chat_id, "Пользователь не найден в очереди")
            return

        user_data = pending_verification[user_id]
        chat_id = user_data["chat_id"]
        await debug_message(context, chat_id, f"Начало кика {user_id} ({reason})")
        
        # Удаление сообщений
        for msg_id in user_data["messages"]:
            await delete_message_safe(chat_id, msg_id, context)
        
        # Удаление инструкции
        await delete_message_safe(chat_id, user_data["instructions_msg"], context)
        
        # Кик пользователя
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(seconds=30)
        await debug_message(context, chat_id, f"Пользователь {user_id} забанен")
        )
        
        # Уведомление
        notification = await context.bot.send_message(
            chat_id,
            f"🚫 Пользователь удалён ({reason})"
        )
        context.job_queue.run_once(
            lambda ctx: delete_message_safe(chat_id, notification.message_id, ctx),
            10,
            name=f"del_notif_{user_id}"
        )

    except Exception as e:
        logger.error(f"Ошибка кика {user_id}: {e}")
        await debug_message(context, chat_id, f"Ошибка кика: {str(e)}")
    finally:
        if user_id in pending_verification:
            del pending_verification[user_id]
            await debug_message(context, chat_id, f"Пользователь {user_id} удалён из очереди")

def main():
    application = Application.builder().token("7931308034:AAGoN08BoCi4eQl7fI-KFbgIvMYRwsVITAE").build()
    
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
