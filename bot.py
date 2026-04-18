import os
import asyncio
import aiohttp
import logging
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BACKEND_URL = os.environ.get("BACKEND_URL", "https://youtube-production-a411.up.railway.app")

# YouTube URL regex
YT_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/)?[^\s&]+")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *YT Clipper Bot*\n\n"
        "YouTube video ka URL bhejo — main uski *Top 10 best clips* bana ke bhejunga!\n\n"
        "⏱ Processing time: ~5-10 min per video\n"
        "📏 Max video: 30 min recommended",
        parse_mode=ParseMode.MARKDOWN
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands:*\n\n"
        "/start — Bot shuru karo\n"
        "/help — Yeh message\n\n"
        "Bas koi bhi YouTube URL bhejo aur main clips bana dunga! 🚀",
        parse_mode=ParseMode.MARKDOWN
    )


async def poll_job(job_id: str) -> dict:
    """Backend se job status check karo"""
    async with aiohttp.ClientSession() as session:
        for _ in range(120):  # max 10 min (120 * 5s)
            try:
                async with session.get(
                    f"{BACKEND_URL}/status/{job_id}", timeout=aiohttp.ClientTimeout(total=10)
                ) as res:
                    data = await res.json()
                    status = data.get("status")
                    if status in ("done", "error"):
                        return data
            except Exception as e:
                logger.warning(f"Poll error: {e}")
            await asyncio.sleep(5)
    return {"status": "error", "error": "Timeout — 10 minute mein response nahi aaya"}


async def send_clip(bot: Bot, chat_id: int, clip: dict, idx: int, total: int):
    """Cloudinary se clip download karo aur Telegram pe bhejo"""
    clip_url = clip.get("url", "")
    reason = clip.get("reason", f"Clip {clip.get('index', idx+1)}")
    start = clip.get("start", 0)
    m, s = divmod(int(start), 60)

    caption = (
        f"🎬 *Clip {clip.get('index', idx+1)}/{total}*\n"
        f"⏱ {m}:{s:02d} se shuru\n"
        f"📝 {reason[:100]}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(clip_url, timeout=aiohttp.ClientTimeout(total=120)) as res:
                if res.status != 200:
                    raise Exception(f"Download failed: {res.status}")
                video_data = await res.read()

        # 50MB limit check
        if len(video_data) > 49 * 1024 * 1024:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Clip {clip.get('index')} badi hai (>50MB) — link:\n{clip_url}",
            )
            return

        await bot.send_video(
            chat_id=chat_id,
            video=video_data,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            supports_streaming=True,
            read_timeout=120,
            write_timeout=120,
        )

    except Exception as e:
        logger.error(f"Clip {idx+1} send error: {e}")
        # Fallback — link bhejo
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Clip {clip.get('index')} send nahi ho payi — link:\n{clip_url}",
        )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id
    bot = context.bot

    if not YT_REGEX.search(url):
        await update.message.reply_text(
            "❌ Yeh valid YouTube URL nahi hai!\n\n"
            "Aisa kuch bhejo:\n`https://youtube.com/watch?v=xxxxx`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Processing shuru karo
    status_msg = await update.message.reply_text(
        "⏳ *Processing shuru ho gaya!*\n\n"
        "🔄 Video download ho rahi hai...\n"
        "⏱ ~5-10 min lagenge, wait karo bhai!",
        parse_mode=ParseMode.MARKDOWN
    )

    # Backend ko job submit karo
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BACKEND_URL}/clip",
                json={"url": url},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as res:
                if not res.ok:
                    raise Exception(f"Backend error: {res.status}")
                data = await res.json()
                job_id = data.get("job_id")
    except Exception as e:
        await status_msg.edit_text(f"❌ Backend se connect nahi ho pa raha:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    # Progress updates bhejta raho
    last_status = ""
    status_map = {
        "queued":       "⏳ Queue mein hai...",
        "downloading":  "⬇️ Video download ho rahi hai...",
        "transcribing": "🎙️ Transcript ban raha hai...",
        "analyzing":    "🤖 AI best moments dhundh raha hai...",
        "cutting":      "✂️ Clips cut ho rahi hain...",
        "uploading":    "☁️ Clips upload ho rahi hain...",
    }

    async def update_progress():
        nonlocal last_status
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{BACKEND_URL}/status/{job_id}",
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as res:
                        d = await res.json()
                        st = d.get("status", "")
                        prog = d.get("progress", 0)

                        if st in ("done", "error"):
                            return d

                        label = status_map.get(st, f"🔄 {st}...")
                        new_text = (
                            f"⚙️ *Processing...*\n\n"
                            f"{label}\n"
                            f"📊 Progress: {prog}%"
                        )
                        if new_text != last_status:
                            try:
                                await status_msg.edit_text(new_text, parse_mode=ParseMode.MARKDOWN)
                                last_status = new_text
                            except Exception:
                                pass
            except Exception:
                pass
            await asyncio.sleep(5)

    # Job complete hone ka wait karo
    result = await poll_job(job_id)

    if result.get("status") == "error":
        err = result.get("error", "Kuch gadbad ho gayi")
        await status_msg.edit_text(
            f"❌ *Error aaya bhai!*\n\n`{err}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    clips = result.get("clips", [])
    if not clips:
        await status_msg.edit_text("❌ Koi clip nahi bani — video check karo")
        return

    # Success message
    await status_msg.edit_text(
        f"✅ *{len(clips)} Clips ready hain!*\n\n"
        f"📤 Ab ek ek karke bhej raha hoon...",
        parse_mode=ParseMode.MARKDOWN
    )

    # Clips ek ek bhejo
    for i, clip in enumerate(clips):
        await bot.send_chat_action(chat_id=chat_id, action="upload_video")
        await send_clip(bot, chat_id, clip, i, len(clips))
        await asyncio.sleep(1)  # rate limit avoid

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎉 *Sab clips bhej diye!* ({len(clips)} clips)\n\nAur video chahiye? URL bhejo! 🚀",
        parse_mode=ParseMode.MARKDOWN
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("🤖 Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
