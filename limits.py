import math
import time

import db
from admin import ADMIN_IDS

# пользователи, у которых прямо сейчас идёт загрузка
active: set[int] = set()
_last_request: dict[int, float] = {}


def exempt(user_id: int) -> bool:
    """Админы и VIP без ограничений."""
    return user_id in ADMIN_IDS or db.is_vip(user_id)


def is_busy(user_id: int) -> bool:
    return user_id in active and not exempt(user_id)


def check(user_id: int) -> str | None:
    """Возвращает текст отказа или None, если можно качать."""
    if exempt(user_id):
        return None

    if user_id in active:
        return "⏳ Подожди, предыдущее видео ещё скачивается."

    cooldown = db.get_setting("cooldown", 5)
    wait = _last_request.get(user_id, 0) + cooldown - time.monotonic()
    if wait > 0:
        return f"⏱ Слишком часто. Подожди {math.ceil(wait)} с."

    return None


def start(user_id: int) -> None:
    _last_request[user_id] = time.monotonic()
    active.add(user_id)


def finish(user_id: int) -> None:
    active.discard(user_id)


def status_text(user_id: int) -> str:
    if exempt(user_id):
        return "⭐ У тебя нет ограничений."
    cooldown = db.get_setting("cooldown", 5)
    return f"⏱ Пауза между запросами: {cooldown} с\n🔒 Одна загрузка за раз."
