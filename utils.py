# utils.py — Utility functions: formatting, parsing, text helpers

import re
import logging
from datetime import datetime, timezone, timedelta
from config import GRACE_SECONDS

log = logging.getLogger("relay")


# ── Text escaping ────────────────────────────────────────────────────────────

def md(text: str) -> str:
    """Escape user-supplied text for Telegram Markdown mode."""
    for ch in ("\\", "*", "_", "`", "[", "]", "(", ")"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Time formatting ───────────────────────────────────────────────────────────

def fmt_time(seconds: int) -> str:
    if seconds <= 0:
        return "0 min"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    if m and s:
        return f"{m}m {s}s"
    if m:
        return f"{m} min"
    return f"{s}s"


def time_bar(seconds: int, total: int = GRACE_SECONDS) -> str:
    if seconds <= 0:
        return "▱▱▱▱▱▱▱▱▱▱ 0%"
    pct    = min(100, int(seconds / total * 100)) if total > 0 else 100
    filled = pct // 10
    return "▰" * filled + "▱" * (10 - filled) + f" {pct}%"


# ── Duration parsing ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds"
    r"|m|min|mins|minute|minutes"
    r"|h|hr|hrs|hour|hours"
    r"|d|day|days)",
    re.IGNORECASE,
)


def parse_duration(text: str):
    """Parse a human duration string and return total seconds, or None if invalid."""
    text = text.strip()
    matches = _DURATION_RE.findall(text)
    if not matches:
        return None
    total = 0
    for val_str, unit in matches:
        val  = float(val_str)
        unit = unit.lower()
        if unit.startswith("s"):
            total += int(val)
        elif unit.startswith("m"):
            total += int(val * 60)
        elif unit.startswith("h"):
            total += int(val * 3600)
        elif unit.startswith("d"):
            total += int(val * 86400)
    return total if total > 0 else None


def parse_del_time(arg: str):
    """Parse /del time argument (supports only seconds and minutes)."""
    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)",
        arg.strip(), re.IGNORECASE,
    )
    if not m:
        return None
    val  = float(m.group(1))
    unit = m.group(2).lower()
    return int(val * 60) if unit.startswith("m") else int(val)


# ── Link stripping ────────────────────────────────────────────────────────────

_URL_RE = re.compile(
    r"(?:https?://\S+"
    r"|www\.\S+"
    r"|t\.me/\S+"
    r"|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+"
    r"(?:com|net|org|io|me|co|info|biz|xyz|ru|ir|tv|link|club|site|online|"
    r"shop|store|app|dev|gg|cc|to|us|uk|ca)\b(?:/\S*)?)",
    re.IGNORECASE,
)


def contains_link(text: str) -> bool:
    if not text:
        return False
    return _URL_RE.search(text) is not None


def strip_links(text: str) -> str:
    if not text:
        return text
    cleaned = _URL_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ── User info text builders ───────────────────────────────────────────────────

def user_info_text(u, ref_count: int = 0, media_count: int = 0) -> str:
    from database import _row_access_secs, _row_is_unlimited
    name      = u["display_name"] or u["random_id"]
    raw_uname = u["username"] if u["username"] else ""
    uname     = f"@{raw_uname}" if raw_uname else "—"

    if u["role"] >= 2:
        role_str = "👑 Main Admin"
    elif u["role"] >= 1:
        role_str = "🛡️ Admin"
    elif _row_is_unlimited(u):
        role_str = "♾️ User (Unlimited)"
    else:
        role_str = "👤 User"

    status_str = (
        "🔴 Banned"    if u["is_banned"]
        else "💤 Inactive" if not u["active"]
        else "🟢 Active"
    )

    if u["role"] >= 1:
        time_str = "∞ Unlimited (Admin)"
        bar_str  = ""
    elif _row_is_unlimited(u):
        time_str = "♾️ Unlimited (Gifted)"
        bar_str  = ""
    else:
        secs     = _row_access_secs(u)
        time_str = fmt_time(secs) if secs > 0 else "⌛ Expired"
        bar_str  = f"\n`{time_bar(secs)}`"

    joined = (u["joined_at"] or "")[:10] or "—"
    last   = (u["last_seen"]  or "")[:10] or "—"

    ref_line   = f"\n🔗 Referrals: *{ref_count}* successful" if ref_count else ""
    media_line = f"\n🎞 Media sent: *{media_count}*" if media_count else ""

    return (
        f"👤 *User Info*\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"🏷 Name: *{md(name)}*\n"
        f"🆔 ID: `{u['random_id']}`\n"
        f"📱 Username: {md(uname)}\n"
        f"🎭 Role: {role_str}\n"
        f"📶 Status: {status_str}\n"
        f"⏱ Time: *{time_str}*{bar_str}"
        f"{ref_line}"
        f"{media_line}\n\n"
        f"📅 Joined: `{joined}`\n"
        f"🕐 Last seen: `{last}`"
    )


def admin_panel_text(s) -> str:
    unlimited_line = f"\n♾️ Unlimited users:   *{s.get('unlimited', 0)}*" if s.get('unlimited', 0) > 0 else ""
    return (
        "🛡 *Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total users:       *{s['total']}*\n"
        f"🟢 Can receive media: *{s.get('eligible_active', s['active'])}*\n"
        f"🔇 Muted:             *{s.get('muted', 0)}*\n"
        f"⏳ Expired (no time): *{s.get('expired', 0)}*\n"
        f"🔴 Banned:            *{s['banned']}*\n"
        f"🕐 Active (24h):      *{s['recent_24h']}*\n"
        f"🛡 Admins:            *{s['admins']}*"
        f"{unlimited_line}\n"
        f"💾 Media backups:     *{s.get('backups', 0)}*\n"
        f"🔗 Referrals:         *{s.get('referrals', 0)}*\n"
        "━━━━━━━━━━━━━━━━━"
    )


def media_settings_text(ms) -> str:
    def _t(f): return "✅" if f else "❌"
    mn_s = f"{ms['min_video_bytes'] // 1048576} MB" if ms["min_video_bytes"] else "no limit"
    mx_s = f"{ms['max_video_bytes'] // 1048576} MB" if ms["max_video_bytes"] else "no limit"
    return (
        "⚙️ *Media Settings*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"{_t(ms['allow_text'])} Text   {_t(ms['allow_photo'])} Photo   {_t(ms['allow_video'])} Video\n"
        f"{_t(ms['allow_animation'])} GIF   {_t(ms['allow_sticker'])} Sticker   {_t(ms['allow_voice'])} Voice\n"
        f"{_t(ms['allow_audio'])} Audio   {_t(ms['allow_document'])} File\n\n"
        f"📹 Video size: min {mn_s} — max {mx_s}"
    )


def mute_duration_text(seconds: int) -> str:
    return fmt_time(seconds)


_UNIT_TO_SECS = {"s": 1, "m": 60, "h": 3600}


def mute_builder_text(u, unit: str, value: int) -> str:
    name = (u["display_name"] or u["random_id"]) if u else "Unknown"
    secs = value * _UNIT_TO_SECS.get(unit, 1)
    return (
        "🔇 *Mute User*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"Target: *{md(name)}*\n"
        f"Duration: *{fmt_time(secs)}*\n\n"
        "Pick a unit and amount below, then confirm."
    )


def usage_time_builder_text(u, direction: str, unit: str, value: int) -> str:
    name = (u["display_name"] or u["random_id"]) if u else "Unknown"
    secs = value * (60 if unit == "m" else 3600)
    verb = "Increase" if direction == "add" else "Decrease"
    sign = "+" if direction == "add" else "-"
    return (
        "⏰ *Adjust Usage Time*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"Target: *{md(name)}*\n"
        f"Action: *{verb}* balance by *{sign}{fmt_time(secs)}*\n\n"
        "Pick a direction, unit and amount below, then confirm."
    )


# ── Broadcast styling ─────────────────────────────────────────────────────────

_BOLD_MAP = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _BOLD_MAP[_c] = chr(0x1D400 + _i)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BOLD_MAP[_c] = chr(0x1D41A + _i)
for _i, _c in enumerate("0123456789"):
    _BOLD_MAP[_c] = chr(0x1D7CE + _i)


def to_big_bold(text: str) -> str:
    return "".join(_BOLD_MAP.get(ch, ch) for ch in text)


def broadcast_message_text(body: str) -> str:
    big = to_big_bold(body.strip())
    return (
        "📢🔔📢 " + to_big_bold("ANNOUNCEMENT") + " 📢🔔📢\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"{big}\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "🛡 Sent by an admin"
    )
