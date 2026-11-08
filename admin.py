import asyncio
import os

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

router = Router()
# роутер реагирует только на админов; остальным события уходят дальше
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class Admin(StatesGroup):
    broadcast = State()
    confirm = State()
    ban = State()
    unban = State()
    set_cooldown = State()
    vip_add = State()
    vip_del = State()


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


MENU = _kb(
    [("📊 Статистика", "adm:stats")],
    [("📣 Рассылка", "adm:broadcast")],
    [("⚙️ Лимиты и VIP", "adm:limits")],
    [("🚫 Бан", "adm:ban"), ("✅ Разбан", "adm:unban")],
)
BACK = _kb([("⬅️ В меню", "adm:menu")])


@router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 Админ-панель", reply_markup=MENU)


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=MENU)


@router.callback_query(F.data == "adm:menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🛠 Админ-панель", reply_markup=MENU)
    await cb.answer()


@router.callback_query(F.data == "adm:stats")
async def stats(cb: CallbackQuery):
    s = db.stats()
    text = (
        "📊 Статистика\n\n"
        f"👥 Пользователей: {s['users']} (новых за 24 ч: {s['new_24h']})\n"
        f"🔥 Активных за 24 ч: {s['active_24h']}\n"
        f"🚫 В бане: {s['banned']}   ⭐ VIP: {s['vip']}\n\n"
        f"🎬 Видео скачано: {s['video_total']}\n"
        f"🎵 Звуков скачано: {s['audio_total']}\n"
        f"📥 Скачиваний за 24 ч: {s['dl_24h']}\n"
        f"⚠️ Ошибок за 24 ч: {s['err_24h']}"
    )
    await cb.message.edit_text(text, reply_markup=BACK)
    await cb.answer()


# ---------- рассылка ----------
@router.callback_query(F.data == "adm:broadcast")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.broadcast)
    await cb.message.edit_text(
        "Пришли сообщение для рассылки (текст, фото, видео — что угодно).\n/cancel — отмена"
    )
    await cb.answer()


@router.message(Admin.broadcast)
async def bc_preview(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.chat.id, msg_id=message.message_id)
    await state.set_state(Admin.confirm)
    n = len(db.all_user_ids())
    await message.answer(
        f"Отправить это сообщение {n} пользователям?",
        reply_markup=_kb([("✅ Отправить", "adm:bc_go"), ("❌ Отмена", "adm:menu")]),
    )


@router.callback_query(F.data == "adm:bc_go", Admin.confirm)
async def bc_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    ids = db.all_user_ids()
    await cb.message.edit_text(f"📣 Рассылка идёт... (0/{len(ids)})")
    await cb.answer()

    ok = fail = 0
    for uid in ids:
        for _ in range(2):  # одна повторная попытка при флуд-лимите
            try:
                await bot.copy_message(uid, data["chat_id"], data["msg_id"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)

    await cb.message.edit_text(
        f"✅ Готово.\nДоставлено: {ok}\nНе доставлено: {fail}", reply_markup=BACK
    )


# ---------- бан / разбан ----------
@router.callback_query(F.data.in_({"adm:ban", "adm:unban"}))
async def ban_start(cb: CallbackQuery, state: FSMContext):
    is_ban = cb.data == "adm:ban"
    await state.set_state(Admin.ban if is_ban else Admin.unban)
    await cb.message.edit_text(
        f"Пришли Telegram ID пользователя для {'бана' if is_ban else 'разбана'}.\n/cancel — отмена"
    )
    await cb.answer()


@router.message(Admin.ban)
@router.message(Admin.unban)
async def ban_do(message: Message, state: FSMContext):
    current = await state.get_state()
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой ID. Попробуй ещё раз или /cancel.")
        return
    uid = int(text)
    if uid in ADMIN_IDS:
        await message.answer("Админа банить нельзя.")
        return
    banned = current == Admin.ban.state
    found = db.set_ban(uid, banned)
    await state.clear()
    if found:
        await message.answer(
            f"{'🚫 Заблокирован' if banned else '✅ Разблокирован'}: {uid}", reply_markup=MENU
        )
    else:
        await message.answer("Такого пользователя нет в базе.", reply_markup=MENU)


# ---------- лимиты и VIP ----------
LIMITS_KB = _kb(
    [("✏️ Пауза между запросами", "adm:set_cooldown")],
    [("⭐ Добавить VIP", "adm:vip_add"), ("➖ Убрать VIP", "adm:vip_del")],
    [("⬅️ В меню", "adm:menu")],
)


def limits_text() -> str:
    cooldown = db.get_setting("cooldown", 5)
    return (
        "⚙️ Ограничения для обычных пользователей\n\n"
        f"⏱ Пауза между запросами: {cooldown} с\n"
        "🔒 Одна загрузка за раз — всегда\n\n"
        "Админы и VIP ограничений не имеют."
    )


@router.callback_query(F.data == "adm:limits")
async def limits_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(limits_text(), reply_markup=LIMITS_KB)
    await cb.answer()


@router.callback_query(F.data == "adm:set_cooldown")
async def cooldown_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.set_cooldown)
    await cb.message.edit_text(
        "Пришли паузу между запросами в секундах (0 — без паузы).\n/cancel — отмена"
    )
    await cb.answer()


@router.message(Admin.set_cooldown)
async def cooldown_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) > 3600:
        await message.answer("Нужно целое число от 0 до 3600. Попробуй ещё раз или /cancel.")
        return
    db.set_setting("cooldown", int(text))
    await state.clear()
    await message.answer("✅ Сохранено.\n\n" + limits_text(), reply_markup=LIMITS_KB)


@router.callback_query(F.data.in_({"adm:vip_add", "adm:vip_del"}))
async def vip_start(cb: CallbackQuery, state: FSMContext):
    add = cb.data == "adm:vip_add"
    await state.set_state(Admin.vip_add if add else Admin.vip_del)
    await cb.message.edit_text(
        f"Пришли Telegram ID пользователя, которого нужно {'сделать VIP' if add else 'лишить VIP'}.\n"
        "/cancel — отмена"
    )
    await cb.answer()


@router.message(Admin.vip_add)
@router.message(Admin.vip_del)
async def vip_do(message: Message, state: FSMContext):
    current = await state.get_state()
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой ID. Попробуй ещё раз или /cancel.")
        return
    add = current == Admin.vip_add.state
    found = db.set_vip(int(text), add)
    await state.clear()
    if found:
        await message.answer(
            f"{'⭐ VIP выдан' if add else '➖ VIP снят'}: {text}", reply_markup=LIMITS_KB
        )
    else:
        await message.answer("Такого пользователя нет в базе.", reply_markup=LIMITS_KB)
