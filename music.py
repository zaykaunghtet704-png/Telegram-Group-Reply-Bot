"""Telegram group voice-chat music player.

Requires a separate Telegram user session. Secrets are read only from environment
variables and must never be committed to the repository.
"""
import asyncio
import logging
import os
import shutil
import tempfile
from collections import defaultdict, deque
from pathlib import Path

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

log = logging.getLogger(__name__)

_API_ID = os.getenv("MUSIC_API_ID", "").strip()
_API_HASH = os.getenv("MUSIC_API_HASH", "").strip()
_SESSION = os.getenv("MUSIC_SESSION_STRING", "").strip()

_client = None
_calls = None
_queues = defaultdict(deque)
_current = {}
_locks = defaultdict(asyncio.Lock)
_temp_files = defaultdict(list)


def music_ready():
    return bool(_API_ID and _API_HASH and _SESSION)


async def start_music():
    """Start the assistant user account and its group-call client."""
    global _client, _calls
    if not music_ready():
        log.warning("Music disabled: MUSIC_API_ID / MUSIC_API_HASH / MUSIC_SESSION_STRING missing")
        return False
    try:
        from pyrogram import Client
        from pytgcalls import PyTgCalls
        _client = Client(
            "music_assistant",
            api_id=int(_API_ID),
            api_hash=_API_HASH,
            session_string=_SESSION,
            in_memory=True,
        )
        await _client.start()
        _calls = PyTgCalls(_client)
        from pytgcalls import filters as call_filters
        @_calls.on_update(call_filters.stream_end())
        async def _stream_finished(_, update):
            chat_id = update.chat_id
            try:
                async with _locks[chat_id]:
                    finished = _current.pop(chat_id, None)
                    _cleanup_track(finished)
                    track = await _play_next(chat_id)
                if track:
                    await _client.send_message(chat_id, f"🎶 နောက်သီချင်း: {track['title']}")
            except Exception:
                log.exception("Could not advance music queue for %s", chat_id)
        await _calls.start()
        log.info("Music Assistant connected")
        return True
    except Exception:
        log.exception("Music Assistant failed to start")
        _client = None
        _calls = None
        return False


async def stop_music():
    global _client, _calls
    try:
        if _calls:
            await _calls.stop()
    except Exception:
        log.exception("Error stopping PyTgCalls")
    try:
        if _client:
            await _client.stop()
    except Exception:
        log.exception("Error stopping Music Assistant")
    _client = None
    _calls = None


async def _search_download(query):
    """Search YouTube and download one audio track; return metadata and path."""
    import yt_dlp

    folder = tempfile.mkdtemp(prefix="myae_music_")
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "max_filesize": 80 * 1024 * 1024,
        "match_filter": yt_dlp.utils.match_filter_func("duration <= 1800"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "128",
        }],
    }

    def work():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(query, download=True)
            if "entries" in info:
                info = next((x for x in info["entries"] if x), None)
            if not info:
                raise ValueError("သီချင်းမတွေ့ပါ။")
            path = ydl.prepare_filename(info)
            path = str(Path(path).with_suffix(".opus"))
            if not os.path.exists(path):
                candidates = list(Path(folder).glob("*"))
                path = str(candidates[0]) if candidates else path
            if not os.path.exists(path):
                raise FileNotFoundError("Downloaded audio file is missing")
            return {
                "title": info.get("title") or "Unknown",
                "url": info.get("webpage_url") or "",
                "duration": int(info.get("duration") or 0),
                "path": path,
                "folder": folder,
            }

    try:
        return await asyncio.to_thread(work)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _cleanup_track(track):
    if track and track.get("folder"):
        shutil.rmtree(track["folder"], ignore_errors=True)


async def _play_next(chat_id):
    if not _calls:
        raise RuntimeError("Music Assistant မချိတ်ဆက်ရသေးပါ။ Render Environment Variables စစ်ပါ။")
    queue = _queues[chat_id]
    if not queue:
        _current.pop(chat_id, None)
        return None
    track = queue.popleft()
    from pytgcalls.types import MediaStream
    await _calls.play(chat_id, MediaStream(track["path"]))
    _current[chat_id] = track
    _temp_files[chat_id].append(track["folder"])
    return track


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await msg.reply_text("🎵 ဒီ Command ကို Group ထဲမှာ အသုံးပြုပါ။")
        return
    if not _calls:
        await msg.reply_text(
            "🎧 Music Assistant မချိတ်ဆက်ရသေးပါ။\n"
            "Owner က Render မှာ MUSIC_API_ID, MUSIC_API_HASH, MUSIC_SESSION_STRING ထည့်ပေးပါ။"
        )
        return
    query = " ".join(context.args).strip()
    if not query:
        await msg.reply_text("အသုံးပြုပုံ: /play သီချင်းအမည်")
        return
    status = await msg.reply_text("🔎 YouTube မှာ သီချင်းရှာနေပါတယ်...")
    track = None
    try:
        track = await _search_download(query)
        async with _locks[chat.id]:
            if chat.id in _current:
                _queues[chat.id].append(track)
                position = len(_queues[chat.id])
                await status.edit_text(
                    f"🎶 Queue ထဲထည့်ပြီးပါပြီ\n"
                    f"🎵 {track['title']}\n📌 နေရာ: {position}"
                )
            else:
                from pytgcalls.types import MediaStream
                await _calls.play(chat.id, MediaStream(track["path"]))
                _current[chat.id] = track
                _temp_files[chat.id].append(track["folder"])
                await status.edit_text(f"▶️ ဖွင့်နေပါပြီ\n🎵 {track['title']}")
    except Exception as exc:
        _cleanup_track(track)
        log.exception("Music play failed")
        await status.edit_text(f"❌ သီချင်းဖွင့်မရပါ။ Voice Chat ဖွင့်ထားပြီး Assistant ကို Group ထဲထည့်ထားပါ။\n{str(exc)[:250]}")


async def pause_command(update, context):
    if not _calls:
        await update.effective_message.reply_text("Music Assistant မချိတ်ဆက်ရသေးပါ။")
        return
    try:
        await _calls.pause(update.effective_chat.id)
        await update.effective_message.reply_text("⏸️ သီချင်းခဏရပ်ထားပါပြီ။")
    except Exception as exc:
        await update.effective_message.reply_text(f"ခဏရပ်မရပါ: {str(exc)[:180]}")


async def resume_command(update, context):
    if not _calls:
        await update.effective_message.reply_text("Music Assistant မချိတ်ဆက်ရသေးပါ။")
        return
    try:
        await _calls.resume(update.effective_chat.id)
        await update.effective_message.reply_text("▶️ ပြန်ဖွင့်နေပါပြီ။")
    except Exception as exc:
        await update.effective_message.reply_text(f"ပြန်ဖွင့်မရပါ: {str(exc)[:180]}")


async def skip_command(update, context):
    chat_id = update.effective_chat.id
    if not _calls:
        await update.effective_message.reply_text("Music Assistant မချိတ်ဆက်ရသေးပါ။")
        return
    try:
        async with _locks[chat_id]:
            await _calls.leave_call(chat_id)
            old = _current.pop(chat_id, None)
            _cleanup_track(old)
            track = await _play_next(chat_id)
        await update.effective_message.reply_text(
            f"⏭️ နောက်သီချင်း: {track['title']}" if track else "Queue ကုန်သွားပါပြီ။"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Skip မလုပ်နိုင်ပါ: {str(exc)[:180]}")


async def stop_command(update, context):
    chat_id = update.effective_chat.id
    try:
        if _calls:
            await _calls.leave_call(chat_id)
        old = _current.pop(chat_id, None)
        _cleanup_track(old)
        for queued in list(_queues[chat_id]):
            _cleanup_track(queued)
        _queues[chat_id].clear()
        await update.effective_message.reply_text("⏹️ Music ရပ်ပြီး Queue ရှင်းလိုက်ပါပြီ။")
    except Exception as exc:
        await update.effective_message.reply_text(f"ရပ်မရပါ: {str(exc)[:180]}")


async def queue_command(update, context):
    chat_id = update.effective_chat.id
    current = _current.get(chat_id)
    waiting = list(_queues[chat_id])
    lines = ["🎧 **Music Queue**"]
    if current:
        lines.append(f"▶️ Now: {current['title']}")
    else:
        lines.append("▶️ လက်ရှိဖွင့်နေသော သီချင်းမရှိပါ။")
    lines.extend(f"{i}. {item['title']}" for i, item in enumerate(waiting, 1))
    await update.effective_message.reply_text("\n".join(lines))


async def volume_command(update, context):
    if not _calls:
        await update.effective_message.reply_text("Music Assistant မချိတ်ဆက်ရသေးပါ။")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /volume 1-200")
        return
    volume = int(context.args[0])
    if not 1 <= volume <= 200:
        await update.effective_message.reply_text("Volume ကို 1 မှ 200 အတွင်း သတ်မှတ်ပါ။")
        return
    try:
        await _calls.change_volume_call(update.effective_chat.id, volume)
        await update.effective_message.reply_text(f"🔊 Volume: {volume}%")
    except Exception as exc:
        await update.effective_message.reply_text(f"Volume ပြောင်းမရပါ: {str(exc)[:180]}")


async def song_command(update, context):
    track = _current.get(update.effective_chat.id)
    if not track:
        await update.effective_message.reply_text("လက်ရှိ သီချင်းဖွင့်မထားပါ။")
        return
    await update.effective_message.reply_text(
        f"🎵 {track['title']}\n"
        f"⏱️ {track['duration']} sec\n"
        f"🔗 {track['url']}"
    )


def music_handlers():
    return [
        CommandHandler("play", play_command),
        CommandHandler("pause", pause_command),
        CommandHandler("resume", resume_command),
        CommandHandler("skip", skip_command),
        CommandHandler("stop", stop_command),
        CommandHandler("queue", queue_command),
        CommandHandler("song", song_command),
        CommandHandler("volume", volume_command),
    ]
