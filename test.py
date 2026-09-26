"""
Indian Cyber Hub - Authorized/Public-Data OSINT Bot
Config pre-set. Just run: python bot.py
Hidden fields: expiry_date, days_left, developer, updates
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
from typing import Any, Optional

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ===========================================================================
# 🔧 CONFIG — ALL PRE-SET (env override optional)
# ===========================================================================
BOT_TOKEN            = os.getenv("BOT_TOKEN",            "8725925256:AAFNCrMrSUuu8O-q442S17JRfT_xWJzUjbk").strip()
PUBLIC_OSINT_API_URL = os.getenv("PUBLIC_OSINT_API_URL", "https://osint.invalidayushh.workers.dev/numv2").strip()
PUBLIC_OSINT_API_KEY = os.getenv("PUBLIC_OSINT_API_KEY", "Yogixysjisjsn").strip()
DB_PATH              = os.getenv("DB_PATH",              "bot.db").strip() or "bot.db"

_env_owner = os.getenv("OWNER_ID", "8250721152").strip()
OWNER_ID: int = int(_env_owner) if _env_owner.isdigit() else 8250721152

_env_admins = os.getenv("ADMIN_IDS", "8250721152").strip()
ADMIN_IDS: set[int] = set()
if _env_admins:
    for x in _env_admins.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

SUPER_ADMIN_NAME      = "@indiancyberhub247"
SUPER_ADMIN_LINK      = "https://t.me/indiancyberhub247"
DEFAULT_CREDITS       = 0
ADMIN_UNLIMITED       = True
MAX_MSG_LEN           = 3800
SHOW_RAW_API_RESPONSE = True

# 🔒 Ye 4 keys user ko kabhi nahi dikhengi
HIDDEN_API_KEYS = {
    "expiry_date",
    "days_left",
    "developer",
    "updates",
}


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN or "PASTE_" in BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("OWNER_ID / ADMIN_IDS")
    if not PUBLIC_OSINT_API_URL:
        missing.append("PUBLIC_OSINT_API_URL")
    if missing:
        raise RuntimeError("Missing config: " + ", ".join(missing))


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
        c.executescript(
            """
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
            """
        )
        for sql in (
            "ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE access_codes ADD COLUMN credits INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE lookup_logs ADD COLUMN username TEXT",
            "ALTER TABLE user_codes ADD COLUMN expires_at TEXT",
        ):
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass


def upsert_user(user_id: int, username: str) -> None:
    now = _iso(_now())
    with _conn() as c:
        c.execute(
            """
            INSERT INTO users (user_id, first_seen, last_seen, username, credits)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                username  = excluded.username
            """,
            (user_id, now, now, username or "", DEFAULT_CREDITS),
        )


def get_credits(user_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def add_credits(user_id: int, amount: int) -> int:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO users (user_id, first_seen, last_seen, username, credits)
            VALUES (?, ?, ?, '', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                credits = MAX(0, credits + ?)
            """,
            (user_id, _iso(_now()), _iso(_now()), max(0, amount), amount),
        )
        row = c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def deduct_credit(user_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET credits = credits - 1 WHERE user_id = ? AND credits > 0",
            (user_id,),
        )
        return cur.rowcount > 0


def list_users(limit: int = 50) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT user_id, username, last_seen, credits FROM users "
            "ORDER BY last_seen DESC LIMIT ?", (limit,),
        ).fetchall()


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
                c.execute(
                    """
                    INSERT INTO access_codes
                        (code, created_at, expires_at, max_uses, uses, credits, active, created_by)
                    VALUES (?, ?, ?, ?, 0, ?, 1, ?)
                    """,
                    (code, _iso(now), _iso(expires), max_uses, credits_per_use, created_by),
                )
                return code
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("Could not generate unique code")


def revoke_code(code: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE access_codes SET active = 0 WHERE code = ? AND active = 1",
            (code.upper(),),
        )
        return cur.rowcount > 0


def list_codes(limit: int = 30) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT code, created_at, expires_at, max_uses, uses, credits, active "
            "FROM access_codes ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()


def _code_is_valid(row: sqlite3.Row) -> bool:
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


def activate_code(user_id: int, code: str) -> tuple[bool, dict | str]:
    code = code.upper().strip()
    now = _now()
    with _conn() as c:
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass

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
            return False, "Key invalid (bad expiry)."
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False, "Key expired."

        already = c.execute(
            "SELECT 1 FROM user_codes WHERE user_id = ? AND code = ?",
            (user_id, code),
        ).fetchone()
        if already is not None:
            return False, "Aap ye key pehle hi activate kar chuke ho."

        credits_to_add = int(row["credits"] if row["credits"] is not None else 1)

        c.execute(
            "INSERT INTO user_codes (user_id, code, activated_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, code, _iso(now), _iso(exp)),
        )
        c.execute("UPDATE access_codes SET uses = uses + 1 WHERE code = ?", (code,))
        c.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (credits_to_add, user_id),
        )
        new_bal = c.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()["credits"]

    return True, {
        "code": code,
        "credits_added": credits_to_add,
        "balance": new_bal,
        "expires_at": exp,
    }


def user_active_key_info(user_id: int) -> Optional[dict]:
    now = _now()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT ac.code, ac.expires_at, ac.active, ac.uses, ac.max_uses,
                   uc.activated_at
            FROM user_codes uc
            JOIN access_codes ac ON ac.code = uc.code
            WHERE uc.user_id = ?
            ORDER BY uc.activated_at DESC
            """,
            (user_id,),
        ).fetchall()
    for r in rows:
        if not r["active"]:
            continue
        if r["uses"] > r["max_uses"]:
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
            (user_id, cutoff),
        ).fetchone()["n"]
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
            (user_id, username or "", query, status, _iso(_now())),
        )


def list_logs(limit: int = 30) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT user_id, username, query, status, timestamp FROM lookup_logs "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()


def get_stats() -> dict:
    day_ago = _iso(_now() - timedelta(hours=24))
    with _conn() as c:
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_codes = c.execute("SELECT COUNT(*) AS n FROM access_codes").fetchone()["n"]
        active_codes = sum(
            1 for r in c.execute("SELECT * FROM access_codes WHERE active = 1").fetchall()
            if _code_is_valid(r)
        )
        lookups = c.execute("SELECT COUNT(*) AS n FROM lookup_logs").fetchone()["n"]
        lookups_ok = c.execute(
            "SELECT COUNT(*) AS n FROM lookup_logs WHERE status = 'ok'"
        ).fetchone()["n"]
        lookups_24h = c.execute(
            "SELECT COUNT(*) AS n FROM lookup_logs WHERE timestamp >= ?", (day_ago,)
        ).fetchone()["n"]
    return {
        "users": users, "total_codes": total_codes, "active_codes": active_codes,
        "lookups": lookups, "lookups_ok": lookups_ok, "lookups_24h": lookups_24h,
    }


# ===========================================================================
# API CLIENT
# ===========================================================================
ALLOWED_KEYS = {
    "country", "country_code", "region", "carrier", "line_type",
    "number_type", "timezone", "valid", "e164", "national_format",
    "international_format", "location", "mcc", "mnc", "status",
    "message", "source", "name", "operator", "circle", "state",
    "city", "alternate", "alt", "phone", "mobile", "number",
    "id", "reference", "ref_id", "result", "data",
}


class LookupError(Exception):
    def __init__(self, kind: str, user_message: str):
        super().__init__(kind)
        self.kind = kind
        self.user_message = user_message


def _scrub_hidden(obj: Any) -> Any:
    """Recursively remove hidden keys (case-insensitive) from dicts/lists."""
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in HIDDEN_API_KEYS:
                continue
            cleaned[k] = _scrub_hidden(v)
        return cleaned
    if isinstance(obj, list):
        return [_scrub_hidden(x) for x in obj]
    return obj


def _do_request(number: str) -> Any:
    params = {"q": number}
    if PUBLIC_OSINT_API_KEY:
        params["key"] = PUBLIC_OSINT_API_KEY

    log.info("API call: %s?q=%s&key=***", PUBLIC_OSINT_API_URL, number)

    try:
        r = requests.get(
            PUBLIC_OSINT_API_URL, params=params, timeout=20,
            headers={"Accept": "application/json",
                     "User-Agent": "IndianCyberHub-OSINT/1.0"},
        )
    except requests.Timeout:
        raise LookupError("timeout", "API timed out. Try again.")
    except requests.ConnectionError as e:
        log.error("Connection error: %s", e)
        raise LookupError("connection", "API unreachable. Try later.")
    except requests.RequestException as e:
        log.error("Request error: %s", e)
        raise LookupError("request", "API request failed.")

    log.info("API status: %s | body[:600]: %s", r.status_code, r.text[:600])

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

    # 🔒 Hidden keys scrub
    payload = _scrub_hidden(payload)

    if SHOW_RAW_API_RESPONSE:
        return payload

    def _flatten(d: dict, depth: int = 0) -> dict:
        if depth > 2:
            return {}
        out: dict[str, Any] = {}
        for k, v in d.items():
            kl = k.lower()
            if isinstance(v, dict):
                for sk, sv in _flatten(v, depth + 1).items():
                    out[f"{k}.{sk}"] = sv
            elif isinstance(v, list):
                if v and all(not isinstance(x, (dict, list)) for x in v):
                    out[k] = ", ".join(str(x) for x in v[:10])
            elif kl in ALLOWED_KEYS:
                out[k] = v
        return out

    if isinstance(payload, dict):
        for key in ("data", "result", "results", "response", "info"):
            if key in payload and isinstance(payload[key], dict):
                payload = payload[key]
                break
        return _flatten(payload)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return _flatten(payload[0])
    return payload


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
    return (
        "🎟 Access Key ke liye DM kare:\n"
        f"[{SUPER_ADMIN_NAME}]({SUPER_ADMIN_LINK})"
    )


def access_denied_text(reason: str = "no_key") -> str:
    if reason == "expired":
        head = "🔐 Access Expired"
        body = "Aapki access key expire ho gayi hai. Naya key lene ke liye contact kare."
    elif reason == "revoked":
        head = "🔐 Access Revoked"
        body = "Aapki access key revoke kar di gayi hai. Naya key lene ke liye contact kare."
    elif reason == "exhausted":
        head = "🔐 Access Exhausted"
        body = "Aapki access key ki maximum uses khatam ho gayi. Naya key lene ke liye contact kare."
    elif reason == "no_credits":
        head = "💎 Credits Khatam"
        body = "Aapke credits khatam ho gaye. Naya key activate kare ya admin se credits le."
    else:
        head = "🔐 Access Required"
        body = "Is bot ko use karne ke liye valid access key required hai."

    return (
        f"*{head}*\n\n"
        f"{body}\n\n"
        "Key milne ke baad:\n"
        "`/activate YOUR-KEY`\n\n"
        f"{contact_block()}\n\n"
        "⚠️ Bina valid key ke lookup available nahi hai."
    )


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
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="a:back")],
    ])


def touch_user(update: Update) -> None:
    u = update.effective_user
    if u is not None:
        upsert_user(u.id, u.username or "")


def check_access(user_id: int) -> tuple[bool, str]:
    if is_admin(user_id) and ADMIN_UNLIMITED:
        return True, "admin"
    info = user_active_key_info(user_id)
    if info is not None:
        return True, "key"
    if get_credits(user_id) > 0:
        return True, "credits"
    return False, "no_key"


# ===========================================================================
# USER COMMANDS
# ===========================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    credits = get_credits(uid)
    allowed, reason = check_access(uid)
    has_key = user_active_key_info(uid) is not None
    admin_flag = "🛡 *You are an ADMIN.*\n\n" if is_admin(uid) else ""

    if not allowed and not is_admin(uid):
        text = (
            "👋 *Welcome to Indian Cyber Hub – Authorized OSINT Bot*\n\n"
            f"{admin_flag}"
            f"{access_denied_text(reason)}"
        )
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu(is_admin(uid))
        )
        return

    mode_line = "♾ *Unlimited* (active key)" if has_key else f"💎 Credits: *{credits}*"
    text = (
        "👋 *Welcome to Indian Cyber Hub – Authorized OSINT Bot*\n\n"
        f"{admin_flag}"
        "📱 *Bas number bhejo — result milega.*\n\n"
        "Examples:\n"
        "`9876543210`\n"
        "`+919876543210`\n\n"
        f"{mode_line}\n\n"
        "• /activate CODE – key activate\n"
        "• /credits – balance\n"
        "• /status – status\n"
        "• /help – help"
    )
    if is_admin(uid):
        text += "\n\n🛡 `/admin` – Admin Panel"
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu(is_admin(uid))
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    text = (
        "ℹ️ *Help*\n\n"
        "*Kaise use karein:*\n"
        "1️⃣ Access key activate karo: `/activate ICH-XXXX-XXXX`\n"
        "2️⃣ Phir number bhejo:\n"
        "`9876543210`\n"
        "`+919876543210`\n\n"
        "*Commands:*\n"
        "`/start` – main menu\n"
        "`/activate CODE` – key activate\n"
        "`/lookup NUMBER` – lookup\n"
        "`/credits` – balance\n"
        "`/status` – full status\n"
        "`/help` – ye message\n\n"
        f"{contact_block()}"
    )
    if is_admin(uid):
        text += (
            "\n\n*Admin Commands:*\n"
            "`/admin` – panel\n"
            "`/newcode [hours] [uses] [credits]`\n"
            "`/addcredits USER_ID AMOUNT`\n"
            "`/revoke CODE`\n`/codes`\n`/users`\n`/logs`\n`/stats`"
        )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu(is_admin(uid))
    )


async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    if not context.args:
        await update.message.reply_text(
            "Usage: `/activate ICH-XXXX-XXXX`\n\n"
            f"{contact_block()}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code = context.args[0].strip().upper()
    ok, info = activate_code(update.effective_user.id, code)

    if not ok:
        err = info if isinstance(info, str) else "Invalid key."
        await update.message.reply_text(
            "❌ *Invalid Access Key*\n\n"
            f"Reason: {err}\n\n"
            "Possible reasons:\n"
            "• Key does not exist\n"
            "• Key expired\n"
            "• Key revoked\n"
            "• Maximum uses reached\n"
            "• Already activated by you\n\n"
            f"{contact_block()}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    exp_str = info["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
    await update.message.reply_text(
        "✅ *Access Activated*\n\n"
        f"🔑 Key: `{info['code']}`\n"
        f"♾ Mode: *Unlimited lookups*\n"
        f"⏳ Access valid until: `{exp_str}`\n\n"
        "Ab aap **unlimited** `/lookup NUMBER` kar sakte ho jab tak key valid hai.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu(is_admin(uid))
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    uid = update.effective_user.id
    info = user_active_key_info(uid)
    allowed, reason = check_access(uid)

    access_line = "🟢 Active" if allowed else "🔴 Inactive"
    key_line = f"`{info['code']}`" if info else "—"
    exp_line = info["expires_at"].strftime("%Y-%m-%d %H:%M UTC") if info else "—"
    mode_line = "♾ Unlimited" if (info or (is_admin(uid) and ADMIN_UNLIMITED)) else "💎 Credit-based"

    text = (
        "📊 *Your Status*\n\n"
        f"🆔 User ID: `{uid}`\n"
        f"🔐 Access: *{access_line}*\n"
        f"🎟 Active Key: {key_line}\n"
        f"⏳ Key Expiry: `{exp_line}`\n"
        f"📈 Mode: *{mode_line}*\n"
        f"💎 Credits: `{get_credits(uid)}`\n"
        f"🛡 Admin: `{'Yes' if is_admin(uid) else 'No'}`"
    )
    if not allowed:
        text += f"\n\n_{reason}_"
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu(is_admin(uid))
    )


# ===========================================================================
# LOOKUP
# ===========================================================================
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
        await context.bot.send_message(
            chat.id, access_denied_text(reason), parse_mode=ParseMode.MARKDOWN,
        )
        return

    is_admin_unlim = is_admin(uid) and ADMIN_UNLIMITED
    has_valid_key  = user_active_key_info(uid) is not None
    unlimited_mode = is_admin_unlim or has_valid_key

    if not unlimited_mode:
        if not deduct_credit(uid):
            log_lookup(uid, uname, number, "no_credits")
            await context.bot.send_message(chat.id, access_denied_text("no_credits"),
                                           parse_mode=ParseMode.MARKDOWN)
            return

    msg = await context.bot.send_message(chat.id, "🔎 API se query kar raha hoon…")

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

    if result is None or result == {} or result == []:
        log_lookup(uid, uname, number, "empty")
        if not unlimited_mode:
            add_credits(uid, 1)
        await msg.edit_text(
            f"ℹ️ Number `{number}` ka koi data nahi mila.\n\n_Credit refunded._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    log_lookup(uid, uname, number, "ok")
    body = format_result(result)
    header = f"✅ *Result for* `{number}`\n\n"

    if unlimited_mode:
        footer = "\n\n♾ *Unlimited access*"
    else:
        footer = f"\n\n💎 Credits left: `{get_credits(uid)}`"

    full = header + "```\n" + body + "\n```" + footer

    if len(full) > MAX_MSG_LEN:
        await msg.edit_text(header + "📄 Result lamba hai, parts me bhej raha hoon…",
                            parse_mode=ParseMode.MARKDOWN)
        chunks = []
        cur = ""
        for line in body.splitlines():
            if len(cur) + len(line) + 1 > MAX_MSG_LEN - 40:
                chunks.append(cur)
                cur = line
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
            await context.bot.send_message(chat.id, footer, parse_mode=ParseMode.MARKDOWN)
    else:
        try:
            await msg.edit_text(full, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await msg.edit_text(header + body + footer)


async def cmd_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    if not context.args:
        await update.message.reply_text(
            "Usage: `/lookup 9876543210`\n\nYa direct number bhi bhej sakte ho.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    number = validate_number(context.args[0])
    if not number:
        await update.message.reply_text(
            "❌ Invalid number. Example: `9876543210`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await do_lookup(update, context, number)


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_user(update)
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    if not looks_like_number(text):
        await update.message.reply_text(
            "🤔 Ye number nahi lagta.\n\nNumber bhejo aise:\n`9876543210`\n`+919876543210`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    number = validate_number(text)
    if not number:
        await update.message.reply_text("❌ Number format galat hai.")
        return

    await do_lookup(update, context, number)


# ===========================================================================
# ADMIN helpers
# ===========================================================================
async def send_admin_text(update: Update, text: str,
                          reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=reply_markup)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


async def edit_admin_text(update: Update, text: str,
                          reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return
        except Exception as e:
            log.warning("edit failed, sending new: %s", e)
    await send_admin_text(update, text, reply_markup=reply_markup)


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if u is None or not is_admin(u.id):
            if update.message:
                await update.message.reply_text("⛔ Unauthorized.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
            return
        touch_user(update)
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛡 *ADMIN PANEL*\n\n"
        f"👑 Owner ID: `{OWNER_ID}`\n"
        f"👥 Admins: `{len(ADMIN_IDS)}`\n\n"
        "🎟 Generate Access Key\n📋 Active Keys\n🚫 Revoke Key\n"
        "👥 Users\n💎 Add Credits\n🔎 Lookup Logs\n📊 Statistics\n\n"
        "*Commands*\n"
        "`/newcode [hours] [uses] [credits]`\n"
        "`/addcredits USER_ID AMOUNT`\n"
        "`/revoke CODE`\n`/codes`\n`/users`\n`/logs`\n`/stats`"
    )
    if update.callback_query:
        await edit_admin_text(update, text, reply_markup=admin_menu())
    else:
        await send_admin_text(update, text, reply_markup=admin_menu())


@admin_only
async def cmd_newcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hours, uses, credits = 24, 5, 1
    args = context.args or []
    try:
        if len(args) >= 1: hours = int(args[0])
        if len(args) >= 2: uses = int(args[1])
        if len(args) >= 3: credits = int(args[2])
    except ValueError:
        await send_admin_text(
            update,
            "Usage: `/newcode [hours] [uses] [credits]`\nExample: `/newcode 24 5 10`",
        )
        return
    if not (1 <= hours <= 24 * 365 * 100):
        await send_admin_text(update, "Hours: 1–876000"); return
    if not (1 <= uses <= 10000):
        await send_admin_text(update, "Uses: 1–10000"); return
    if not (0 <= credits <= 100000):
        await send_admin_text(update, "Credits: 0–100000"); return

    code = create_code(hours=hours, max_uses=uses,
                       credits_per_use=credits,
                       created_by=update.effective_user.id)

    await send_admin_text(
        update,
        "✅ *Access Key Generated*\n\n"
        f"🔑 Key:\n`{code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}`\n"
        f"💎 Credits: `{credits}`\n"
        f"📊 Uses: `0/{uses}`\n\n"
        "ℹ️ Activate karne par user ko *unlimited* lookups milenge (jab tak key valid).",
    )


@admin_only
async def cmd_addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_admin_text(
            update,
            "Usage: `/addcredits USER_ID AMOUNT`\nExample: `/addcredits 8250721152 10`",
        )
        return
    try:
        target = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await send_admin_text(update, "Numbers do.")
        return
    if amount == 0:
        await send_admin_text(update, "Amount 0 nahi.")
        return
    new_bal = add_credits(target, amount)
    await send_admin_text(
        update,
        f"✅ `{target}` ko `{amount:+d}` credits.\n💎 Balance: `{new_bal}`",
    )


@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_admin_text(update, "Usage: `/revoke ICH-XXXX-XXXX`")
        return
    ok = revoke_code(context.args[0].strip().upper())
    await send_admin_text(update, "✅ Revoked." if ok else "❌ Not found.")


@admin_only
async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_codes(limit=30)
    if not rows:
        await send_admin_text(update, "No keys. Use `/newcode 24 1 0`.")
        return
    lines = ["📋 *Recent Access Keys*", ""]
    for r in rows:
        status_icon = "✅" if (r["active"] and _code_is_valid(r)) else "❌"
        lines.append(
            f"`{r['code']}` – {r['uses']}/{r['max_uses']} – "
            f"exp {r['expires_at'][:16]} – {status_icon}"
        )
    await send_admin_text(update, "\n".join(lines))


@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_users(limit=50)
    if not rows:
        await send_admin_text(update, "No users.")
        return
    lines = ["👥 *Recent Users*", ""]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(
            f"`{r['user_id']}` – {uname} – 💎{r['credits']} – {r['last_seen'][:16]}"
        )
    await send_admin_text(update, "\n".join(lines))


@admin_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_logs(limit=30)
    if not rows:
        await send_admin_text(update, "No lookups.")
        return
    lines = ["🔎 *Recent Lookups*", ""]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(
            f"`{r['timestamp'][:16]}` – `{r['user_id']}` {uname} – "
            f"`{r['query']}` – {r['status']}"
        )
    await send_admin_text(update, "\n".join(lines))


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_stats()
    text = (
        "📊 *Statistics*\n\n"
        f"Users: `{s['users']}`\n"
        f"Active keys: `{s['active_codes']}`\n"
        f"Total keys: `{s['total_codes']}`\n"
        f"Lookups (total): `{s['lookups']}`\n"
        f"Lookups (ok): `{s['lookups_ok']}`\n"
        f"Lookups (24h): `{s['lookups_24h']}`"
    )
    await send_admin_text(update, text)


# ===========================================================================
# GENERATE KEY — 3-step Conversation
# ===========================================================================
GEN_HOURS, GEN_USES, GEN_CREDITS = range(3)


async def gen_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    if not is_admin(uid):
        await q.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data["gen"] = {}
    await q.message.reply_text(
        "🎟 *Generate Access Key* — Step 1/3\n\n"
        "Validity hours bhejein (e.g. `720` = 30 din).\n"
        "Cancel: /cancel",
        parse_mode=ParseMode.MARKDOWN,
    )
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
        "👥 *Step 2/3* — Max uses (kitne users activate kar sakte hain)\n"
        "Example: `1`",
        parse_mode=ParseMode.MARKDOWN,
    )
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
        "💎 *Step 3/3* — Credits per activation (unlimited ke liye `0`)\n"
        "Example: `0`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return GEN_CREDITS


async def gen_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or not (0 <= int(txt) <= 100000):
        await update.message.reply_text("❌ 0–100000 ke beech integer bhejo.")
        return GEN_CREDITS

    g = context.user_data.pop("gen", {})
    hours = g.get("hours", 24)
    uses = g.get("uses", 5)
    credits = int(txt)

    code = create_code(hours=hours, max_uses=uses,
                       credits_per_use=credits,
                       created_by=update.effective_user.id)

    await update.message.reply_text(
        "✅ *Access Key Generated*\n\n"
        f"🔑 Key:\n`{code}`\n\n"
        f"⏳ Validity: `{hours} hours`\n"
        f"👥 Max Uses: `{uses}`\n"
        f"💎 Credits: `{credits}`\n"
        f"📊 Uses: `0/{uses}`\n\n"
        "ℹ️ Activate karne par user ko *unlimited* lookups milenge (jab tak key valid).",
        parse_mode=ParseMode.MARKDOWN,
    )
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
    await q.answer()
    touch_user(update)
    data = q.data or ""
    uid = update.effective_user.id

    log.info("callback: %s from %s", data, uid)

    # USER
    if data == "u:howlookup":
        await q.message.reply_text(
            "📱 *Kaise lookup karein:*\n\n"
            "1️⃣ Pehle access key activate karo:\n"
            "`/activate ICH-XXXX-XXXX`\n\n"
            "2️⃣ Phir number bhejo:\n"
            "`9876543210`\n"
            "`+919876543210`\n\n"
            "Ya `/lookup 9876543210` bhi chalta hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "u:activate":
        await q.message.reply_text(
            "Apna key bhejo:\n`/activate ICH-ABCD-1234`\n\n"
            f"{contact_block()}",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "u:status":
        await cmd_status(update, context)
    elif data == "u:credits":
        await cmd_credits(update, context)
    elif data == "u:help":
        await cmd_help(update, context)

    # ADMIN
    elif data == "a:panel":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        await cmd_admin(update, context)
    elif data == "a:codes":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await cmd_codes(update, context)
    elif data == "a:revoke":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await q.message.reply_text(
            "🚫 *Revoke Key*\n\nUse: `/revoke ICH-XXXX-XXXX`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "a:users":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await cmd_users(update, context)
    elif data == "a:addcredits":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await q.message.reply_text(
            "💎 *Add Credits*\n\nUse: `/addcredits USER_ID AMOUNT`\n"
            "Example: `/addcredits 8250721152 10`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "a:logs":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await cmd_logs(update, context)
    elif data == "a:stats":
        if not is_admin(uid):
            await q.answer("⛔ Unauthorized", show_alert=True); return
        await cmd_stats(update, context)
    elif data == "a:back":
        try:
            await q.message.edit_text(
                "🏠 *Main Menu*\n\nNumber bhejo ya menu use karo 👇",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu(is_admin(uid)),
            )
        except Exception:
            await q.message.reply_text(
                "🏠 *Main Menu*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu(is_admin(uid)),
            )


# ===========================================================================
# ERROR HANDLER
# ===========================================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception", exc_info=context.error)


# ===========================================================================
# MAIN
# ===========================================================================
async def run() -> None:
    validate_config()
    init_db()

    log.info("Owner ID: %s | Admins: %s", OWNER_ID, ADMIN_IDS)
    log.info("API: %s", PUBLIC_OSINT_API_URL)
    log.info("Hidden keys: %s", sorted(HIDDEN_API_KEYS))

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 1) Generate-Key Conversation FIRST
    gen_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(gen_entry, pattern=r"^a:newcode$")],
        states={
            GEN_HOURS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_hours)],
            GEN_USES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_uses)],
            GEN_CREDITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_credits)],
        },
        fallbacks=[CommandHandler("cancel", gen_cancel)],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(gen_conv)

    # 2) Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("credits", cmd_credits))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("newcode", cmd_newcode))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("codes", cmd_codes))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # 3) Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # 4) Plain text (LAST)
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


if __name__ == "__main__":
    main()