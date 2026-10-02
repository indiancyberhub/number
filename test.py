"""
Indian Cyber Hub - OSINT Bot (Termux Ready — Token Hardcoded)
Run: python test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Optional

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ===========================================================================
# ⚙️ CONFIG — TOKEN HARDCODED (ENV VAR IGNORED COMPLETELY)
# ===========================================================================
# ⚠️⚠️⚠️ YE TOKEN FIXED HAI — ENV VAR ISSE OVERRIDE NAHI KAR SAKTA ⚠️⚠️⚠️
BOT_TOKEN = "8791206646:AAHI2xud5nXubIDqLTVEZvdZBlaVLQRZjB4"

# API endpoints (env var optional — default me hardcoded)
PUBLIC_OSINT_API_URL = "https://osint.invalidayushh.workers.dev/numv2"
PUBLIC_OSINT_API_KEY = "Yogixysjisjsn"

# ⭐ Termux-friendly path (absolute)
DB_PATH = os.path.expanduser("~/number1/bot.db")

OWNER_ID = 8250721152
ADMIN_IDS: set[int] = {OWNER_ID}
_env_admins = os.getenv("ADMIN_IDS", "").strip()
if _env_admins:
    for x in _env_admins.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

SUPER_ADMIN_NAME      = "@indiancyberhub247"
SUPER_ADMIN_LINK      = "https://t.me/indiancyberhub247"
DEFAULT_CREDITS       = 0
ADMIN_UNLIMITED       = True
MAX_MSG_LEN           = 3800
SHOW_RAW_API_RESPONSE = True

HIDDEN_API_KEYS = {"expiry_date", "days_left", "developer", "updates"}

DEFAULT_CHANNELS = [
    {"username": "@indiancyberhub24", "name": "Indian Cyber Hub", "url": "https://t.me/indiancyberhub24"},
]

BLOCKED_CHANNELS = {
    "@A_ToolsX", "@a_toolsx", "@AToolsX", "@atoolsx",
    "a_toolsx", "A_ToolsX", "AToolsX",
}

_REQUIRED_CHANNELS: list[dict] = []

_VALID_MEMBER_STATUSES = {"creator", "administrator", "member"}


# ===========================================================================
# LOGGING
# ===========================================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("osint-bot")


# ===========================================================================
# DATABASE
# ===========================================================================
_DB_LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


@contextmanager
def _conn():
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    # Ensure parent folder exists
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass

    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                username    TEXT,
                credits     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS access_codes (
                code        TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                max_uses    INTEGER NOT NULL,
                uses        INTEGER NOT NULL DEFAULT 0,
                credits     INTEGER NOT NULL DEFAULT 1,
                active      INTEGER NOT NULL DEFAULT 1,
                created_by  INTEGER
            );
            CREATE TABLE IF NOT EXISTS user_codes (
                user_id      INTEGER NOT NULL,
                code         TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                PRIMARY KEY (user_id, code)
            );
            CREATE TABLE IF NOT EXISTS lookup_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                username  TEXT,
                query     TEXT NOT NULL,
                status    TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                username TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                url      TEXT NOT NULL,
                added_at TEXT NOT NULL,
                added_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    _migrate_db()


def _migrate_db() -> None:
    with _conn() as c:
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(required_channels)").fetchall()}
        except Exception:
            cols = set()
        for col_name, sql in [
            ("added_at", "ALTER TABLE required_channels ADD COLUMN added_at TEXT"),
            ("added_by", "ALTER TABLE required_channels ADD COLUMN added_by INTEGER"),
        ]:
            if col_name not in cols:
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass

    with _conn() as c:
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        except Exception:
            cols = set()
        if "credits" not in cols:
            try:
                c.execute("ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    with _conn() as c:
        try:
            c.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        except Exception:
            pass


def get_setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))


# ---------------- Channels ----------------
def _is_blocked(username: str) -> bool:
    if not username:
        return False
    u = username.strip().lower()
    if not u.startswith("@"):
        u = "@" + u
    for b in BLOCKED_CHANNELS:
        bb = b.strip().lower()
        if not bb.startswith("@"):
            bb = "@" + bb
        if u == bb:
            return True
    return False


def db_add_channel(username: str, name: str, url: str, added_by: Optional[int] = None) -> bool:
    if _is_blocked(username):
        log.warning("🚫 Refused to add blocked channel: %s", username)
        return False
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO required_channels (username, name, url, added_at, added_by)
                VALUES (?, ?, ?, ?, ?)
            """, (username, name, url, _iso(_now()), added_by))
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.OperationalError as e:
        log.error("db_add_channel failed: %s", e)
        return False


def db_remove_channel(username: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM required_channels WHERE username = ?", (username,))
        return cur.rowcount > 0


def db_clear_channels() -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM required_channels")
        return cur.rowcount


def db_list_channels() -> list:
    with _conn() as c:
        return c.execute(
            "SELECT username, name, url, added_at FROM required_channels ORDER BY added_at"
        ).fetchall()


def purge_blocked_channels() -> int:
    removed = 0
    for r in db_list_channels():
        if _is_blocked(r["username"]):
            log.info("🚫 Purging blocked channel: %s (%s)", r["username"], r["name"])
            db_remove_channel(r["username"])
            removed += 1
    return removed


def load_channels() -> None:
    global _REQUIRED_CHANNELS

    n_blocked = purge_blocked_channels()
    if n_blocked:
        log.info("✅ Purged %d blocked channel(s)", n_blocked)

    cleanup_done = get_setting("channels_cleanup_v5", "") == "1"
    if not cleanup_done:
        default_usernames = {ch["username"] for ch in DEFAULT_CHANNELS}
        removed = 0
        for r in db_list_channels():
            if r["username"] not in default_usernames:
                log.info("🧹 Auto-removing old channel: %s (%s)", r["username"], r["name"])
                db_remove_channel(r["username"])
                removed += 1
        if removed:
            log.info("✅ Cleanup complete — removed %d old channel(s)", removed)
        set_setting("channels_cleanup_v5", "1")

    initialized = get_setting("channels_initialized", "") == "1"
    if not initialized:
        for ch in DEFAULT_CHANNELS:
            db_add_channel(ch["username"], ch["name"], ch["url"], None)
        set_setting("channels_initialized", "1")
        log.info("🌱 First run: seeded %d default channel(s)", len(DEFAULT_CHANNELS))

    rows = db_list_channels()
    _REQUIRED_CHANNELS = [
        {"username": r["username"], "name": r["name"], "url": r["url"]}
        for r in rows
        if not _is_blocked(r["username"])
    ]
    log.info("Loaded %d required channel(s): %s",
             len(_REQUIRED_CHANNELS),
             [c["username"] for c in _REQUIRED_CHANNELS])


# ---------------- Users / Credits ----------------
def upsert_user(user_id: int, username: str) -> None:
    now = _iso(_now())
    with _conn() as c:
        c.execute("""
            INSERT INTO users (user_id, first_seen, last_seen, username, credits)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                username  = excluded.username
        """, (user_id, now, now, username or "", DEFAULT_CREDITS))


def get_credits(user_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def add_credits(user_id: int, amount: int) -> int:
    with _conn() as c:
        c.execute("""
            INSERT INTO users (user_id, first_seen, last_seen, username, credits)
            VALUES (?, ?, ?, '', ?)
            ON CONFLICT(user_id) DO UPDATE SET credits = MAX(0, credits + ?)
        """, (user_id, _iso(_now()), _iso(_now()), max(0, amount), amount))
        row = c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        bal = row["credits"] if row else 0
    log.info("💎 add_credits: user=%s amount=%+d new=%s", user_id, amount, bal)
    return bal


def deduct_credit(user_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET credits = credits - 1 WHERE user_id = ? AND credits > 0",
            (user_id,))
        ok = cur.rowcount > 0
        if ok:
            new_bal = c.execute(
                "SELECT credits FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()["credits"]
            log.info("💎 deduct_credit: user=%s new=%s", user_id, new_bal)
        else:
            log.warning("💎 deduct_credit FAILED: user=%s no credits", user_id)
        return ok


def list_users(limit: int = 50) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT user_id, username, last_seen, credits FROM users "
            "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()


# ---------------- Access Codes ----------------
_ALPHABET = string.ascii_uppercase + string.digits


def _rand_block(n: int = 4) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _gen_code() -> str:
    return f"ICH-{_rand_block()}-{_rand_block()}"


def create_code(hours: int, max_uses: int, credits_per_use: int = 1,
                created_by: Optional[int] = None) -> str:
    now = _now()
    expires = now + timedelta(hours=hours)
    with _conn() as c:
        for _ in range(10):
            code = _gen_code()
            try:
                c.execute("""
                    INSERT INTO access_codes
                        (code, created_at, expires_at, max_uses, uses, credits, active, created_by)
                    VALUES (?, ?, ?, ?, 0, ?, 1, ?)
                """, (code, _iso(now), _iso(expires), max_uses, credits_per_use, created_by))
                return code
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("Could not generate unique code")


def revoke_code(code: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE access_codes SET active = 0 WHERE code = ? AND active = 1",
            (code.upper(),))
        return cur.rowcount > 0


def list_codes(limit: int = 30) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT code, created_at, expires_at, max_uses, uses, credits, active "
            "FROM access_codes ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


def _code_is_valid(row) -> bool:
    if not row["active"]:
        return False
    if row["uses"] >= row["max_uses"]:
        return False
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except Exception:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > _now()


def activate_code(user_id: int, code: str):
    code = code.upper().strip()
    now = _now()
    with _conn() as c:
        row = c.execute("SELECT * FROM access_codes WHERE code = ?", (code,)).fetchone()
        if row is None:
            return False, "Key does not exist."
        if not row["active"]:
            return False, "Key revoked."
        if row["uses"] >= row["max_uses"]:
            return False, "Maximum uses reached."
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except Exception:
            return False, "Key invalid."
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False, "Key expired."

        already = c.execute(
            "SELECT 1 FROM user_codes WHERE user_id = ? AND code = ?",
            (user_id, code)).fetchone()
        if already is not None:
            return False, "Aap ye key pehle hi activate kar chuke ho."

        credits_to_add = int(row["credits"] if row["credits"] is not None else 1)
        c.execute(
            "INSERT INTO user_codes (user_id, code, activated_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, code, _iso(now), _iso(exp)))
        c.execute("UPDATE access_codes SET uses = uses + 1 WHERE code = ?", (code,))
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?",
                  (credits_to_add, user_id))
        new_bal = c.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()["credits"]

    log.info("🔑 Key %s activated by user %s (+%s credits, bal %s)",
             code, user_id, credits_to_add, new_bal)
    return True, {"code": code, "credits_added": credits_to_add,
                  "balance": new_bal, "expires_at": exp}


def user_active_key_info(user_id: int) -> Optional[dict]:
    now = _now()
    with _conn() as c:
        rows = c.execute("""
            SELECT ac.code, ac.expires_at, ac.active, ac.uses, ac.max_uses, ac.credits
            FROM user_codes uc JOIN access_codes ac ON ac.code = uc.code
            WHERE uc.user_id = ? ORDER BY uc.activated_at DESC
        """, (user_id,)).fetchall()
    for r in rows:
        if not r["active"] or r["uses"] > r["max_uses"]:
            continue
        try:
            exp = datetime.fromisoformat(r["expires_at"])
        except Exception:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            return {
                "code": r["code"],
                "expires_at": exp,
                "key_credits": int(r["credits"] or 0),
            }
    return None


def rate_limit_ok(user_id: int, max_calls: int = 10, window_seconds: int = 60) -> bool:
    cutoff = _iso(_now() - timedelta(seconds=window_seconds))
    with _conn() as c:
        c.execute("DELETE FROM rate_log WHERE timestamp < ?",
                  (_iso(_now() - timedelta(days=1)),))
        n = c.execute(
            "SELECT COUNT(*) AS n FROM rate_log WHERE user_id = ? AND timestamp >= ?",
            (user_id, cutoff)).fetchone()["n"]
        if n >= max_calls:
            return False
        c.execute("INSERT INTO rate_log (user_id, timestamp) VALUES (?, ?)",
                  (user_id, _iso(_now())))
        return True


def log_lookup(user_id: int, username: str, query: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO lookup_logs (user_id, username, query, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username or "", query, status, _iso(_now())))


def list_logs(limit: int = 30) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT user_id, username, query, status, timestamp FROM lookup_logs "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_stats() -> dict:
    day_ago = _iso(_now() - timedelta(hours=24))
    with _conn() as c:
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_codes = c.execute("SELECT COUNT(*) AS n FROM access_codes").fetchone()["n"]
        active_codes = sum(
            1 for r in c.execute("SELECT * FROM access_codes WHERE active = 1").fetchall()
            if _code_is_valid(r))
        lookups = c.execute("SELECT COUNT(*) AS n FROM lookup_logs").fetchone()["n"]
        lookups_ok = c.execute(
            "SELECT COUNT(*) AS n FROM lookup_logs WHERE status = 'ok'").fetchone()["n"]
        lookups_24h = c.execute(
            "SELECT COUNT(*) AS n FROM lookup_logs WHERE timestamp >= ?",
            (day_ago,)).fetchone()["n"]
    return {"users": users, "total_codes": total_codes, "active_codes": active_codes,
            "lookups": lookups, "lookups_ok": lookups_ok, "lookups_24h": lookups_24h,
            "channels": len(_REQUIRED_CHANNELS)}


# ===========================================================================
# API CLIENT
# ===========================================================================
class LookupError(Exception):
    def __init__(self, kind: str, user_message: str):
        super().__init__(kind)
        self.kind = kind
        self.user_message = user_message


def _scrub_hidden(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub_hidden(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.lower() in HIDDEN_API_KEYS)}
    if isinstance(obj, list):
        return [_scrub_hidden(x) for x in obj]
    return obj


def _do_request(number: str) -> Any:
    params = {"q": number}
    if PUBLIC_OSINT_API_KEY:
        params["key"] = PUBLIC_OSINT_API_KEY

    try:
        r = requests.get(
            PUBLIC_OSINT_API_URL, params=params, timeout=20,
            headers={"Accept": "application/json",
                     "User-Agent": "IndianCyberHub-OSINT/1.0"})
    except requests.Timeout:
        raise LookupError("timeout", "API timed out. Try again.")
    except requests.ConnectionError:
        raise LookupError("connection", "API unreachable. Try later.")
    except requests.RequestException:
        raise LookupError("request", "API request failed.")

    log.info("API status: %s | body: %s", r.status_code, r.text[:300])

    if r.status_code == 401:
        raise LookupError("auth", "API key invalid (401).")
    if r.status_code == 403:
        raise LookupError("forbidden", "API access denied (403).")
    if r.status_code == 429:
        raise LookupError("rate_limit", "API rate limit. Try later.")
    if r.status_code >= 500:
        raise LookupError("server", "API server error. Try later.")
    if r.status_code >= 400:
        raise LookupError("client", f"API error HTTP {r.status_code}.")

    try:
        payload = r.json()
    except ValueError:
        txt = r.text.strip()
        if txt:
            return {"raw_response": txt}
        raise LookupError("json", "API returned invalid response.")

    return _scrub_hidden(payload)


async def lookup_number(number: str) -> Any:
    return await asyncio.to_thread(_do_request, number)


def format_result(result: Any) -> str:
    if result is None:
        return "No data."
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, indent=2, ensure_ascii=False)
        except Exception:
            return str(result)
    return str(result)


# ===========================================================================
# HELPERS
# ===========================================================================
PHONE_RE = re.compile(r"^\+?\d{8,15}$")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def validate_number(raw: str) -> Optional[str]:
    if not raw:
        return None
    n = raw.strip().replace(" ", "").replace("-", "")
    return n if PHONE_RE.match(n) else None


def looks_like_number(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    cleaned = t.replace(" ", "").replace("-", "")
    return bool(PHONE_RE.match(cleaned))


def contact_block() -> str:
    return f"🎟 Access Key ke liye DM kare:\n[{SUPER_ADMIN_NAME}]({SUPER_ADMIN_LINK})"


def access_denied_text(reason: str = "no_key") -> str:
    if reason == "expired":
        head, body = "🔐 Access Expired", "Aapki access key expire ho gayi hai."
    elif reason == "revoked":
        head, body = "🔐 Access Revoked", "Aapki access key revoke kar di gayi hai."
    elif reason == "exhausted":
        head, body = "🔐 Access Exhausted", "Aapki access key ki maximum uses khatam ho gayi."
    elif reason == "no_credits":
        head, body = "💎 Credits Khatam", "Aapke credits khatam ho gaye."
    else:
        head, body = "🔐 Access Required", "Is bot ko use karne ke liye valid access key required hai."

    return (f"*{head}*\n\n{body}\n\n"
            "Key milne ke baad:\n`/activate YOUR-KEY`\n\n"
            f"{contact_block()}\n\n"
            "⚠️ Bina valid key ke lookup available nahi hai.")


def main_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔎 How to Lookup", callback_data="u:howlookup"),
         InlineKeyboardButton("💎 My Credits", callback_data="u:credits")],
        [InlineKeyboardButton("🎟 Activate Key", callback_data="u:activate"),
         InlineKeyboardButton("📊 My Status", callback_data="u:status")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="u:help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("🛡 Admin Panel", callback_data="a:panel")])
    return InlineKeyboardMarkup(rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Generate Access Key", callback_data="a:newcode")],
        [InlineKeyboardButton("📋 Active Keys", callback_data="a:codes"),
         InlineKeyboardButton("🚫 Revoke Key", callback_data="a:revoke")],
        [InlineKeyboardButton("👥 Users", callback_data="a:users"),
         InlineKeyboardButton("💎 Add Credits", callback_data="a:addcredits")],
        [InlineKeyboardButton("🔎 Lookup Logs", callback_data="a:logs"),
         InlineKeyboardButton("📊 Statistics", callback_data="a:stats")],
        [InlineKeyboardButton("📢 Manage Channels", callback_data="a:channels_menu")],
        [InlineKeyboardButton("📡 Debug Channels", callback_data="a:debug")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="a:back")],
    ])


def channels_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel/Group", callback_data="a:addch_hint")],
        [InlineKeyboardButton("➖ Remove Channel", callback_data="a:removech_hint")],
        [InlineKeyboardButton("📋 List Channels", callback_data="a:listchannels")],
        [InlineKeyboardButton("🧹 Clear All", callback_data="a:clearch_hint")],
        [InlineKeyboardButton("🌱 Add Defaults", callback_data="a:adddefaults")],
        [InlineKeyboardButton("⬅️ Back", callback_data="a:panel")],
    ])


def touch_user(update: Update) -> None:
    u = update.effective_user
    if u is not None:
        upsert_user(u.id, u.username or "")


def check_access(user_id: int):
    if is_admin(user_id) and ADMIN_UNLIMITED:
        return True, "admin"
    key_info = user_active_key_info(user_id)
    if key_info is not None:
        return True, "key"
    if get_credits(user_id) > 0:
        return True, "credits"
    return False, "no_key"


# ===========================================================================
# 📢 CHANNEL GATE
# ===========================================================================
def channels_join_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for ch in _REQUIRED_CHANNELS:
        if _is_blocked(ch["username"]):
            continue
        rows.append([InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["url"])])
    rows.append([InlineKeyboardButton("✅ Verify / Main Menu", callback_data="u:verify")])
    return InlineKeyboardMarkup(rows)


def channels_join_text() -> str:
    lines = [
        "🔐 *Access Restricted*",
        "",
        "Bot use karne ke liye pehle niche diye gaye *channel(s)/group(s)* join karo:",
        "",
    ]
    for ch in _REQUIRED_CHANNELS:
        if _is_blocked(ch["username"]):
            continue
        lines.append(f"📢 [{ch['name']}]({ch['url']})")
    lines += [
        "",
        "✅ *Join karne ke baad* neeche wala *Verify* button dabao.",
        "",
        "❗️ _Bina join kiye bot kaam nahi karega._",
    ]
    return "\n".join(lines)


async def check_missing_channels(context: ContextTypes.DEFAULT_TYPE,
                                  user_id: int) -> list[str]:
    if not _REQUIRED_CHANNELS:
        return []

    missing: list[str] = []

    for ch in _REQUIRED_CHANNELS:
        if _is_blocked(ch["username"]):
            continue
        try:
            member = await context.bot.get_chat_member(
                chat_id=ch["username"], user_id=user_id)
            status = getattr(member, "status", "")
            if hasattr(status, "value"):
                status = status.value
            status = str(status).lower().strip()

            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted" and getattr(member, "is_member", False):
                continue
            missing.append(ch["name"])
        except Exception as e:
            log.warning("Channel check FAILED [%s] → marking missing: %s",
                        ch["username"], str(e)[:150])
            missing.append(ch["name"])

    return missing


async def _reply(update: Update, text: str,
                 reply_markup=None, prefer_edit: bool = False) -> None:
    try:
        cq = update.callback_query
        target = None
        use_edit = False

        if cq and cq.message:
            target = cq.message
            use_edit = prefer_edit
        elif update.message:
            target = update.message
        else:
            return

        if use_edit:
            try:
                await target.edit_text(
                    text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True)
                return
            except Exception:
                pass

        try:
            await target.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
                disable_web_page_preview=True)
            return
        except Exception as md_err:
            log.warning("Markdown failed: %s", md_err)

        try:
            await target.reply_text(text, reply_markup=reply_markup)
        except Exception as e:
            log.exception("Plain reply failed: %s", e)

    except Exception as e:
        log.exception("_reply failed: %s", e)


async def send_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, channels_join_text(),
                 reply_markup=channels_join_keyboard(), prefer_edit=True)


def require_channels(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        u = update.effective_user
        if u is None:
            return

        if is_admin(u.id):
            return await func(update, context, *a, **kw)

        missing = await check_missing_channels(context, u.id)
        if missing:
            await send_join_prompt(update, context)
            return

        return await func(update, context, *a, **kw)

    return wrapper


# ===========================================================================
# USER COMMANDS
# ===========================================================================
@require_channels
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    credits = get_credits(uid)
    allowed, reason = check_access(uid)
    key_info = user_active_key_info(uid)
    admin_flag = "🛡 *You are an ADMIN.*\n\n" if is_admin(uid) else ""

    if not allowed and not is_admin(uid):
        await _reply(update,
            "👋 *Welcome to Indian Cyber Hub – Authorized OSINT Bot*\n\n"
            f"{admin_flag}{access_denied_text(reason)}",
            reply_markup=main_menu(is_admin(uid)))
        return

    if is_admin(uid):
        mode_line = f"🛡 Admin Unlimited  |  💎 Credits: *{credits}*"
    elif key_info:
        if key_info["key_credits"] == 0:
            mode_line = f"♾ Unlimited (key)  |  💎 Credits: *{credits}*"
        else:
            mode_line = f"🔑 Key active  |  💎 Credits: *{credits}* (1 per lookup)"
    else:
        mode_line = f"💎 Credits: *{credits}* (1 credit = 1 lookup)"

    text = ("👋 *Welcome to Indian Cyber Hub – Authorized OSINT Bot*\n\n"
            f"{admin_flag}"
            "📱 *Bas number bhejo — result milega.*\n\n"
            "Examples:\n`9876543210`\n`+919876543210`\n\n"
            f"{mode_line}\n\n"
            "• /activate CODE – key activate\n"
            "• /credits – balance\n"
            "• /status – status\n"
            "• /help – help")
    if is_admin(uid):
        text += "\n\n🛡 `/admin` – Admin Panel"
    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))


@require_channels
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    text = ("ℹ️ *Help*\n\n*Kaise use karein:*\n"
            "1️⃣ Access key activate karo: `/activate ICH-XXXX-XXXX`\n"
            "2️⃣ Phir number bhejo:\n`9876543210`\n`+919876543210`\n\n"
            "*Commands:*\n"
            "`/start` – main menu\n`/activate CODE` – key activate\n"
            "`/lookup NUMBER` – lookup\n`/credits` – balance\n"
            "`/status` – full status\n`/help` – ye message\n\n"
            f"{contact_block()}")
    if is_admin(uid):
        text += ("\n\n*Admin:*\n`/admin` `/newcode` `/addcredits` "
                 "`/revoke` `/codes` `/users` `/logs` `/stats`\n"
                 "`/addchannel` `/removechannel` `/channels` `/clearchannels`\n"
                 "`/adddefaults` `/resetchannels` `/debugchannels` `/purge`")
    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))


@require_channels
async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    if not context.args:
        await _reply(update,
            f"Usage: `/activate ICH-XXXX-XXXX`\n\n{contact_block()}")
        return

    code = context.args[0].strip().upper()
    ok, info = activate_code(update.effective_user.id, code)
    if not ok:
        await _reply(update,
            f"❌ *Invalid Access Key*\n\nReason: {info}\n\n"
            "Possible reasons:\n• Key does not exist\n• Key expired\n"
            "• Key revoked\n• Max uses reached\n• Already activated\n\n"
            f"{contact_block()}")
        return

    exp_str = info["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
    bal = info["balance"]
    credits_added = info["credits_added"]

    if credits_added == 0:
        mode_line = "♾ *Unlimited lookups* (jab tak key valid hai)"
        footer_line = "Ab aap bejhijhak `/lookup NUMBER` kar sakte ho — unlimited!"
    else:
        mode_line = f"💎 *{credits_added} lookups* mil gaye"
        footer_line = f"Ab aapke paas *{bal}* lookups hain. Har lookup 1 credit katega."

    await _reply(update,
        "✅ *Access Activated*\n\n"
        f"🔑 Key: `{info['code']}`\n"
        f"⏳ Valid until: `{exp_str}`\n"
        f"📈 Mode: {mode_line}\n"
        f"💎 Balance: `{bal}`\n\n"
        f"{footer_line}",
        reply_markup=main_menu(is_admin(update.effective_user.id)))


@require_channels
async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    bal = get_credits(uid)
    key_info = user_active_key_info(uid)

    if is_admin(uid) and ADMIN_UNLIMITED:
        text = f"💎 Credits: *{bal}*\n🛡 Admin: *Unlimited (credits not used)*"
    elif key_info and key_info["key_credits"] == 0:
        text = (f"💎 Credits: *{bal}*\n"
                "♾ *Unlimited mode* — key active hai, credits use nahi ho rahe")
    elif key_info and key_info["key_credits"] > 0:
        text = (f"💎 Credits: *{bal}*\n"
                f"🔑 Key: `{key_info['code']}`\n"
                "📌 Har lookup 1 credit katega.")
    else:
        text = (f"💎 Credits: *{bal}*\n\n"
                "📌 *1 lookup = 1 credit*\n"
                "Har number bhejne par 1 credit katega.")
    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))


@require_channels
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    info = user_active_key_info(uid)
    allowed, reason = check_access(uid)
    bal = get_credits(uid)

    access_line = "🟢 Active" if allowed else "🔴 Inactive"
    key_line = f"`{info['code']}`" if info else "—"
    exp_line = info["expires_at"].strftime("%Y-%m-%d %H:%M UTC") if info else "—"

    if is_admin(uid) and ADMIN_UNLIMITED:
        mode_line = "🛡 Admin Unlimited"
    elif info and info.get("key_credits", 0) == 0:
        mode_line = "♾ Unlimited (key)"
    elif info and info.get("key_credits", 0) > 0:
        mode_line = f"🔑 Key ({info['key_credits']} credits/key)"
    elif bal > 0:
        mode_line = f"💎 Credit-based ({bal} left)"
    else:
        mode_line = "🔴 No access"

    text = ("📊 *Your Status*\n\n"
            f"🆔 User ID: `{uid}`\n"
            f"🔐 Access: *{access_line}*\n"
            f"🎟 Active Key: {key_line}\n"
            f"⏳ Key Expiry: `{exp_line}`\n"
            f"📈 Mode: *{mode_line}*\n"
            f"💎 Credits: `{bal}`\n"
            f"🛡 Admin: `{'Yes' if is_admin(uid) else 'No'}`")
    if not allowed:
        text += f"\n\n_{reason}_"
    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))


async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u is None:
        return
    touch_user(update)

    if is_admin(u.id):
        await _reply(update, "✅ Welcome back, admin!",
                     reply_markup=main_menu(True))
        return

    missing = await check_missing_channels(context, u.id)
    if missing:
        text = ("⚠️ *Verification Pending*\n\n"
                "Abhi ye channel(s)/group(s) join karna baaki hai:\n\n"
                + "\n".join(f"• {m}" for m in missing)
                + "\n\n👇 Join karke *✅ Verify* dobara dabao.")
        await _reply(update, text, reply_markup=channels_join_keyboard())
        return

    await _reply(update,
                 "✅ *Verified Successfully!*\n\n"
                 "Ab aap bot use kar sakte ho.\n\n"
                 "📱 Bas apna number bhejo — result milega.",
                 reply_markup=main_menu(False))


# ===========================================================================
# LOOKUP
# ===========================================================================
@require_channels
async def do_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, number: str) -> None:
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    chat = update.effective_chat

    if not rate_limit_ok(uid, max_calls=10, window_seconds=60):
        log_lookup(uid, uname, number, "rate_limited")
        await context.bot.send_message(chat.id, "⏳ Rate limit. 1 min wait karo.")
        return

    allowed, reason = check_access(uid)
    if not allowed:
        log_lookup(uid, uname, number, f"denied:{reason}")
        await context.bot.send_message(chat.id, access_denied_text(reason),
                                       parse_mode=ParseMode.MARKDOWN)
        return

    is_admin_user = is_admin(uid) and ADMIN_UNLIMITED
    key_info = user_active_key_info(uid)
    key_is_unlimited = key_info is not None and key_info.get("key_credits", 0) == 0

    if is_admin_user or key_is_unlimited:
        unlimited_mode = True
    else:
        unlimited_mode = False

    credits_used = 0
    if not unlimited_mode:
        if not deduct_credit(uid):
            log_lookup(uid, uname, number, "no_credits")
            await context.bot.send_message(chat.id, access_denied_text("no_credits"),
                                           parse_mode=ParseMode.MARKDOWN)
            return
        credits_used = 1

    msg = await context.bot.send_message(chat.id, "🔎 Processing…")

    try:
        result = await lookup_number(number)
    except LookupError as e:
        log_lookup(uid, uname, number, f"error:{e.kind}")
        if credits_used:
            add_credits(uid, 1)
        await msg.edit_text(f"⚠️ {e.user_message}\n\n_Credit refunded._",
                            parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        log.exception("lookup failed: %s", e)
        log_lookup(uid, uname, number, "error:unexpected")
        if credits_used:
            add_credits(uid, 1)
        await msg.edit_text("⚠️ Unexpected error.\n\n_Credit refunded._",
                            parse_mode=ParseMode.MARKDOWN)
        return

    if result in (None, {}, []):
        log_lookup(uid, uname, number, "empty")
        if credits_used:
            add_credits(uid, 1)
        await msg.edit_text(
            f"ℹ️ Number `{number}` ka koi data nahi mila.\n\n_Credit refunded._",
            parse_mode=ParseMode.MARKDOWN)
        return

    log_lookup(uid, uname, number, "ok")
    body = format_result(result)
    header = f"✅ *Result for* `{number}`\n\n"
    bal = get_credits(uid)

    if unlimited_mode:
        if is_admin_user:
            footer = f"\n\n🛡 *Admin mode*  •  💎 Credits: `{bal}` _(not used)_"
        else:
            footer = f"\n\n♾ *Unlimited*  •  💎 Credits: `{bal}` _(key active)_"
    else:
        if credits_used:
            footer = f"\n\n💎 *1 credit used*  •  Credits left: `{bal}`"
        else:
            footer = f"\n\n💎 Credits: `{bal}`"

    full = header + "```\n" + body + "\n```" + footer

    if len(full) > MAX_MSG_LEN:
        await msg.edit_text(header + "📄 Result lamba hai, parts me bhej raha hoon…",
                            parse_mode=ParseMode.MARKDOWN)
        chunks, cur = [], ""
        for line in body.splitlines():
            if len(cur) + len(line) + 1 > MAX_MSG_LEN - 40:
                chunks.append(cur); cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur:
            chunks.append(cur)
        for i, chunk in enumerate(chunks):
            payload = f"📄 Part {i+1}/{len(chunks)}\n```\n{chunk}\n```"
            try:
                await context.bot.send_message(chat.id, payload,
                                               parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await context.bot.send_message(chat.id, payload)
        if footer:
            await context.bot.send_message(chat.id, footer,
                                           parse_mode=ParseMode.MARKDOWN)
    else:
        try:
            await msg.edit_text(full, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await msg.edit_text(header + body + footer)


@require_channels
async def cmd_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    if not context.args:
        await _reply(update,
            "Usage: `/lookup 9876543210`\n\nYa direct number bhi bhej sakte ho.")
        return
    number = validate_number(context.args[0])
    if not number:
        await _reply(update, "❌ Invalid number. Example: `9876543210`")
        return
    await do_lookup(update, context, number)


@require_channels
async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if not looks_like_number(text):
        await _reply(update,
            "🤔 Ye number nahi lagta.\n\nNumber bhejo aise:\n"
            "`9876543210`\n`+919876543210`")
        return
    number = validate_number(text)
    if not number:
        await _reply(update, "❌ Number format galat hai.")
        return
    await do_lookup(update, context, number)


# ===========================================================================
# ADMIN
# ===========================================================================
async def send_admin_text(update: Update, text: str, reply_markup=None) -> None:
    await _reply(update, text, reply_markup=reply_markup)


async def edit_admin_text(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return
        except Exception as e:
            log.warning("edit failed: %s", e)
    await _reply(update, text, reply_markup=reply_markup)


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if u is None or not is_admin(u.id):
            if update.callback_query:
                try:
                    await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
                except Exception:
                    pass
            elif update.message:
                await update.message.reply_text("⛔ Unauthorized.")
            return
        touch_user(update)
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("🛡 *ADMIN PANEL*\n\n"
            f"👑 Owner ID: `{OWNER_ID}`\n"
            f"👥 Admins: `{len(ADMIN_IDS)}`\n"
            f"📢 Required Channels: `{len(_REQUIRED_CHANNELS)}`\n\n"
            "🎟 Generate Access Key\n📋 Active Keys\n🚫 Revoke Key\n"
            "👥 Users\n💎 Add Credits\n🔎 Lookup Logs\n📊 Statistics\n"
            "📢 Manage Channels\n\n"
            "*Commands*\n"
            "`/newcode [hours] [uses] [credits]`\n"
            "`/addcredits USER_ID AMOUNT`\n"
            "`/revoke CODE`\n"
            "`/addchannel @username Display Name`\n"
            "`/removechannel @username`\n"
            "`/channels` – list required channels\n"
            "`/clearchannels` – remove ALL channels\n"
            "`/adddefaults` – add default channels\n"
            "`/resetchannels` – full reset\n"
            "`/purge` – purge blocked channels")
    if update.callback_query:
        await edit_admin_text(update, text, reply_markup=admin_menu())
    else:
        await _reply(update, text, reply_markup=admin_menu())


@admin_only
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2:
        await _reply(update,
            "📢 *Add Channel/Group*\n\n"
            "Usage: `/addchannel <username_or_id> <display name> [url]`\n\n"
            "*Examples:*\n"
            "`/addchannel @mychannel My Channel`\n"
            "`/addchannel @mychannel My Channel https://t.me/mychannel`\n"
            "`/addchannel -1001234567890 Private Group https://t.me/+abc123`\n\n"
            "⚠️ Bot ko us channel/group me *admin* banana zaroori hai.")
        return

    raw_username = args[0].strip()
    url = None
    if len(args) >= 3 and args[-1].startswith("http"):
        url = args[-1]
        name = " ".join(args[1:-1]).strip()
    else:
        name = " ".join(args[1:]).strip()

    if raw_username.startswith(("@", "+", "-")):
        username = raw_username
    elif raw_username.isdigit() or (raw_username.startswith("-") and raw_username[1:].isdigit()):
        username = raw_username
    else:
        username = "@" + raw_username

    if _is_blocked(username):
        await _reply(update,
            f"🚫 *Channel Blocked*\n\n"
            f"`{username}` is a blocked channel and cannot be added.")
        return

    if not url:
        if username.startswith("@"):
            url = f"https://t.me/{username.lstrip('@')}"
        else:
            url = "https://t.me/"

    if not name:
        await _reply(update, "❌ Display name required.")
        return

    ok = db_add_channel(username, name, url, update.effective_user.id)
    if not ok:
        await _reply(update,
            f"❌ Channel `{username}` already exists.\n\n"
            f"Remove first: `/removechannel {username}`")
        return

    load_channels()
    await _reply(update,
        "✅ *Channel Added*\n\n"
        f"📢 Name: {name}\n"
        f"🆔 ID: `{username}`\n"
        f"🔗 URL: {url}\n\n"
        f"📊 Total required: `{len(_REQUIRED_CHANNELS)}`\n\n"
        "ℹ️ Ab is channel me bot ko *admin* banao.")


@admin_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update,
            "Usage: `/removechannel @username`\n"
            "Example: `/removechannel @mychannel`")
        return

    raw = context.args[0].strip()
    username = raw if raw.startswith(("@", "+", "-")) else "@" + raw

    ok = db_remove_channel(username)
    if not ok:
        await _reply(update, f"❌ Not found: `{username}`")
        return

    load_channels()
    await _reply(update,
        f"✅ Removed: `{username}`\n\n"
        f"📊 Total required: `{len(_REQUIRED_CHANNELS)}`\n\n"
        f"ℹ️ Ye restart ke baad wapas nahi aayega.")


@admin_only
async def cmd_clearchannels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = db_clear_channels()
    set_setting("channels_initialized", "1")
    load_channels()
    await _reply(update,
        f"🧹 *All Channels Cleared*\n\n"
        f"Removed: `{n}` channel(s)\n"
        f"Active now: `{len(_REQUIRED_CHANNELS)}`\n\n"
        f"ℹ️ Restart ke baad bhi koi channel wapas nahi aayega.\n"
        f"Add new: `/addchannel @username Name`")


@admin_only
async def cmd_adddefaults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    added = 0
    for ch in DEFAULT_CHANNELS:
        if db_add_channel(ch["username"], ch["name"], ch["url"], None):
            added += 1
    load_channels()
    await _reply(update,
        f"🌱 *Defaults Added*\n\n"
        f"Added: `{added}` new channel(s)\n"
        f"Active total: `{len(_REQUIRED_CHANNELS)}`")


@admin_only
async def cmd_resetchannels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = db_clear_channels()
    set_setting("channels_initialized", "0")
    set_setting("channels_cleanup_v5", "0")
    load_channels()
    await _reply(update,
        f"🔄 *Full Reset Done*\n\n"
        f"Cleared: `{n}` channel(s)\n\n"
        f"ℹ️ Ab bot restart karo — DEFAULT_CHANNELS se re-seed hoga.")


@admin_only
async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = purge_blocked_channels()
    load_channels()
    await _reply(update,
        f"🚫 *Blocked Channels Purged*\n\n"
        f"Removed: `{n}` blocked channel(s)\n"
        f"Active: `{len(_REQUIRED_CHANNELS)}`")


@admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _REQUIRED_CHANNELS:
        await _reply(update,
            "📢 *No required channels.*\n\n"
            "Add one: `/addchannel @username Display Name`\n"
            "Or: `/adddefaults`")
        return

    lines = [f"📢 *Required Channels* ({len(_REQUIRED_CHANNELS)})", ""]
    for i, ch in enumerate(_REQUIRED_CHANNELS, 1):
        lines.append(f"{i}. {ch['name']}\n   `{ch['username']}`\n   {ch['url']}")
    lines.append("")
    lines.append("➕ `/addchannel @username Name`")
    lines.append("➖ `/removechannel @username`")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_debugchannels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["🔧 *Channel Debug*", ""]

    lines.append("*🚫 Blocked Channels:*")
    for b in sorted(BLOCKED_CHANNELS):
        lines.append(f"  • `{b}`")
    lines.append("")

    if not _REQUIRED_CHANNELS:
        lines.append("📢 No required channels configured.")
        await _reply(update, "\n".join(lines))
        return

    lines.append("*📢 Active Channels:*")
    for ch in _REQUIRED_CHANNELS:
        try:
            me = await context.bot.get_chat_member(ch["username"], context.bot.id)
            bot_status = getattr(me, "status", "")
            if hasattr(bot_status, "value"):
                bot_status = bot_status.value
            bot_status = str(bot_status).lower()
            if bot_status in ("administrator", "creator"):
                icon, note = "✅", f"bot is {bot_status}"
            elif bot_status == "member":
                icon, note = "⚠️", "bot is member (admin banao!)"
            else:
                icon, note = "❌", f"bot status: {bot_status}"
            lines.append(f"{icon} `{ch['username']}` — {note}")
        except Exception as e:
            lines.append(f"❌ `{ch['username']}` — ERROR: {str(e)[:70]}")

    lines.append("")
    lines.append("ℹ️ ✅=OK, ⚠️=bot ko admin banao, ❌=error")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_newcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hours, uses, credits = 24, 5, 1
    args = context.args or []
    try:
        if len(args) >= 1: hours = int(args[0])
        if len(args) >= 2: uses = int(args[1])
        if len(args) >= 3: credits = int(args[2])
    except ValueError:
        await _reply(update,
            "Usage: `/newcode [hours] [uses] [credits]`\n\n"
            "Examples:\n"
            "`/newcode 720 1 0` – 30 din, 1 user, unlimited\n"
            "`/newcode 720 5 100` – 30 din, 5 users, 100 credits each")
        return
    if not (1 <= hours <= 24 * 365 * 100):
        await _reply(update, "Hours: 1–876000"); return
    if not (1 <= uses <= 10000):
        await _reply(update, "Uses: 1–10000"); return
    if not (0 <= credits <= 100000):
        await _reply(update, "Credits: 0–100000"); return

    code = create_code(hours=hours, max_uses=uses,
                       credits_per_use=credits,
                       created_by=update.effective_user.id)

    if credits == 0:
        mode_line = "♾ *Unlimited lookups* (key valid tak)"
    else:
        mode_line = f"💎 *{credits} lookups* per user"

    await _reply(update,
        "✅ *Access Key Generated*\n\n"
        f"🔑 *Key (copy this):*\n"
        f"`/activate {code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}` user(s)\n"
        f"📊 Uses: `0/{uses}`\n"
        f"📈 Mode: {mode_line}\n\n"
        "ℹ️ Upar wali line user ko bhejo — wo direct `/activate` kar lega.")


@admin_only
async def cmd_addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await _reply(update,
            "Usage: `/addcredits USER_ID AMOUNT`\nExample: `/addcredits 8250721152 10`")
        return
    try:
        target = int(context.args[0]); amount = int(context.args[1])
    except ValueError:
        await _reply(update, "Numbers do."); return
    if amount == 0:
        await _reply(update, "Amount 0 nahi."); return
    new_bal = add_credits(target, amount)
    await _reply(update,
        f"✅ `{target}` ko `{amount:+d}` credits.\n💎 New balance: `{new_bal}`")


@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Usage: `/revoke ICH-XXXX-XXXX`"); return
    ok = revoke_code(context.args[0].strip().upper())
    await _reply(update, "✅ Revoked." if ok else "❌ Not found.")


@admin_only
async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_codes(limit=30)
    if not rows:
        await _reply(update, "No keys. Use `/newcode 24 1 0`."); return
    lines = ["📋 *Recent Access Keys*", ""]
    for r in rows:
        icon = "✅" if (r["active"] and _code_is_valid(r)) else "❌"
        mode = "♾" if r["credits"] == 0 else f"💎{r['credits']}"
        lines.append(f"`{r['code']}` – {r['uses']}/{r['max_uses']} – "
                     f"{mode} – exp {r['expires_at'][:16]} – {icon}")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_users(limit=50)
    if not rows:
        await _reply(update, "No users."); return
    lines = ["👥 *Recent Users*", ""]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"`{r['user_id']}` – {uname} – 💎{r['credits']} – {r['last_seen'][:16]}")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_logs(limit=30)
    if not rows:
        await _reply(update, "No lookups."); return
    lines = ["🔎 *Recent Lookups*", ""]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"`{r['timestamp'][:16]}` – `{r['user_id']}` {uname} – "
                     f"`{r['query']}` – {r['status']}")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_stats()
    await _reply(update,
        "📊 *Statistics*\n\n"
        f"Users: `{s['users']}`\n"
        f"Active keys: `{s['active_codes']}`\n"
        f"Total keys: `{s['total_codes']}`\n"
        f"Required channels: `{s['channels']}`\n"
        f"Lookups (total): `{s['lookups']}`\n"
        f"Lookups (ok): `{s['lookups_ok']}`\n"
        f"Lookups (24h): `{s['lookups_24h']}`")


# ===========================================================================
# GENERATE KEY CONVERSATION
# ===========================================================================
GEN_HOURS, GEN_USES, GEN_CREDITS = range(3)


async def gen_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data["gen"] = {}
    await q.message.reply_text(
        "🎟 *Generate Access Key* — Step 1/3\n\n"
        "Validity hours bhejein (e.g. `720` = 30 din).\nCancel: /cancel",
        parse_mode=ParseMode.MARKDOWN)
    return GEN_HOURS


async def gen_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 24 * 365 * 100):
        await update.message.reply_text("❌ 1–876000 ke beech integer bhejo.")
        return GEN_HOURS
    context.user_data["gen"]["hours"] = int(txt)
    await update.message.reply_text(
        "👥 *Step 2/3* — Max uses\nExample: `1`",
        parse_mode=ParseMode.MARKDOWN)
    return GEN_USES


async def gen_uses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 10000):
        await update.message.reply_text("❌ 1–10000 ke beech integer bhejo.")
        return GEN_USES
    context.user_data["gen"]["uses"] = int(txt)
    await update.message.reply_text(
        "💎 *Step 3/3* — Credits per activation\n"
        "• `0` = Unlimited lookups (key valid tak)\n"
        "• `100` = 100 lookups per user\n\nExample: `0`",
        parse_mode=ParseMode.MARKDOWN)
    return GEN_CREDITS


async def gen_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or not (0 <= int(txt) <= 100000):
        await update.message.reply_text("❌ 0–100000 ke beech integer bhejo.")
        return GEN_CREDITS
    g = context.user_data.pop("gen", {})
    hours, uses, credits = g.get("hours", 24), g.get("uses", 5), int(txt)
    code = create_code(hours=hours, max_uses=uses, credits_per_use=credits,
                       created_by=update.effective_user.id)

    if credits == 0:
        mode_line = "♾ *Unlimited lookups* (key valid tak)"
    else:
        mode_line = f"💎 *{credits} lookups* per user"

    await update.message.reply_text(
        "✅ *Access Key Generated*\n\n"
        f"🔑 *Key (copy this):*\n"
        f"`/activate {code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}` user(s)\n"
        f"📊 Uses: `0/{uses}`\n"
        f"📈 Mode: {mode_line}\n\n"
        "ℹ️ Upar wali line user ko bhejo — wo direct `/activate` kar lega.",
        parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def gen_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("gen", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ===========================================================================
# CALLBACK ROUTER
# ===========================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return

    data = q.data or ""
    uid = update.effective_user.id if update.effective_user else 0

    try:
        await q.answer()
    except Exception:
        pass

    try:
        if data == "u:verify":
            touch_user(update)
            await cmd_verify(update, context)
            return

        if data.startswith("a:"):
            if not is_admin(uid):
                try:
                    await q.answer("⛔ Unauthorized", show_alert=True)
                except Exception:
                    pass
                return
            touch_user(update)

            if data == "a:panel":
                await cmd_admin(update, context)
            elif data == "a:codes":
                await cmd_codes(update, context)
            elif data == "a:revoke":
                await _reply(update, "🚫 *Revoke Key*\n\nUse: `/revoke ICH-XXXX-XXXX`")
            elif data == "a:users":
                await cmd_users(update, context)
            elif data == "a:addcredits":
                await _reply(update,
                    "💎 *Add Credits*\n\nUse: `/addcredits USER_ID AMOUNT`\n"
                    "Example: `/addcredits 8250721152 10`")
            elif data == "a:logs":
                await cmd_logs(update, context)
            elif data == "a:stats":
                await cmd_stats(update, context)
            elif data == "a:debug":
                await cmd_debugchannels(update, context)

            elif data == "a:channels_menu":
                text = ("📢 *Manage Required Channels*\n\n"
                        f"Currently active: `{len(_REQUIRED_CHANNELS)}`\n\n"
                        "➕ Add · ➖ Remove · 📋 List · 🧹 Clear · 🌱 Defaults")
                await edit_admin_text(update, text, reply_markup=channels_admin_menu())

            elif data == "a:addch_hint":
                await _reply(update,
                    "➕ *Add Channel/Group*\n\n"
                    "Use: `/addchannel @username Display Name`\n\n"
                    "*Examples:*\n"
                    "`/addchannel @mychannel My Channel`\n"
                    "`/addchannel @mychannel My Channel https://t.me/mychannel`\n"
                    "`/addchannel -1001234567890 Private Group https://t.me/+abc123`\n\n"
                    "⚠️ Bot ko *admin* banana zaroori hai us channel/group me.")

            elif data == "a:removech_hint":
                await _reply(update,
                    "➖ *Remove Channel*\n\n"
                    "Use: `/removechannel @username`\n"
                    "Example: `/removechannel @mychannel`\n\n"
                    "📋 Full list: `/channels`")

            elif data == "a:listchannels":
                await cmd_channels(update, context)

            elif data == "a:clearch_hint":
                await _reply(update,
                    "🧹 *Clear All Channels*\n\n"
                    "Use: `/clearchannels`\n\n"
                    "⚠️ Ye saare channels delete kar dega. Restart ke baad bhi wapas nahi aayenge.")

            elif data == "a:adddefaults":
                await cmd_adddefaults(update, context)

            elif data == "a:back":
                text = "🏠 *Main Menu*\n\nNumber bhejo ya menu use karo 👇"
                try:
                    await q.message.edit_text(
                        text, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=main_menu(is_admin(uid)))
                except Exception:
                    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))
            return

        touch_user(update)

        if not is_admin(uid):
            missing = await check_missing_channels(context, uid)
            if missing:
                await send_join_prompt(update, context)
                return

        if data == "u:howlookup":
            await _reply(update,
                "📱 *Kaise lookup karein:*\n\n"
                "1️⃣ Pehle access key activate karo:\n`/activate ICH-XXXX-XXXX`\n\n"
                "2️⃣ Phir number bhejo:\n`9876543210`\n`+919876543210`\n\n"
                "Ya `/lookup 9876543210` bhi chalta hai.")
        elif data == "u:activate":
            await _reply(update,
                f"Apna key bhejo:\n`/activate ICH-ABCD-1234`\n\n{contact_block()}")
        elif data == "u:status":
            await cmd_status(update, context)
        elif data == "u:credits":
            await cmd_credits(update, context)
        elif data == "u:help":
            await cmd_help(update, context)

    except Exception as e:
        log.exception("on_callback error data=%s: %s", data, e)
        try:
            if q.message:
                await q.message.reply_text(f"⚠️ Error: {str(e)[:200]}")
        except Exception:
            pass


# ===========================================================================
# ERROR HANDLER
# ===========================================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception", exc_info=context.error)


# ===========================================================================
# STARTUP CHECK
# ===========================================================================
def print_startup_banner() -> None:
    tok_preview = BOT_TOKEN[:15] + "..." + BOT_TOKEN[-5:] if BOT_TOKEN else "EMPTY"
    print("=" * 65)
    print("  INDIAN CYBER HUB - OSINT BOT")
    print("=" * 65)
    print(f"  BOT_TOKEN       : {tok_preview}")
    print(f"  DB_PATH         : {DB_PATH}")
    print(f"  OWNER_ID        : {OWNER_ID}")
    print(f"  ADMIN_IDS       : {sorted(ADMIN_IDS)}")
    print(f"  PYTHON          : {os.sys.version.split()[0]}")
    print(f"  ENV BOT_TOKEN   : {os.getenv('BOT_TOKEN', '<NOT SET>')[:20]}")
    print("=" * 65)


# ===========================================================================
# MAIN
# ===========================================================================
async def run() -> None:
    print_startup_banner()

    init_db()
    load_channels()

    log.info("Loaded %d required channel(s): %s",
             len(_REQUIRED_CHANNELS),
             [c["username"] for c in _REQUIRED_CHANNELS])
    log.info("Blocked channels: %s", sorted(BLOCKED_CHANNELS))

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(gen_entry, pattern=r"^a:newcode$")],
        states={
            GEN_HOURS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_hours)],
            GEN_USES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_uses)],
            GEN_CREDITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_credits)],
        },
        fallbacks=[CommandHandler("cancel", gen_cancel)],
        per_chat=True, per_user=True))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("credits", cmd_credits))
    app.add_handler(CommandHandler("verify", cmd_verify))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("newcode", cmd_newcode))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("codes", cmd_codes))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("debugchannels", cmd_debugchannels))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("clearchannels", cmd_clearchannels))
    app.add_handler(CommandHandler("adddefaults", cmd_adddefaults))
    app.add_handler(CommandHandler("resetchannels", cmd_resetchannels))
    app.add_handler(CommandHandler("purge", cmd_purge))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_error_handler(on_error)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("✅ Bot started successfully!")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down…")
    except Exception as e:
        log.exception("FATAL: %s", e)
        print("\n❌ Bot crash ho gaya. Error dekho upar.\n")
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()
