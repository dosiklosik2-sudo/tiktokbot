import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import yt_dlp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import admin
import db
import limits

TOKEN = os.environ["BOT_TOKEN"]
MAX_SIZE = 50 * 1024 * 1024  # лимит Bot API на отправку файла

URL_RE = re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/\S+")

# 1) H.264 без водяного знака  2) любой без водяного знака  3) что угодно
FORMAT = (
    "b[vcodec^=avc1][format_note!*=?watermarked]"
    "/b[format_note!*=?watermarked]"
    "/b"
)

semaphore = asyncio.Semaphore(3)
user_router = Router()


def download(url: str, out_dir: str, audio: bool = False) -> tuple[Path, dict]:
    opts = {
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "format": FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
    }
    if audio:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    cookies = os.getenv("COOKIES_FILE")
    if cookies:
        opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if audio:
        path = next(Path(out_dir).glob("*.mp3"))
    return path, info


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя и отсекает забаненных."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            db.touch_user(user.id, user.username, user.first_name)
            if user.id not in admin.ADMIN_IDS and db.is_banned(user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ закрыт.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("Доступ закрыт.")
                return
        return await handler(event, data)


@user_router.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! Пришли ссылку на TikTok, пришлю видео без водяного знака.")


@user_router.message(Command("limits"))
async def show_limits(message: Message):
    await message.answer(limits.status_text(message.from_user.id))


@user_router.message()
async def handle_link(message: Message):
    match = URL_RE.search(message.text or "")
    if not match:
        await message.answer("Пришли ссылку на видео из TikTok 🙂")
        return

    url = match.group(0)
    uid = message.from_user.id
    if reason := limits.check(uid):
        await message.answer(reason)
        return

    limits.start(uid)
    status = await message.answer("⏳ Скачиваю...")
    tmp = tempfile.mkdtemp(prefix="tt_")
    try:
        async with semaphore:
            path, info = await asyncio.to_thread(download, url, tmp)

        if path.stat().st_size > MAX_SIZE:
            db.log_download(uid, url, "video", False)
            await status.edit_text("Видео больше 50 МБ, обычный бот не может его отправить.")
            return

        dl_id = db.log_download(uid, url, "video", True)
        author = info.get("uploader") or info.get("creator") or ""
        caption = f"@{author}" if author else None
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎵 Скачать звук (MP3)", callback_data=f"aud:{dl_id}")]
            ]
        )
        await message.answer_video(
            FSInputFile(path),
            caption=caption,
            width=info.get("width"),
            height=info.get("height"),
            duration=int(info["duration"]) if info.get("duration") else None,
            supports_streaming=True,
            reply_markup=kb,
        )
        await status.delete()
    except yt_dlp.utils.DownloadError as e:
        logging.warning("download error: %s", e)
        db.log_download(uid, url, "video", False)
        await status.edit_text("Не вышло скачать: видео удалено, приватное или это фото-карусель.")
    except Exception:
        logging.exception("unexpected error")
        db.log_download(uid, url, "video", False)
        await status.edit_text("Что-то пошло не так 😕")
    finally:
        limits.finish(uid)
        shutil.rmtree(tmp, ignore_errors=True)


@user_router.callback_query(F.data.startswith("aud:"))
async def handle_audio(cb: CallbackQuery):
    if not shutil.which("ffmpeg"):
        await cb.answer("На сервере не установлен ffmpeg.", show_alert=True)
        return

    url = db.get_download_url(int(cb.data.split(":")[1]))
    if not url:
        await cb.answer("Ссылка не найдена, пришли видео заново.", show_alert=True)
        return

    uid = cb.from_user.id
    if limits.is_busy(uid):
        await cb.answer("Подожди, предыдущая загрузка ещё идёт.", show_alert=True)
        return

    await cb.answer("Готовлю звук...")
    limits.active.add(uid)
    tmp = tempfile.mkdtemp(prefix="tt_a_")
    try:
        async with semaphore:
            path, info = await asyncio.to_thread(download, url, tmp, True)

        if path.stat().st_size > MAX_SIZE:
            db.log_download(uid, url, "audio", False)
            await cb.message.answer("Аудио больше 50 МБ, не могу отправить.")
            return

        db.log_download(uid, url, "audio", True)
        await cb.message.answer_audio(
            FSInputFile(path),
            title=info.get("track") or info.get("title") or "TikTok",
            performer=info.get("artist") or info.get("uploader"),
            duration=int(info["duration"]) if info.get("duration") else None,
        )
    except Exception:
        logging.exception("audio error")
        db.log_download(uid, url, "audio", False)
        await cb.message.answer("Не удалось вытащить звук 😕")
    finally:
        limits.finish(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not admin.ADMIN_IDS:
        logging.warning("ADMIN_IDS не задан: админ-панель недоступна")
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg не найден: кнопка MP3 работать не будет")

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    # порядок важен: админский роутер раньше общего «ловца всех сообщений»
    dp.include_router(admin.router)
    dp.include_router(user_router)

    # Прокси нужен только на домашнем ПК с VPN. На Railway переменную PROXY_URL не задавай.
    proxy = os.getenv("PROXY_URL")
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession  # нужен pip install aiohttp-socks

        bot = Bot(TOKEN, session=AiohttpSession(proxy=proxy))
    else:
        bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
