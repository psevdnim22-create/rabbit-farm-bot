import os
import logging
import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -------------------- Logging --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------- Token from ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    # Crash early with a clear message if the token is missing
    raise RuntimeError("BOT_TOKEN environment variable is not set")


# -------------------- Command Handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Hello! 🐇\n"
        "I'm your Rabbit bot.\n\n"
        "Commands:\n"
        "/start - show this message\n"
        "/help - list commands\n"
        "/subscribe - get daily messages\n"
        "/unsubscribe - stop daily messages"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "Here are the commands you can use:\n"
        "/start - start the bot\n"
        "/help - show this help\n"
        "/subscribe - get daily messages\n"
        "/unsubscribe - stop daily messages"
    )


# -------------------- Job Callback --------------------
async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """This function is called by the JobQueue for subscribed users."""
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🐇 Daily rabbit message! (you can customize this text)"
        )
    except Exception as e:
        logger.error("Error sending daily job message: %s", e)


# -------------------- Subscribe / Unsubscribe --------------------
async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe the user to a daily message."""
    chat_id = update.effective_chat.id
    job_queue = context.job_queue

    if job_queue is None:
        await update.message.reply_text(
            "Job system is not available on this server, "
            "so I can't create subscriptions right now."
        )
        return

    job_name = f"daily_subscription_{chat_id}"

    # Remove existing jobs with the same name
    existing_jobs = job_queue.get_jobs_by_name(job_name)
    for job in existing_jobs:
        job.schedule_removal()

    # Time of day when the daily message will be sent (server time)
    run_time = datetime.time(hour=9, minute=0, second=0)  # 09:00

    job_queue.run_daily(
        callback=daily_job,
        time=run_time,
        name=job_name,
        chat_id=chat_id,
    )

    await update.message.reply_text(
        "✅ You are subscribed to daily messages at 09:00. "
        "Use /unsubscribe to stop."
    )


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe the user from the daily message."""
    chat_id = update.effective_chat.id
    job_queue = context.job_queue

    if job_queue is None:
        await update.message.reply_text(
            "Job system is not available on this server, "
            "so I can't manage subscriptions right now."
        )
        return

    job_name = f"daily_subscription_{chat_id}"
    jobs = job_queue.get_jobs_by_name(job_name)

    if not jobs:
        await update.message.reply_text("You are not subscribed to anything.")
        return

    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text("❌ You have been unsubscribed from daily messages.")


# -------------------- Error Handler --------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a friendly message."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An error occurred while processing your request. Please try again."
            )
        except Exception:
            pass


# -------------------- Main --------------------
def main() -> None:
    """Run the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("subscribe", subscribe_cmd))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))

    # Error handler
    application.add_error_handler(error_handler)

    # Start the bot (long polling)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
