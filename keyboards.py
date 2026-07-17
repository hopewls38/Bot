# keyboards.py — All keyboard builders for the relay bot
# Uses consistent emoji color coding:
#   🟢 Green = Confirm / Continue / Positive
#   🔴 Red   = Delete / Cancel / Ban / Danger
#   🔵 Blue  = Main action / Info
#   ⚪ Grey  = Navigation / Neutral

from telebot import types
from database import (
    get_user, all_users_paged, is_main_admin, is_admin,
    _row_access_secs, get_referral_count, get_banned_users_paged,
)
from utils import fmt_time
from datetime import datetime, timezone
import logging

log = logging.getLogger("relay")


# ── User main menu ────────────────────────────────────────────────────────────

def user_main_keyboard():
    """
    Primary menu shown after /start.
    Five clean sections — Profile, Referral, Feedback, Leave, Help.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("👤   Profile",          callback_data="profile:show"))
    kb.add(types.InlineKeyboardButton("🔗   Referral Link",    callback_data="ref:link"))
    kb.add(types.InlineKeyboardButton("💬   Contact Admin",    callback_data="user:feedback"))
    kb.add(types.InlineKeyboardButton("🚪   Leave Network",    callback_data="user:leave"))
    kb.add(types.InlineKeyboardButton("❓   Help",             callback_data="user:help"))
    return kb


def profile_keyboard():
    """Keyboard shown inside the Profile view."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✏️   Set Display Name", callback_data="profile:setname"))
    kb.add(types.InlineKeyboardButton("🔮   Check Balance",    callback_data="time:check"))
    kb.add(types.InlineKeyboardButton("🔙   Back",             callback_data="user:menu"))
    return kb


def leave_confirm_keyboard():
    """Confirmation dialog before leaving the network."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅   Yes, leave",  callback_data="user:leave_confirm"),
        types.InlineKeyboardButton("❌   Cancel",       callback_data="user:menu"),
    )
    return kb


# ── User time / balance keyboards ─────────────────────────────────────────────

def user_time_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔮 My Balance",    callback_data="time:check"),
        types.InlineKeyboardButton("🔄 Refresh",       callback_data="time:refresh"),
    )
    kb.add(
        types.InlineKeyboardButton("🔗 Referral Link", callback_data="ref:link"),
    )
    kb.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="user:menu"))
    return kb


def user_time_keyboard_refresh():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔄 Refresh",       callback_data="time:check"),
        types.InlineKeyboardButton("📹 How to earn?",  callback_data="time:howto"),
    )
    kb.add(
        types.InlineKeyboardButton("🔗 Referral Link", callback_data="ref:link"),
    )
    return kb


def referral_keyboard():
    """
    Keyboard shown on the Referral Link page.
    Includes a dedicated Copy Link button — tapping it shows an alert popup
    with the raw link text so the user can long-press/copy on any platform.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📋 Copy Referral Link",  callback_data="ref:copy"),
    )
    kb.add(
        types.InlineKeyboardButton("🔄 Refresh Stats",       callback_data="ref:stats"),
    )
    kb.add(
        types.InlineKeyboardButton("🔙 Back to Menu",        callback_data="user:menu"),
    )
    return kb


# ── Admin keyboards ───────────────────────────────────────────────────────────

def admin_keyboard(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Stats",         callback_data="admin:stats"),
        types.InlineKeyboardButton("👥 Users",          callback_data="admin:users:0"),
    )
    kb.add(
        types.InlineKeyboardButton("🔇 Muted Users",   callback_data="admin:muted"),
        types.InlineKeyboardButton("🔴 Banned Users",  callback_data="admin:banned"),
    )
    if is_main_admin(uid):
        kb.add(
            types.InlineKeyboardButton("⚙️ Media Settings", callback_data="media:show"),
            types.InlineKeyboardButton("➕ Add Admin",        callback_data="admin:addadmin"),
        )
    if is_admin(uid):
        kb.add(
            types.InlineKeyboardButton("🎬 Welcome Media",  callback_data="welcome:start"),
            types.InlineKeyboardButton("📢 Broadcast",       callback_data="broadcast:start"),
        )
        kb.add(
            types.InlineKeyboardButton("🎁 Gift 24h (Expired)", callback_data="admin:gift24h"),
        )
    kb.add(types.InlineKeyboardButton("🔃 Refresh Panel", callback_data="admin:back"))
    return kb


def users_keyboard(page=0):
    rows, total = all_users_paged(page, per_page=6)
    per = 6
    kb  = types.InlineKeyboardMarkup(row_width=1)
    for u in rows:
        name = u["display_name"] or u["random_id"]
        if u["is_banned"]:
            status = "🔴"
        elif u["role"] >= 2:
            status = "👑"
        elif u["role"] >= 1:
            status = "🛡️"
        elif not u["active"]:
            status = "💤"
        else:
            status = "🟢"
        secs_    = _row_access_secs(u)
        time_tag = "Unlimited" if u["role"] >= 1 else (
            f"⏱ {fmt_time(secs_)}" if secs_ > 0 else "⌛ Expired"
        )
        kb.add(types.InlineKeyboardButton(
            f"{status} {name}  ·  {time_tag}",
            callback_data=f"admin:userinfo:{u['user_id']}",
        ))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(
            "◀️ Prev", callback_data=f"admin:users:{page - 1}"
        ))
    pages = max(1, (total + per - 1) // per)
    nav.append(types.InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if (page + 1) * per < total:
        nav.append(types.InlineKeyboardButton(
            "Next ▶️", callback_data=f"admin:users:{page + 1}"
        ))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🔙 Back to Panel", callback_data="admin:back"))
    return kb


def user_action_keyboard(target_uid, back_page=0):
    """
    The per-user admin panel. Kept deliberately short — one clear action per
    row — with Mute and Usage Time opening their own focused builder screens
    instead of sprawling across a wall of fixed-duration buttons.
    """
    u = get_user(target_uid)
    if not u:
        return types.InlineKeyboardMarkup()
    kb = types.InlineKeyboardMarkup(row_width=1)

    # ✉️ Direct Message — always first, the most common "reach out" action.
    kb.add(types.InlineKeyboardButton(
        "✉️ Direct Message", callback_data=f"admin:dm:{target_uid}"
    ))

    # 🔴 Ban / 🟢 Unban — stays a single click.
    if u["is_banned"]:
        kb.add(types.InlineKeyboardButton(
            "🟢 Unban", callback_data=f"admin:unban:{target_uid}"
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "🔴 Ban",   callback_data=f"admin:ban:{target_uid}"
        ))

    # 🔇 Mute / 🔊 Unmute — Mute opens the unit + stepper builder below.
    currently_muted = False
    if u["muted_until"]:
        try:
            mu = datetime.fromisoformat(u["muted_until"])
            if mu.tzinfo is None:
                mu = mu.replace(tzinfo=timezone.utc)
            currently_muted = datetime.now(timezone.utc) < mu
        except Exception:
            pass
    if currently_muted:
        kb.add(types.InlineKeyboardButton(
            "🔊 Unmute", callback_data=f"admin:unmute:{target_uid}"
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "🔇 Mute",   callback_data=f"admin:mute:{target_uid}"
        ))

    # ⏰ Usage Time — opens the increase/decrease + unit + stepper builder.
    kb.add(types.InlineKeyboardButton(
        "⏰ Usage Time", callback_data=f"admin:usagetime:{target_uid}"
    ))

    kb.add(types.InlineKeyboardButton(
        "🔙 Back to List", callback_data=f"admin:users:{back_page}"
    ))
    return kb


# Cycle order for the mute-duration unit toggle: seconds → minutes → hours → …
MUTE_UNIT_CYCLE = ["s", "m", "h"]
MUTE_UNIT_LABEL = {"s": "Seconds", "m": "Minutes", "h": "Hours"}
MUTE_UNIT_SHORT = {"s": "s", "m": "m", "h": "h"}


def mute_builder_keyboard(target_uid, unit, value):
    """
    Nested mute screen: one button cycles the unit (s → m → h), a stepper
    row adjusts the amount in that unit, then Confirm applies it. A Back
    button returns to the user's profile without muting anyone.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        f"🔁 Unit: {MUTE_UNIT_LABEL[unit]}",
        callback_data=f"admin:mu:{target_uid}:{unit}:{value}:cycle",
    ))
    kb.row(
        types.InlineKeyboardButton("➖", callback_data=f"admin:mu:{target_uid}:{unit}:{value}:dec"),
        types.InlineKeyboardButton(f"{value}{MUTE_UNIT_SHORT[unit]}", callback_data="noop"),
        types.InlineKeyboardButton("➕", callback_data=f"admin:mu:{target_uid}:{unit}:{value}:inc"),
    )
    kb.add(types.InlineKeyboardButton(
        "✅ Confirm Mute", callback_data=f"admin:mu:{target_uid}:{unit}:{value}:apply"
    ))
    kb.add(types.InlineKeyboardButton(
        "🔙 Back", callback_data=f"admin:mu:{target_uid}:{unit}:{value}:back"
    ))
    return kb


UT_UNIT_LABEL = {"m": "Minutes", "h": "Hours"}
UT_UNIT_SHORT = {"m": "m", "h": "h"}


def usage_time_builder_keyboard(target_uid, direction, unit, value):
    """
    Nested usage-time screen: pick increase/decrease, pick the unit (m/h),
    step the amount, then Confirm. A Back button returns to the profile
    without changing the user's balance.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    dir_label = "➕ Increase Balance" if direction == "add" else "➖ Decrease Balance"
    kb.add(types.InlineKeyboardButton(
        f"🔁 {dir_label}",
        callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:dir",
    ))
    kb.add(types.InlineKeyboardButton(
        f"🔁 Unit: {UT_UNIT_LABEL[unit]}",
        callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:unit",
    ))
    kb.row(
        types.InlineKeyboardButton("➖", callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:dec"),
        types.InlineKeyboardButton(f"{value}{UT_UNIT_SHORT[unit]}", callback_data="noop"),
        types.InlineKeyboardButton("➕", callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:inc"),
    )
    kb.add(types.InlineKeyboardButton(
        "✅ Confirm", callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:apply"
    ))
    kb.add(types.InlineKeyboardButton(
        "🔙 Back", callback_data=f"admin:ut:{target_uid}:{direction}:{unit}:{value}:back"
    ))
    return kb


def banned_users_keyboard(page=0):
    rows, total = get_banned_users_paged(page, per_page=6)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for u in rows:
        name = u["display_name"] or u["random_id"]
        kb.add(types.InlineKeyboardButton(
            f"🔴 {name}", callback_data=f"admin:userinfo:{u['user_id']}"
        ))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(
            "◀️ Prev", callback_data=f"admin:banned_page:{page - 1}"
        ))
    pages = max(1, (total + 5) // 6)
    nav.append(types.InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if (page + 1) * 6 < total:
        nav.append(types.InlineKeyboardButton(
            "Next ▶️", callback_data=f"admin:banned_page:{page + 1}"
        ))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin:back"))
    return kb


def media_keyboard(ms):
    def _t(flag): return "✅" if flag else "❌"
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton(f"{_t(ms['allow_text'])} Text",      callback_data="media:toggle:allow_text"),
        types.InlineKeyboardButton(f"{_t(ms['allow_photo'])} Photo",     callback_data="media:toggle:allow_photo"),
        types.InlineKeyboardButton(f"{_t(ms['allow_video'])} Video",     callback_data="media:toggle:allow_video"),
    )
    kb.add(
        types.InlineKeyboardButton(f"{_t(ms['allow_animation'])} GIF",   callback_data="media:toggle:allow_animation"),
        types.InlineKeyboardButton(f"{_t(ms['allow_sticker'])} Sticker", callback_data="media:toggle:allow_sticker"),
        types.InlineKeyboardButton(f"{_t(ms['allow_voice'])} Voice",     callback_data="media:toggle:allow_voice"),
    )
    kb.add(
        types.InlineKeyboardButton(f"{_t(ms['allow_audio'])} Audio",     callback_data="media:toggle:allow_audio"),
        types.InlineKeyboardButton(f"{_t(ms['allow_document'])} File",   callback_data="media:toggle:allow_document"),
    )
    mn_txt = f"{ms['min_video_bytes'] // 1048576} MB" if ms["min_video_bytes"] else "None"
    mx_txt = f"{ms['max_video_bytes'] // 1048576} MB" if ms["max_video_bytes"] else "None"
    kb.add(types.InlineKeyboardButton(f"📹 Min size: {mn_txt}", callback_data="media:setsize:min"))
    kb.add(types.InlineKeyboardButton(f"📹 Max size: {mx_txt}", callback_data="media:setsize:max"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin:back"))
    return kb


def backups_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔄 Refresh Count", callback_data="admin:backups"),
        types.InlineKeyboardButton("🔙 Back",           callback_data="admin:back"),
    )
    return kb


# ── Gift 24h confirmation keyboard ────────────────────────────────────────────

def gift24h_confirm_keyboard(expired_count: int):
    """Confirmation dialog before gifting 24h to all expired users."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            f"✅ Yes — Gift {expired_count} user(s)",
            callback_data="admin:gift24h:confirm",
        ),
        types.InlineKeyboardButton("❌ Cancel", callback_data="admin:back"),
    )
    return kb


# ── Welcome media collection ──────────────────────────────────────────────────

def welcome_collect_keyboard():
    """Inline (glass) keyboard shown while an admin is uploading welcome media.

    This used to be a ReplyKeyboardMarkup, which had two problems:
      1. Its "✅ Done" button sent a plain text message — if that text message
         ever slipped past the collection-mode gate (e.g. admin role revoked
         mid-session), it would fall straight into the normal relay path and
         get broadcast to every user in the network.
      2. Reply keyboards are sticky per-chat: some admins ended up with the
         button "stuck" on their keyboard permanently, unrelated to whether
         they were still in collection mode.
    An inline keyboard sends a callback_query instead of a text message, so it
    can never be relayed to other users, and it isn't tied to the chat's
    persistent keyboard at all — nothing to get "stuck".
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Done", callback_data="welcome:done"))
    return kb


def remove_keyboard():
    return types.ReplyKeyboardRemove()
