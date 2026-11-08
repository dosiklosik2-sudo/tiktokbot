import os
import sqlite3
import time
from pathlib import Path

# Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH, когда к сервису подключён Volume.
# Локально (на ПК) база просто лежит рядом с кодом.
_data_dir = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR") or Path(__file__).parent)
_data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _data_dir / "bot.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        first_seen INTEGER,
        last_seen INTEGER,
        banned INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS downloads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        url TEXT,
        kind TEXT,
        ok INTEGER,
        ts INTEGER
    );
    """
)
conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER)")
try:  # миграция для баз, созданных до появления VIP
    conn.execute("ALTER TABLE users ADD COLUMN vip INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
conn.commit()


def touch_user(user_id: int, username: str | None, first_name: str | None) -> None:
    now = int(time.time())
    conn.execute(
        """INSERT INTO users(id, username, first_name, first_seen, last_seen)
           VALUES(?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               username=excluded.username,
               first_name=excluded.first_name,
               last_seen=excluded.last_seen""",
        (user_id, username, first_name, now, now),
    )
    conn.commit()


def is_banned(user_id: int) -> bool:
    row = conn.execute("SELECT banned FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def set_ban(user_id: int, banned: bool) -> bool:
    """False, если такого пользователя нет в базе."""
    cur = conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), user_id))
    conn.commit()
    return cur.rowcount > 0


def log_download(user_id: int, url: str, kind: str, ok: bool) -> int:
    cur = conn.execute(
        "INSERT INTO downloads(user_id, url, kind, ok, ts) VALUES(?,?,?,?,?)",
        (user_id, url, kind, int(ok), int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def get_download_url(download_id: int) -> str | None:
    row = conn.execute("SELECT url FROM downloads WHERE id=?", (download_id,)).fetchone()
    return row["url"] if row else None


def all_user_ids() -> list[int]:
    rows = conn.execute("SELECT id FROM users WHERE banned=0").fetchall()
    return [r["id"] for r in rows]


def stats() -> dict:
    day = int(time.time()) - 86400
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "new_24h": q("SELECT COUNT(*) FROM users WHERE first_seen>?", day),
        "active_24h": q("SELECT COUNT(*) FROM users WHERE last_seen>?", day),
        "banned": q("SELECT COUNT(*) FROM users WHERE banned=1"),
        "vip": q("SELECT COUNT(*) FROM users WHERE vip=1"),
        "video_total": q("SELECT COUNT(*) FROM downloads WHERE kind='video' AND ok=1"),
        "audio_total": q("SELECT COUNT(*) FROM downloads WHERE kind='audio' AND ok=1"),
        "dl_24h": q("SELECT COUNT(*) FROM downloads WHERE ok=1 AND ts>?", day),
        "err_24h": q("SELECT COUNT(*) FROM downloads WHERE ok=0 AND ts>?", day),
    }


# ---------- настройки, VIP, лимиты ----------
def get_setting(key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def is_vip(user_id: int) -> bool:
    row = conn.execute("SELECT vip FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["vip"])


def set_vip(user_id: int, vip: bool) -> bool:
    cur = conn.execute("UPDATE users SET vip=? WHERE id=?", (int(vip), user_id))
    conn.commit()
    return cur.rowcount > 0

