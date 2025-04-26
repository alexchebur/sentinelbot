import logging
import asyncio
from datetime import datetime, timedelta
from functools import partial
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue,
    CommandHandler
)
from telegram.error import RetryAfter

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

PASSWORD = "123"
MAX_ATTEMPTS = 1
pending_verification = {}

async def debug_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """Отправка отладочных сообщений в группу"""
    try:
        await context.bot.send_message(
            chat_id,
            f"🔧 [DEBUG] {datetime.now().strftime('%H:%M:%S')}: {text}"
        )
    except Exception as e:
        logger.error(f"Ошибка отладки: {e}")

async def delete_message_safe(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Удаление сообщения с уведомлением в группу"""
    try:
        await context.bot.delete_message(chat_id, message_id)
        await debug_message(context, chat_id, f"Сообщение {message_id} удалено")
    except Exception as e:
        error_text = f"Ошибка удаления {message_id}: {str(e)}"
        await debug_message(context, chat_id, error_text)
        logger.error(error_text)

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых участников"""
    chat_id = update.effective_chat.id
    try:
        await debug_message(context, chat_id, "Обнаружен новый участник")
        
        # Проверка прав бота
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not (bot_member.can_restrict_members and bot_member.can_delete_messages):
            await debug_message(context, chat_id, "❌ Бот не имеет прав на удаление/бан!")
            return

        # Удаление системного сообщения
        await update.message.delete()
        await debug_message(context, chat_id, "Системное сообщение удалено")

        for user in update.message.new_chat_members:
            if user.is_bot:
                continue

            user_id = user.id
            await debug_message(context, chat_id, f"Начата обработка @{user.username} (ID: {user_id})")

            # Отправка инструкции
            instructions = await context.bot.send_message(
                chat_id,
                f"👋 {user.mention_markdown()}, у вас 60 секунд для ввода кода!",
                parse_mode="Markdown"
            )
            
            # Задание на удаление инструкции
            context.job_queue.run_once(
                partial(delete_message_safe, chat_id, instructions.message_id),
                15,
                name=f"del_instr_{user_id}"
            )

            # Регистрация задания на кик
            job = context.job_queue.run_once(
                partial(kick_user_callback, user_id=user_id),
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
            await debug_message(context, chat_id, f"Запланирован кик @{user.username} в {datetime.now() + timedelta(seconds=60)}")

    except Exception as e:
        error_text = f"🚨 ОШИБКА: {str(e)}"
        await debug_message(context, chat_id, error_text)
        logger.error(error_text)

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    try:
        if user_id not in pending_verification:
            await debug_message(context, chat_id, f"Сообщение от @{update.effective_user.username} (не в очереди)")
            return

        user_data = pending_verification[user_id]
        user_data["attempts"] += 1
        await debug_message(context, chat_id, f"Попытка {user_data['attempts']} от @{update.effective_user.username}")

        # Удаление сообщения
        await delete_message_safe(chat_id, update.message.message_id, context)
        user_data["messages"].append(update.message.message_id)

        # Проверка пароля
        if update.message.text and update.message.text.strip() == PASSWORD:
            await debug_message(context, chat_id, f"✅ @{update.effective_user.username} ввел верный код!")
            user_data["job"].schedule_removal()
            await delete_message_safe(chat_id, user_data["instructions_msg"], context)
            
            confirmation = await context.bot.send_message(
                chat_id,
                f"{update.effective_user.mention_markdown()} верифицирован!",
                parse_mode="Markdown"
            )
            
            del pending_verification[user_id]
            return

        # Превышение попыток
        if user_data["attempts"] >= MAX_ATTEMPTS:
            await debug_message(context, chat_id, f"🔥 @{update.effective_user.username} исчерпал попытки!")
            await execute_kick(user_id, context, "неверный код")
            return

        # Уведомление об ошибке
        warning = await context.bot.send_message(
            chat_id,
            f"❌ Неверно! Осталось попыток: {MAX_ATTEMPTS - user_data['attempts']}"
        )
        context.job_queue.run_once(
            partial(delete_message_safe, chat_id, warning.message_id),
            5,
            name=f"del_warn_{user_id}"
        )

    except Exception as e:
        error_text = f"🚨 ОШИБКА: {str(e)}"
        await debug_message(context, chat_id, error_text)
        logger.error(error_text)

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Обработка таймаута"""
    job = context.job
    chat_id, user_id, instr_msg = job.data
    await debug_message(context, chat_id, f"⏰ Таймаут верификации для пользователя {user_id}")
    await execute_kick(user_id, context, "таймаут")

async def execute_kick(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    """Кик пользователя"""
    try:
        user_data = pending_verification.pop(user_id, None)
        if not user_data:
            await debug_message(context, "unknown", f"⚠️ Пользователь {user_id} уже удален")
            return

        chat_id = user_data["chat_id"]
        await debug_message(context, chat_id, f"🛑 Начало процедуры кика ({reason})...")

        # Проверка статуса пользователя
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ["creator", "administrator"]:
                await debug_message(context, chat_id, "⛔ Невозможно удалить администратора!")
                return
        except Exception as e:
            await debug_message(context, chat_id, f"⚠️ Ошибка проверки прав: {str(e)}")

        # Удаление сообщений
        await debug_message(context, chat_id, f"🧹 Удаление {len(user_data['messages']} сообщений...")
        for msg_id in user_data["messages"]:
            await delete_message_safe(chat_id, msg_id, context)

        # Удаление инструкции
        await delete_message_safe(chat_id, user_data["instructions_msg"], context)

        # Бан
        try:
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=datetime.now() + timedelta(seconds=30)
            )
            await debug_message(context, chat_id, f"🔨 Пользователь забанен на 30 сек")
        except RetryAfter as e:
            await debug_message(context, chat_id, f"⏳ Ожидаем {e.retry_after} сек...")
            await asyncio.sleep(e.retry_after)
            await execute_kick(user_id, context, reason)
            return
        except Exception as e:
            await debug_message(context, chat_id, f"🚨 Ошибка бана: {str(e)}")
            return

        # Уведомление
        notification = await context.bot.send_message(
            chat_id,
            f"🚫 Пользователь удалён ({reason})"
        )
        context.job_queue.run_once(
            partial(delete_message_safe, chat_id, notification.message_id),
            10,
            name=f"del_notif_{user_id}"
        )

    except Exception as e:
        error_text = f"💥 КРИТИЧЕСКАЯ ОШИБКА: {str(e)}"
        await debug_message(context, chat_id, error_text)
        logger.error(error_text)

async def check_rights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка прав бота"""
    chat_id = update.effective_chat.id
    bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    await update.message.reply_text(
        f"🔐 Права бота:\n"
        f"• Удалять сообщения: {'✅' if bot_member.can_delete_messages else '❌'}\n"
        f"• Банить пользователей: {'✅' if bot_member.can_restrict_members else '❌'}"
    )

def main():
    application = Application.builder().token("YOUR_BOT_TOKEN").build()
    application.add_handler(CommandHandler("check_rights", check_rights))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    application.run_polling()

if __name__ == "__main__":
    main()
