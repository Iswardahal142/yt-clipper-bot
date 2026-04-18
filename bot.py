import os
import asyncio
import aiohttp
import logging
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
import re
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BACKEND_URL = os.environ.get("BACKEND_URL", "https://youtube-production-a411.up.railway.app")

YT_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/)?[^\s&]+")

STATUS_MAP = {
    "queued":       "⏳ Queue mein hai",
    "downloading":  "⬇️ Video download ho rahi hai",
    "transcribing": "🎙️ Transcript ban raha hai",
    "analyzing":    "🤖 AI best moments dhundh raha hai",
    "cutting":      "✂️ Clips cut ho rahi hain",
    "uploading":    "☁️ Clips upload ho rahi hain",
}

# User ka pending URL store karo jab tak settings select na ho
pending_urls = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *YT Clipper Bot*\n\nYouTube video ka URL bhejo — main uski *Top 10 best clips* bana ke bhejunga!\n\n⏱ Processing time: ~5-10 min per video",
        parse_mode=ParseMode.MARKDOWN
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands:*\n\n/start — Bot shuru karo\n/help — Yeh message\n\nBas koi bhi YouTube URL bhejo! 🚀",
        parse_mode=ParseMode.MARKDOWN
    )


def duration_keyboard():
    """Clip length select karne ke liye buttons"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ 30 sec", callback_data="dur_30"),
            InlineKeyboardButton("🎬 60 sec", callback_data="dur_60"),
            InlineKeyboardButton("🎥 90 sec", callback_data="dur_90"),
        ]
    ])


def format_keyboard(duration: int):
    """Format select karne ke liye buttons"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 Portrait (Reels/Shorts)", callback_data=f"fmt_portrait_{duration}"),
            InlineKeyboardButton("🖥 Landscape (YouTube)", callback_data=f"fmt_landscape_{duration}"),
        ]
    ])


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not YT_REGEX.search(url):
        await update.message.reply_text(
            "❌ Yeh valid YouTube URL nahi hai!\n\nAisa bhejo:\n`https://youtube.com/watch?v=xxxxx`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # URL save karo — settings select hone tak
    pending_urls[chat_id] = url

    await update.message.reply_text(
        "⏱ *Clip length choose karo:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=duration_keyboard()
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    # Duration select hua
    if data.startswith("dur_"):
        duration = int(data.split("_")[1])
        await query.edit_message_text(
            f"✅ *{duration} sec* select hua!\n\n📐 *Format choose karo:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=format_keyboard(duration)
        )

    # Format select hua — ab processing shuru
    elif data.startswith("fmt_"):
        parts = data.split("_")
        fmt = parts[1]         # portrait / landscape
        duration = int(parts[2])
        url = pending_urls.pop(chat_id, None)

        if not url:
            await query.edit_message_text("❌ URL expire ho gaya — dobara bhejo")
            return

        fmt_label = "📱 Portrait" if fmt == "portrait" else "🖥 Landscape"
        await query.edit_message_text(
            f"✅ Settings confirm:\n"
            f"⏱ *{duration} sec* | {fmt_label}\n\n"
            f"⏳ Processing shuru ho raha hai...",
            parse_mode=ParseMode.MARKDOWN
        )

        # Naya status message
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔄 Backend ko request bhej raha hoon...",
        )

        # Backend pe job submit karo
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{BACKEND_URL}/clip",
                    json={"url": url, "clip_duration": duration, "fmt": fmt},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as res:
                    if not res.ok:
                        raise Exception(f"Backend error: {res.status}")
                    resp_data = await res.json()
                    job_id = resp_data.get("job_id")
        except Exception as e:
            await status_msg.edit_text(f"❌ Backend se connect nahi ho pa raha\n`{e}`", parse_mode=ParseMode.MARKDOWN)
            return

        last_text = ""
        result = {}

        # Live progress — same message edit hota rahega
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
                            result = d
                            break

                        filled = int(prog / 10)
                        bar = "█" * filled + "░" * (10 - filled)
                        label = STATUS_MAP.get(st, f"🔄 {st}")
                        new_text = f"⚙️ *Processing...*\n\n{label}...\n\n`[{bar}]` {prog}%"

                        if new_text != last_text:
                            try:
                                await status_msg.edit_text(new_text, parse_mode=ParseMode.MARKDOWN)
                                last_text = new_text
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"Poll error: {e}")

            await asyncio.sleep(4)

        # Error?
        if result.get("status") == "error":
            err = result.get("error", "Kuch gadbad ho gayi")
            await status_msg.edit_text(f"❌ *Error aaya bhai!*\n\n`{err}`", parse_mode=ParseMode.MARKDOWN)
            return

        clips = result.get("clips", [])
        if not clips:
            await status_msg.edit_text("❌ Koi clip nahi bani — video check karo")
            return

        await status_msg.edit_text(
            f"✅ *{len(clips)} Clips ready hain!*\n\n📤 Ab ek ek karke bhej raha hoon...",
            parse_mode=ParseMode.MARKDOWN
        )

        # Clips bhejo
        for i, clip in enumerate(clips):
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
            await send_clip(context.bot, chat_id, clip, i, len(clips))
            await asyncio.sleep(1)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 *Sab clips bhej diye!* ({len(clips)} clips)\n\nAur video chahiye? URL bhejo! 🚀",
            parse_mode=ParseMode.MARKDOWN
        )


async def send_clip(bot: Bot, chat_id: int, clip: dict, idx: int, total: int):
    clip_url = clip.get("url", "")
    reason = clip.get("reason", f"Clip {clip.get('index', idx+1)}")
    start = clip.get("start", 0)
    m, s = divmod(int(start), 60)
    caption = f"🎬 *Clip {clip.get('index', idx+1)}/{total}*\n⏱ {m}:{s:02d} se shuru\n📝 {reason[:100]}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(clip_url, timeout=aiohttp.ClientTimeout(total=120)) as res:
                if res.status != 200:
                    raise Exception(f"Download failed: {res.status}")
                video_data = await res.read()

        if len(video_data) > 49 * 1024 * 1024:
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Clip {clip.get('index')} badi hai (>50MB)\nLink: {clip_url}")
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
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Clip {clip.get('index')} send nahi hui\nLink: {clip_url}")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("🤖 Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
