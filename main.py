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

PASSWORD = "SECRET123"
pending_verification = {}

async def delete_user_message(update: Update):
    """Удаляет сообщение пользователя и логирует действие"""
    try:
        await update.message.delete()
        logger.info(f"Deleted message from {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Failed to delete message: {e}")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик новых участников группы"""
    try:
        # Удаляем системное сообщение о входе
        await update.message.delete()
        
        for user in update.message.new_chat_members:
            if user.is_bot:
                continue
            
            user_id = user.id
            chat_id = update.effective_chat.id
            
            # Отправляем инструкцию в ЛС
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"Отправьте кодовое слово в группу в течение 60 секунд."
                )
            except Exception as e:
                logger.error(f"Не удалось отправить ЛС: {e}")
                # Альтернативное временное сообщение
                msg = await update.effective_chat.send_message(
                    f"🔒 {user.mention_markdown()}, проверьте ЛС для инструкций!"
                )
                context.job_queue.run_once(
                    lambda ctx: msg.delete(), 
                    10, 
                    data=msg.id
                )

            # Регистрируем пользователя для проверки
            job = context.job_queue.run_once(
                kick_user_callback, 
                60, 
                data=(chat_id, user_id),
                name=f"kick_job_{user_id}"
            )
            
            pending_verification[user_id] = {
                "job": job,
                "chat_id": chat_id,
                "messages": []
            }

    except Exception as e:
        logger.error(f"Error in welcome_new_member: {e}")

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех сообщений"""
    user_id = update.effective_user.id
    if user_id not in pending_verification:
        return
    
    try:
        # Удаляем сообщение сразу
        await delete_user_message(update)
        
        # Сохраняем ID сообщения для последующей проверки
        pending_verification[user_id]["messages"].append(update.message.id)
        
        # Проверяем только текстовые сообщения
        if update.message.text:
            text = update.message.text.strip()
            if text == PASSWORD:
                # Успешная проверка
                job = pending_verification[user_id]["job"]
                job.schedule_removal()
                
                # Удаляем все предыдущие сообщения
                chat_id = pending_verification[user_id]["chat_id"]
                for msg_id in pending_verification[user_id]["messages"]:
                    try:
                        await context.bot.delete_message(chat_id, msg_id)
                    except Exception as e:
                        logger.error(f"Failed to delete message {msg_id}: {e}")
                
                # Отправляем и удаляем подтверждение
                msg = await update.effective_chat.send_message(
                    f"✅ {update.effective_user.mention_markdown()} верифицирован!"
                )
                context.job_queue.run_once(
                    lambda ctx: msg.delete(),
                    10,
                    data=msg.id
                )
                
                del pending_verification[user_id]
                
    except Exception as e:
        logger.error(f"Error in handle_all_messages: {e}")

async def kick_user_callback(context: ContextTypes.DEFAULT_TYPE):
    """Коллбэк для удаления неподтвержденных пользователей"""
    try:
        job = context.job
        chat_id, user_id = job.data
        
        if user_id not in pending_verification:
            return
        
        # Удаляем все сообщения пользователя
        for msg_id in pending_verification[user_id]["messages"]:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception as e:
                logger.error(f"Failed to delete message {msg_id}: {e}")
        
        # Кикаем пользователя
        await context.bot.ban_chat_member(  # Исправлено: добавлена закрывающая скобка
            chat_id=chat_id,
            user_id=user_id,
            until_date=datetime.now() + timedelta(seconds=30)
        )  # Закрывающая скобка здесь
        
        # Опционально: разбанить, если нужно сразу разрешить вернуться
        # await context.bot.unban_chat_member(chat_id, user_id)
        
        # Отправляем и удаляем уведомление
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 Пользователь не прошел проверку!"
        )
        context.job_queue.run_once(
            lambda ctx: msg.delete(),
            10,
            data=msg.id
        )
        
    except Exception as e:
        logger.error(f"Error in kick_user_callback: {e}")
    finally:
        if user_id in pending_verification:
            del pending_verification[user_id]
def main():
    application = Application.builder().token("7931308034:AAGoN08BoCi4eQl7fI-KFbgIvMYRwsVITAE").build()
    
    # Обработчики
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_messages)
    )
    
    application.run_polling()

if __name__ == "__main__":
    main()
