"""
Indian Cyber Hub - OSINT Bot (All Buttons Working)
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
# ⚙️ CONFIG
# ===========================================================================
BOT_TOKEN            = os.getenv("BOT_TOKEN",            "8725925256:AAFNCrMrSUuu8O-q442S17JRfT_xWJzUjbk").strip()
PUBLIC_OSINT_API_URL = os.getenv("PUBLIC_OSINT_API_URL", "https://osint.invalidayushh.workers.dev/numv2").strip()
PUBLIC_OSINT_API_KEY = os.getenv("PUBLIC_OSINT_API_KEY", "Yogixysjisjsn").strip()
DB_PATH              = os.getenv("DB_PATH",              "bot.db").strip() or "bot.db"

OWNER_ID = int(os.getenv("OWNER_ID", "8250721152"))
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
_DB_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


@contextmanager
def _conn():
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
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
        """)


def db_add_channel(username: str, name: str, url: str, added_by: Optional[int] = None) -> bool:
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO required_channels (username, name, url, added_at, added_by)
                VALUES (?, ?, ?, ?, ?)
            """, (username, name, url, _iso(_now()), added_by))
        return True
    except sqlite3.IntegrityError:
        return False


def db_remove_channel(username: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM required_channels WHERE username = ?", (username,))
        return cur.rowcount > 0


def db_list_channels() -> list:
    with _conn() as c:
        return c.execute(
            "SELECT username, name, url, added_at FROM required_channels ORDER BY added_at"
        ).fetchall()


def load_channels() -> None:
    global _REQUIRED_CHANNELS
    rows = db_list_channels()
    if not rows:
        for ch in DEFAULT_CHANNELS:
            db_add_channel(ch["username"], ch["name"], ch["url"], None)
        rows = db_list_channels()

    _REQUIRED_CHANNELS = [
        {"username": r["username"], "name": r["name"], "url": r["url"]}
        for r in rows
    ]
    log.info("Loaded %d required channel(s)", len(_REQUIRED_CHANNELS))


# ------------------- Users / Codes -------------------
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
        return row["credits"] if row else 0


def deduct_credit(user_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET credits = credits - 1 WHERE user_id = ? AND credits > 0",
            (user_id,))
        return cur.rowcount > 0


def list_users(limit: int = 50) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT user_id, username, last_seen, credits FROM users "
            "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()


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

    return True, {"code": code, "credits_added": credits_to_add,
                  "balance": new_bal, "expires_at": exp}


def user_active_key_info(user_id: int) -> Optional[dict]:
    now = _now()
    with _conn() as c:
        rows = c.execute("""
            SELECT ac.code, ac.expires_at, ac.active, ac.uses, ac.max_uses
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
            return {"code": r["code"], "expires_at": exp}
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

    log.info("API call: %s q=%s", PUBLIC_OSINT_API_URL, number)

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

    log.info("API status: %s | body: %s", r.status_code, r.text[:400])

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
        head = "🔐 Access Expired"
        body = "Aapki access key expire ho gayi hai."
    elif reason == "revoked":
        head = "🔐 Access Revoked"
        body = "Aapki access key revoke kar di gayi hai."
    elif reason == "exhausted":
        head = "🔐 Access Exhausted"
        body = "Aapki access key ki maximum uses khatam ho gayi."
    elif reason == "no_credits":
        head = "💎 Credits Khatam"
        body = "Aapke credits khatam ho gaye."
    else:
        head = "🔐 Access Required"
        body = "Is bot ko use karne ke liye valid access key required hai."

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
        [InlineKeyboardButton("⬅️ Back", callback_data="a:panel")],
    ])


def touch_user(update: Update) -> None:
    u = update.effective_user
    if u is not None:
        upsert_user(u.id, u.username or "")


def check_access(user_id: int):
    if is_admin(user_id) and ADMIN_UNLIMITED:
        return True, "admin"
    if user_active_key_info(user_id) is not None:
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
        try:
            member = await context.bot.get_chat_member(
                chat_id=ch["username"], user_id=user_id)

            status = getattr(member, "status", "")
            if hasattr(status, "value"):
                status = status.value
            status = str(status).lower().strip()

            log.info("Channel %s → user %s = %s", ch["username"], user_id, status)

            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted" and getattr(member, "is_member", False):
                continue

            missing.append(ch["name"])

        except Exception as e:
            log.warning("Channel check FAILED [%s] → marking missing. Err: %s",
                        ch["username"], str(e)[:150])
            missing.append(ch["name"])

    return missing


async def _reply(update: Update, text: str,
                 reply_markup=None, prefer_edit: bool = False) -> None:
    """Universal reply — works for messages AND callback queries."""
    try:
        cq = update.callback_query
        if cq and cq.message:
            if prefer_edit:
                try:
                    await cq.message.edit_text(
                        text, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True)
                    return
                except Exception:
                    pass
            await cq.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
                disable_web_page_preview=True)
            return
        if update.message:
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
                disable_web_page_preview=True)
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
    has_key = user_active_key_info(uid) is not None
    admin_flag = "🛡 *You are an ADMIN.*\n\n" if is_admin(uid) else ""

    if not allowed and not is_admin(uid):
        await _reply(update,
            "👋 *Welcome to Indian Cyber Hub – Authorized OSINT Bot*\n\n"
            f"{admin_flag}{access_denied_text(reason)}",
            reply_markup=main_menu(is_admin(uid)))
        return

    mode_line = "♾ *Unlimited* (active key)" if has_key else f"💎 Credits: *{credits}*"
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
                 "`/addchannel` `/removechannel` `/channels` `/debugchannels`")
    # ⭐ FIX: use _reply instead of update.message
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
    await _reply(update,
        "✅ *Access Activated*\n\n"
        f"🔑 Key: `{info['code']}`\n"
        f"♾ Mode: *Unlimited lookups*\n"
        f"⏳ Valid until: `{exp_str}`\n\n"
        "Ab aap **unlimited** `/lookup NUMBER` kar sakte ho.",
        reply_markup=main_menu(is_admin(update.effective_user.id)))


@require_channels
async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ⭐ FIX: use _reply instead of update.message
    touch_user(update)
    uid = update.effective_user.id
    bal = get_credits(uid)
    has_key = user_active_key_info(uid) is not None
    if is_admin(uid) and ADMIN_UNLIMITED:
        text = f"💎 Credits: *{bal}*\n🛡 Admin: *Unlimited*"
    elif has_key:
        text = f"💎 Credits: *{bal}*\n♾ *Unlimited* (active key ke saath)"
    else:
        text = f"💎 Credits: *{bal}*\n\nEk lookup = 1 credit."
    await _reply(update, text, reply_markup=main_menu(is_admin(uid)))


@require_channels
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ⭐ FIX: use _reply instead of update.message
    touch_user(update)
    uid = update.effective_user.id
    info = user_active_key_info(uid)
    allowed, reason = check_access(uid)
    access_line = "🟢 Active" if allowed else "🔴 Inactive"
    key_line = f"`{info['code']}`" if info else "—"
    exp_line = info["expires_at"].strftime("%Y-%m-%d %H:%M UTC") if info else "—"
    mode_line = "♾ Unlimited" if (info or (is_admin(uid) and ADMIN_UNLIMITED)) else "💎 Credit-based"
    text = ("📊 *Your Status*\n\n"
            f"🆔 User ID: `{uid}`\n"
            f"🔐 Access: *{access_line}*\n"
            f"🎟 Active Key: {key_line}\n"
            f"⏳ Key Expiry: `{exp_line}`\n"
            f"📈 Mode: *{mode_line}*\n"
            f"💎 Credits: `{get_credits(uid)}`\n"
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
        text = (
            "⚠️ *Verification Pending*\n\n"
            "Abhi ye channel(s)/group(s) join karna baaki hai:\n\n"
            + "\n".join(f"• {m}" for m in missing)
            + "\n\n👇 Join karke *✅ Verify* dobara dabao."
        )
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

    unlimited_mode = (is_admin(uid) and ADMIN_UNLIMITED) or \
                     (user_active_key_info(uid) is not None)

    if not unlimited_mode:
        if not deduct_credit(uid):
            log_lookup(uid, uname, number, "no_credits")
            await context.bot.send_message(chat.id, access_denied_text("no_credits"),
                                           parse_mode=ParseMode.MARKDOWN)
            return

    msg = await context.bot.send_message(chat.id, "🔎 Processing…")

    try:
        result = await lookup_number(number)
    except LookupError as e:
        log_lookup(uid, uname, number, f"error:{e.kind}")
        if not unlimited_mode:
            add_credits(uid, 1)
        await msg.edit_text(f"⚠️ {e.user_message}\n\n_Credit refunded._",
                            parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        log.exception("lookup failed: %s", e)
        log_lookup(uid, uname, number, "error:unexpected")
        if not unlimited_mode:
            add_credits(uid, 1)
        await msg.edit_text("⚠️ Unexpected error.\n\n_Credit refunded._",
                            parse_mode=ParseMode.MARKDOWN)
        return

    if result in (None, {}, []):
        log_lookup(uid, uname, number, "empty")
        if not unlimited_mode:
            add_credits(uid, 1)
        await msg.edit_text(
            f"ℹ️ Number `{number}` ka koi data nahi mila.\n\n_Credit refunded._",
            parse_mode=ParseMode.MARKDOWN)
        return

    log_lookup(uid, uname, number, "ok")
    body = format_result(result)
    header = f"✅ *Result for* `{number}`\n\n"
    footer = "\n\n♾ *Unlimited access*" if unlimited_mode \
             else f"\n\n💎 Credits left: `{get_credits(uid)}`"

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
                await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
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
            "`/codes` `/users` `/logs` `/stats` `/debugchannels`")
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
            "ℹ️ Public channels: `@username`\n"
            "ℹ️ Private groups: numeric ID (e.g. `-1001234567890`)\n\n"
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
        f"📊 Total required: `{len(_REQUIRED_CHANNELS)}`")


@admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _REQUIRED_CHANNELS:
        await _reply(update,
            "📢 *No required channels.*\n\n"
            "Add one: `/addchannel @username Display Name`")
        return

    lines = [f"📢 *Required Channels* ({len(_REQUIRED_CHANNELS)})", ""]
    for i, ch in enumerate(_REQUIRED_CHANNELS, 1):
        lines.append(f"{i}. {ch['name']}\n   `{ch['username']}`\n   {ch['url']}")
    lines.append("")
    lines.append("➕ Add: `/addchannel @username Name`")
    lines.append("➖ Remove: `/removechannel @username`")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_debugchannels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _REQUIRED_CHANNELS:
        await _reply(update, "📢 No required channels configured.")
        return

    lines = ["🔧 *Channel Debug*", ""]
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
    lines.append("ℹ️ ✅=OK, ⚠️=bot ko admin banao, ❌=username galat / bot add nahi hai")
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
            "Usage: `/newcode [hours] [uses] [credits]`\nExample: `/newcode 24 5 10`")
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
    await _reply(update,
        "✅ *Access Key Generated*\n\n"
        f"🔑 Key:\n`{code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}`\n"
        f"💎 Credits: `{credits}`\n"
        f"📊 Uses: `0/{uses}`\n\n"
        "ℹ️ Activate karne par user ko *unlimited* lookups milenge.")


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
        f"✅ `{target}` ko `{amount:+d}` credits.\n💎 Balance: `{new_bal}`")


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
        lines.append(f"`{r['code']}` – {r['uses']}/{r['max_uses']} – "
                     f"exp {r['expires_at'][:16]} – {icon}")
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
        "👥 *Step 2/3* — Max uses (kitne users activate kar sakte hain)\nExample: `1`",
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
        "💎 *Step 3/3* — Credits per activation (unlimited ke liye `0`)\nExample: `0`",
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
    await update.message.reply_text(
        "✅ *Access Key Generated*\n\n"
        f"🔑 Key:\n`{code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}`\n"
        f"💎 Credits: `{credits}`\n"
        f"📊 Uses: `0/{uses}`\n\n"
        "ℹ️ Activate karne par user ko *unlimited* lookups milenge.",
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
    data = q.data or ""
    uid = update.effective_user.id
    log.info("callback: %s from %s", data, uid)

    # Always answer first to prevent "loading" spinner
    try:
        await q.answer()
    except Exception:
        pass

    # Verify FIRST
    if data == "u:verify":
        touch_user(update)
        await cmd_verify(update, context)
        return

    # Admin callbacks
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
            await _reply(update,
                "🚫 *Revoke Key*\n\nUse: `/revoke ICH-XXXX-XXXX`")
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
                    "➕ Add channel/group\n"
                    "➖ Remove channel/group\n"
                    "📋 View full list")
            await edit_admin_text(update, text, reply_markup=channels_admin_menu())
        elif data == "a:addch_hint":
            await _reply(update,
                "➕ *Add Channel/Group*\n\n"
                "Use command:\n"
                "`/addchannel @username Display Name`\n\n"
                "*Examples:*\n"
                "`/addchannel @mychannel My Channel`\n"
                "`/addchannel @mychannel My Channel https://t.me/mychannel`\n"
                "`/addchannel -1001234567890 Private Group https://t.me/+abc123`\n\n"
                "⚠️ Bot ko us channel/group me *admin* banana zaroori hai.")
        elif data == "a:removech_hint":
            await _reply(update,
                "➖ *Remove Channel*\n\n"
                "Use command:\n"
                "`/removechannel @username`\n\n"
                "Example: `/removechannel @mychannel`\n\n"
                "📋 Full list dekhne ke liye: `/channels`")
        elif data == "a:listchannels":
            await cmd_channels(update, context)
        elif data == "a:back":
            text = "🏠 *Main Menu*\n\nNumber bhejo ya menu use karo 👇"
            try:
                await q.message.edit_text(
                    text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu(is_admin(uid)))
            except Exception:
                await _reply(update, text, reply_markup=main_menu(is_admin(uid)))
        return

    # User callbacks — strict channel gate
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


# ===========================================================================
# ERROR HANDLER
# ===========================================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception", exc_info=context.error)


# ===========================================================================
# MAIN
# ===========================================================================
async def run() -> None:
    init_db()
    load_channels()

    log.info("Owner: %s | Admins: %s", OWNER_ID, ADMIN_IDS)
    log.info("Channels (%d): %s", len(_REQUIRED_CHANNELS),
             [c["username"] for c in _REQUIRED_CHANNELS])

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

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_error_handler(on_error)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("✅ Bot started!")

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


if __name__ == "__main__":
    main()
