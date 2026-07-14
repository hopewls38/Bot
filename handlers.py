# handlers.py — All bot command, callback, and message handlers

import time
import uuid
import random
import threading
import logging
from concurrent.futures import ThreadPoolExecutor

from telebot import types

import telebot
from config import (
    MAIN_ADMIN_ID, MUTE_SECONDS, DEL_COUNTDOWN, SPAM_WINDOW, SPAM_LIMIT,
    LOW_TIME_WARN, GRACE_SECONDS, PHOTO_REWARD_SECS, VIDEO_REWARD_PER_MB,
    BYTES_PER_MIN, REFERRAL_REWARD_SECS, WELCOME_MEDIA_COUNT, RELAY_WORKERS,
    EXPIRED_REMINDER_INTERVAL_SECS, SMALL_VIDEO_MB_THRESHOLD,
    SMALL_VIDEO_STREAK_LIMIT,
)
from database import (
    get_user, get_user_by_username, upsert_user, set_display_name, touch_user,
    ban_user, unban_user,
    deactivate_user, set_mute, clear_mute, set_role, active_users, all_reachable_users,
    all_users_paged,
    is_admin, is_main_admin, get_access_seconds, add_access_time, has_access,
    add_access_time_returning_was_expired, subtract_access_time,
    _row_has_access, _row_access_secs, user_count, stats,
    get_batch_by_msg, get_batch_msgs, get_all_relay_msgs,
    delete_relay_log_all, delete_relay_log_batch, log_relay,
    get_media_settings, set_media_field, is_muted, _row_is_muted, mute_remaining_secs,
    get_referral_code, get_pending_referral, mark_referral_rewarded, get_referral_count,
    get_user_media_count, get_muted_users, get_backup_stats,
    add_welcome_media, count_welcome_media, get_random_welcome_media,
    get_users_needing_expired_reminder, mark_expired_reminder_sent,
    record_media_reward, get_media_reward, delete_media_reward, delete_media_rewards_all,
)
from utils import (
    md, fmt_time, time_bar, parse_duration, parse_del_time,
    user_info_text, admin_panel_text, media_settings_text,
    broadcast_message_text, strip_links,
    mute_builder_text, usage_time_builder_text,
)
from keyboards import (
    user_time_keyboard, user_time_keyboard_refresh, admin_keyboard,
    users_keyboard, user_action_keyboard, banned_users_keyboard,
    media_keyboard, referral_keyboard, backups_keyboard,
    user_main_keyboard, profile_keyboard, leave_confirm_keyboard,
    welcome_collect_keyboard, remove_keyboard,
    mute_builder_keyboard, usage_time_builder_keyboard,
    MUTE_UNIT_CYCLE,
)
from backup_manager import backup_message_media, is_duplicate_media

log = logging.getLogger("relay")

# ── Shared state ─────────────────────────────────────────────────────────────
from collections import deque
_spam       = {}
_spam_lock  = threading.Lock()
_awaiting   = {}
_awaiting_lock = threading.Lock()

# Holds a composed-but-unsent direct message per admin uid — {"target": int,
# "text": str} — while they review the Send/Cancel preview. Kept separate
# from _awaiting (which only tracks "waiting for the next text message").
_dm_pending      = {}
_dm_pending_lock = threading.Lock()

# ── Mute / usage-time builder step sizes ───────────────────────────────────
# All state for these nested keyboards travels in the callback_data itself
# (target, unit, value) — no server-side session needed, so there's nothing
# to go stale if an admin walks away mid-flow.
_MUTE_STEP = {"s": 10, "m": 5, "h": 1}
_MUTE_MIN  = {"s": 10, "m": 1, "h": 1}
_MUTE_MAX  = {"s": 3600, "m": 1440, "h": 720}
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

_UT_STEP = {"m": 5, "h": 1}
_UT_MIN  = {"m": 5, "h": 1}
_UT_MAX  = {"m": 1440, "h": 720}


def _clamp_mute_val(unit, val):
    return max(_MUTE_MIN[unit], min(_MUTE_MAX[unit], val))


def _clamp_ut_val(unit, val):
    return max(_UT_MIN[unit], min(_UT_MAX[unit], val))
_del_lock        = threading.Lock()
_del_running     = False
_del_cancel_evt  = threading.Event()   # set() → cancels the pending deletion
_shutdown   = threading.Event()

# Admins currently in "collect welcome media" mode (in-memory, ephemeral).
_collecting_welcome      = set()
_collecting_welcome_lock = threading.Lock()

# Tracks each user's current streak of consecutive small videos (in-memory,
# ephemeral) — used to trigger the small-video spam warning below.
_small_video_streak      = {}
_small_video_streak_lock = threading.Lock()

_CAP_TYPES  = ("photo", "video", "animation", "audio", "document", "voice")
_DEAD_ERRS  = ("bot was blocked", "user is deactivated", "chat not found",
               "forbidden", "have no rights")
_WELCOME_MEDIA_TYPES = ("photo", "video", "animation")

bot: telebot.TeleBot = None   # injected by main.py


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe(fn, *args, target_uid=None, **kwargs):
    """
    Run a Telegram API call, transparently handling flood-control (429) with
    a single retry, and auto-deactivating users whose chat has gone dead
    (blocked/left/deleted) so future broadcasts skip them.
    """
    try:
        return fn(*args, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if getattr(e, "error_code", None) == 429:
            retry_after = 1
            try:
                retry_after = e.result_json.get("parameters", {}).get("retry_after", 1)
            except Exception:
                pass
            time.sleep(retry_after + 0.2)
            try:
                return fn(*args, **kwargs)
            except Exception as e2:
                e = e2
        err = str(e).lower()
        if any(x in err for x in _DEAD_ERRS):
            if target_uid:
                deactivate_user(target_uid)
                log.info("Auto-deactivated %s: %s", target_uid, e)
            return None
        log.warning("Send error (uid=%s): %s", target_uid, e)
        return None
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in _DEAD_ERRS):
            if target_uid:
                deactivate_user(target_uid)
                log.info("Auto-deactivated %s: %s", target_uid, e)
            return None
        log.warning("Send error (uid=%s): %s", target_uid, e)
        return None


def _parallel_dispatch(items, worker_fn, max_workers=RELAY_WORKERS):
    """
    Run worker_fn(item) for every item concurrently instead of sequentially.
    This is what makes broadcast/relay/welcome-media fan-out fast — no more
    one-message-at-a-time-with-a-sleep. Exceptions in a worker are swallowed
    (workers already use _safe internally) so one bad chat never stops the rest.
    """
    items = list(items)
    if not items:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(worker_fn, item) for item in items]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                log.warning("Parallel dispatch worker error: %s", e)


def _is_eligible_recipient(user_row) -> bool:
    """
    True if a user should currently receive relayed/broadcast media —
    banned, muted, or access-expired users are treated as if they left chat.
    """
    if not user_row or user_row["is_banned"] or not user_row["active"]:
        return False
    # Row-based check (no extra DB call per recipient) — matters when this
    # runs across an entire relay/broadcast target list.
    if _row_is_muted(user_row):
        return False
    return _row_has_access(user_row)


def check_spam(uid) -> bool:
    now = time.time()
    with _spam_lock:
        dq = _spam.setdefault(uid, deque())
        while dq and now - dq[0] > SPAM_WINDOW:
            dq.popleft()
        dq.append(now)
        return len(dq) > SPAM_LIMIT


def prune_memory_state():
    now = time.time()
    with _spam_lock:
        stale = [uid for uid, dq in _spam.items()
                 if not dq or now - dq[-1] > SPAM_WINDOW]
        for uid in stale:
            del _spam[uid]
    with _awaiting_lock:
        if len(_awaiting) > 1000:
            _awaiting.clear()
            log.warning("Cleared _awaiting state: exceeded safety cap")
    with _dm_pending_lock:
        if len(_dm_pending) > 1000:
            _dm_pending.clear()
            log.warning("Cleared _dm_pending state: exceeded safety cap")
    with _small_video_streak_lock:
        if len(_small_video_streak) > 2000:
            _small_video_streak.clear()
            log.warning("Cleared _small_video_streak state: exceeded safety cap")


# ── Relay core ────────────────────────────────────────────────────────────────

def _relay_to(source_chat_id, message, target_uid, prefix) -> list:
    tid  = target_uid
    caption_text = message.caption
    if caption_text and (message.photo or message.video):
        caption_text = strip_links(caption_text)
    cap  = prefix.strip() + ("\n\n" + caption_text if caption_text else "")
    sent = []

    def _s(fn, *a, **kw):
        m = _safe(fn, *a, target_uid=tid, **kw)
        if m and hasattr(m, "message_id"):
            sent.append(m.message_id)

    if message.text:
        _s(bot.send_message, tid, prefix + message.text)
    elif any(getattr(message, t, None) for t in _CAP_TYPES):
        _s(bot.copy_message,
           chat_id=tid, from_chat_id=source_chat_id,
           message_id=message.message_id, caption=cap)
    elif message.location:
        _s(bot.send_message, tid, prefix.strip())
        _s(bot.send_location, tid,
           message.location.latitude, message.location.longitude)
    elif message.contact:
        _s(bot.send_message, tid, prefix.strip())
        _s(bot.send_contact, tid,
           message.contact.phone_number, message.contact.first_name,
           last_name=message.contact.last_name or "")
    elif message.dice:
        _s(bot.send_message, tid,
           prefix + f"🎲 {message.dice.emoji} => {message.dice.value}")
    elif message.poll:
        _s(bot.send_message, tid,
           prefix + f"📊 Poll: {message.poll.question}")
    else:
        _s(bot.send_message, tid, prefix.strip())
        _s(bot.copy_message, chat_id=tid, from_chat_id=source_chat_id,
           message_id=message.message_id)
    return sent


def relay_message(sender_uid, source_chat_id, message, targets=None, batch_id=None):
    """
    targets, if given, must already be filtered to eligible recipients
    (see _is_eligible_recipient) — handle_message pre-computes this so the
    "Relayed to N member(s)" confirmation always matches who actually got it.
    If omitted, targets are looked up and filtered here instead.

    batch_id, if given, is used instead of generating a fresh one — this is
    how handle_message links a relay batch back to the media_rewards row it
    recorded for the earned time, so /delete can find and reverse it later.
    """
    try:
        sender  = get_user(sender_uid)
        if sender is None:
            return
        if targets is None:
            targets = [t for t in active_users(exclude_id=sender_uid)
                       if _is_eligible_recipient(t)]
        if not targets:
            return
        name   = sender["display_name"] or sender["random_id"]
        badge  = " 🛡" if sender["role"] >= 1 else ""
        prefix = f"📩 {name}{badge}\n\n"
        batch  = batch_id or str(uuid.uuid4())

        def _send_one(t):
            if _shutdown.is_set():
                return
            tid  = t["user_id"]
            mids = _relay_to(source_chat_id, message, tid, prefix)
            for mid in mids:
                log_relay(batch, sender_uid, tid, mid)

        # Fan the message out to every recipient concurrently instead of one
        # at a time — this is the main relay speed-up.
        _parallel_dispatch(targets, _send_one)
    except Exception as e:
        log.error("relay_message crashed (sender=%s): %s", sender_uid, e, exc_info=True)


# ── Notification helpers ──────────────────────────────────────────────────────

def _notify_unmuted(target_uid):
    """Send the unmuted user a private notification."""
    _safe(bot.send_message, target_uid,
          "🔊 *You have been unmuted*\n"
          "━━━━━━━━━━━━━━━━━\n\n"
          "You can send messages on NightVi again.",
          parse_mode="Markdown", target_uid=target_uid)


def _notify_unbanned(target_uid):
    """Send the unbanned user a private notification."""
    _safe(bot.send_message, target_uid,
          "🟢 *You have been unbanned*\n"
          "━━━━━━━━━━━━━━━━━\n\n"
          "Your access to NightVi has been restored.\n"
          "Send /start to rejoin.",
          parse_mode="Markdown", target_uid=target_uid)


def _notify_muted(target_uid, duration_secs: int, reason: str = None):
    """Send the muted user a private notification."""
    dur_str = fmt_time(duration_secs)
    text = (
        "🔇 *You have been muted*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ Duration: *{dur_str}*\n"
    )
    if reason:
        text += f"📝 Reason: _{md(reason)}_\n"
    text += "\nYou will be unmuted automatically when the duration expires."
    _safe(bot.send_message, target_uid, text, parse_mode="Markdown",
          target_uid=target_uid)


def _notify_banned(target_uid, reason: str = None):
    """Send the banned user a private notification."""
    text = (
        "🚫 *You have been banned*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        "You have been permanently banned from NightVi.\n"
    )
    if reason:
        text += f"📝 Reason: _{md(reason)}_\n"
    text += "\nIf you believe this is a mistake, contact the admin."
    _safe(bot.send_message, target_uid, text, parse_mode="Markdown",
          target_uid=target_uid)


def _notify_direct_message(target_uid, text: str):
    """
    Deliver an admin's direct message. Distinctly framed as a personal note
    from staff — not part of the anonymous relay network — via an English
    header that also serves as the push-notification preview.
    """
    _safe(bot.send_message, target_uid,
          "🔔 *Direct Message From Admin*\n"
          "━━━━━━━━━━━━━━━━━\n\n"
          f"{md(text)}\n\n"
          "_This was sent to you directly by an administrator — it is not "
          "part of the shared network chat._",
          parse_mode="Markdown", target_uid=target_uid)


def _notify_media_deleted(target_uid, media_type: str, secs: int, new_balance: int):
    """Tell the sender their media was removed and how much reward was reversed."""
    label = {"photo": "photo", "video": "video"}.get(media_type, "media")
    _safe(bot.send_message, target_uid,
          "🗑 *Your media was removed*\n"
          "━━━━━━━━━━━━━━━━━\n\n"
          f"An admin deleted a {label} you sent to the network.\n"
          f"⏱ Time reward reversed: *-{fmt_time(secs)}*\n"
          f"⏳ Your new balance: *{fmt_time(new_balance)}*\n"
          f"`{time_bar(new_balance)}`",
          parse_mode="Markdown", target_uid=target_uid)


def _notify_time_adjusted(target_uid, direction: str, secs: int, new_balance: int):
    """Tell a user an admin manually added/removed usage time via the profile panel."""
    if direction == "add":
        text = (
            "⏰ *Time Added!*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            f"An admin granted you *+{fmt_time(secs)}* of access.\n"
            f"Your new balance: *{fmt_time(new_balance)}*\n"
            f"`{time_bar(new_balance)}`"
        )
    else:
        text = (
            "⏰ *Time Adjusted*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            f"An admin removed *{fmt_time(secs)}* from your access time.\n"
            f"Your new balance: *{fmt_time(new_balance)}*\n"
            f"`{time_bar(new_balance)}`"
        )
    _safe(bot.send_message, target_uid, text, parse_mode="Markdown",
          target_uid=target_uid)


# ── Welcome media (new-user greeting) ─────────────────────────────────────────

def _send_welcome_media(uid: int, intro_caption: str = "🎬 A few welcome clips just for you — enjoy!"):
    """
    Send up to WELCOME_MEDIA_COUNT randomly-picked cached welcome media items
    to a user, as a separate batch from the main relay chat. Used both for
    brand-new users (/start) and for previously-expired users who come back
    and send media again (see the "welcome back" hook in handle_message).
    Only `file_id`s are stored/used, so nothing is ever downloaded — sending
    is just as cheap on RAM/bandwidth as the relay's own copy_message calls.
    """
    try:
        items = get_random_welcome_media(WELCOME_MEDIA_COUNT)
        if not items:
            return
        items = list(items)
        random.shuffle(items)

        def _send_one(item):
            file_type = item["file_type"]
            file_id   = item["file_id"]
            caption   = intro_caption if item is items[0] else None
            if file_type == "video":
                _safe(bot.send_video, uid, file_id, caption=caption, target_uid=uid)
            elif file_type == "animation":
                _safe(bot.send_animation, uid, file_id, caption=caption, target_uid=uid)
            else:
                _safe(bot.send_photo, uid, file_id, caption=caption, target_uid=uid)

        _parallel_dispatch(items, _send_one, max_workers=min(RELAY_WORKERS, 4))
    except Exception as e:
        log.warning("_send_welcome_media error uid=%s: %s", uid, e)


_greet_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="greet")


def _greet_returning_user(uid: int):
    """
    A previously time-expired user just sent media and has a balance again —
    treat them like a fresh /start: fire the same welcome-media batch, with
    a "welcome back" caption instead of the new-user one.

    Dispatched on a small, bounded executor (not a fresh thread per call) so
    a burst of returning users can't spawn unbounded OS threads.
    """
    _greet_executor.submit(
        _send_welcome_media,
        uid,
        "🎉 Welcome back! Here are a few clips to get you started again.",
    )


# ── Expired-time reminders ────────────────────────────────────────────────────

def remind_expired_users():
    """
    DM every user whose access time has run out (and who hasn't already been
    reminded within EXPIRED_REMINDER_INTERVAL_SECS) with a nudge on how to
    earn more time. Called from main.py's maintenance loop roughly every 6h.
    Banned/muted/admin users are never selected by the underlying query.
    """
    try:
        rows = get_users_needing_expired_reminder(EXPIRED_REMINDER_INTERVAL_SECS)
        if not rows:
            return
        text = (
            "⏳ *Your access time has run out*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "You can top up anytime:\n"
            "📸 Photo → *+1 min*\n"
            "📹 1 MB video → *+5 min*\n"
            "🔗 Invite a friend → *+2h* for both of you\n\n"
            "Just send a photo or video, or grab your referral link below."
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔗 Get Referral Link", callback_data="ref:link"))

        def _send_one(u):
            tid = u["user_id"]
            _safe(bot.send_message, tid, text, parse_mode="Markdown",
                  reply_markup=kb, target_uid=tid)
            # Marked as sent even on failure — a dead chat gets deactivated by
            # _safe anyway, and we never want a retry storm on stale rows.
            mark_expired_reminder_sent(tid)

        _parallel_dispatch(rows, _send_one, max_workers=min(RELAY_WORKERS, 4))
        log.info("Expired-time reminder sent to %d user(s)", len(rows))
    except Exception as e:
        log.warning("remind_expired_users error: %s", e)


# ── Referral reward helper ────────────────────────────────────────────────────

def _process_referral_reward(new_uid: int):
    """
    Check if new_uid was referred by someone and grant rewards.
    Called once after a brand-new user's first /start.
    """
    ref_row = get_pending_referral(new_uid)
    if not ref_row:
        return
    referrer_id = ref_row["referrer_id"]

    # Add +2 hours to both parties
    add_access_time(referrer_id, REFERRAL_REWARD_SECS)
    add_access_time(new_uid,     REFERRAL_REWARD_SECS)
    mark_referral_rewarded(ref_row["id"])

    ref_count = get_referral_count(referrer_id)
    # Notify referrer
    _safe(bot.send_message, referrer_id,
          f"🎉 *Referral reward!*\n\n"
          f"Someone joined using your link.\n"
          f"⏰ *+{fmt_time(REFERRAL_REWARD_SECS)}* added to your balance!\n"
          f"🔗 Total successful referrals: *{ref_count}*",
          parse_mode="Markdown", target_uid=referrer_id)

    # Notify new user
    _safe(bot.send_message, new_uid,
          f"🎉 *Welcome bonus!*\n\n"
          f"You joined via a referral link.\n"
          f"⏰ *+{fmt_time(REFERRAL_REWARD_SECS)}* added to your balance!",
          parse_mode="Markdown", target_uid=new_uid)

    log.info("Referral reward: referrer=%s referred=%s (+%ds each)",
             referrer_id, new_uid, REFERRAL_REWARD_SECS)


# ── /start ─────────────────────────────────────────────────────────────────

def cmd_start(msg: types.Message):
    uid      = msg.from_user.id
    username = msg.from_user.username

    # Parse referral code from /start payload
    parts   = msg.text.strip().split(maxsplit=1)
    ref_arg = parts[1].strip() if len(parts) > 1 else None
    ref_code = ref_arg if (ref_arg and ref_arg.startswith("ref_")) else None

    rid, is_new = upsert_user(uid, username, referral_code=ref_code)
    if rid is None:
        bot.reply_to(msg, "🚫 You are banned from NightVi.")
        return
    touch_user(uid)

    # Muted users can still browse but not send — tell them where they stand.
    if is_muted(uid):
        remaining = mute_remaining_secs(uid)
        bot.reply_to(msg,
            f"🔇 *You are muted*\n\n"
            f"You can still browse, but sending is disabled for another "
            f"*{fmt_time(remaining)}*.",
            parse_mode="Markdown",
        )

    # Grant referral rewards for brand-new users
    if is_new:
        threading.Thread(
            target=_process_referral_reward, args=(uid,), daemon=True
        ).start()
        # Send cached welcome media (photos/videos) as a separate batch.
        threading.Thread(
            target=_send_welcome_media, args=(uid,), daemon=True
        ).start()

    n    = user_count()
    secs = get_access_seconds(uid) if not is_admin(uid) else -1
    if is_admin(uid):
        time_line = "🛡 Admin — unlimited access"
        kb = admin_keyboard(uid)
    elif secs > 0:
        time_line = f"⏳ Time balance: *{fmt_time(secs)}*\n`{time_bar(secs)}`"
        kb = user_main_keyboard()
    else:
        time_line = "⏳ Time balance: *0 min* — send media to earn time"
        kb = user_main_keyboard()

    bot.reply_to(msg,
        f"✦ *Welcome to NightVi*\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 Your ID: `{rid}`\n"
        f"👥 Members: *{n}*\n"
        f"{time_line}\n\n"
        f"📨 Every message is delivered anonymously.\n"
        f"📸 Photo → *+1 min*  ·  📹 1 MB video → *+5 min*",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    log.info("User %s joined (rid=%s, new=%s)", uid, rid, is_new)


# ── /help ──────────────────────────────────────────────────────────────────

def cmd_help(msg: types.Message):
    is_adm = is_admin(msg.from_user.id)
    text = _help_text(is_adm)
    bot.reply_to(msg, text, parse_mode="Markdown")


def _help_text(is_adm: bool) -> str:
    text = (
        "❓ *NightVi — Help*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        "📨 Every message you send is relayed *anonymously*.\n\n"
        "👤 *Profile* — tap the Profile button\n"
        "  View your ID, set display name, check balance\n\n"
        "🔗 *Referral* — tap the Referral button\n"
        "  Invite friends — you *both* get +2 hours on join\n\n"
        "⏱ *Earn Time*\n"
        "  📸 Photo → +1 minute\n"
        "  📹 1 MB video → +5 minutes\n\n"
        "🚪 *Leave* — tap the Leave button to exit NightVi"
    )
    if is_adm:
        text += (
            "\n\n━━━━━━━━━━━━━━━━━\n"
            "🛡 *Admin Commands*\n"
            "`/admin` — admin panel\n"
            "`/users` — user list\n"
            "`/prof username` · `/prof` (reply) — open a user's profile panel\n"
            "`/ban [reason]` — ban (reply to message)\n"
            "`/unban` — unban user\n"
            "`/mute 10min [reason]` — mute with duration\n"
            "  (or open a user's profile → 🔇 Mute for the unit + stepper picker)\n"
            "`/pin` — pin a message in every chat (reply to it)\n"
            "`/del 5sec` · `/del 2min` — schedule delete\n"
            "`/delete` — delete one message (reply to it); reverses any time it earned\n"
            "`/broadcast Your text` — announce to every user (even banned/muted/expired)\n"
            "`/stats` — network stats\n"
            "`/addadmin` — promote to admin (reply to message)\n"
            "\n_A user's profile panel also has ✉️ Direct Message and ⏰ Usage Time pickers._\n"
            "_Durations: `s` / `min` / `h` / `d`_"
        )
    return text


# ── Low-time proactive notification ──────────────────────────────────────────

_low_time_warned: set = set()   # uids already notified; cleared when they recharge


def check_low_time_users():
    """
    Called from the maintenance loop every 5 min.
    Proactively notifies users whose remaining time just dropped below LOW_TIME_WARN.
    """
    from config import LOW_TIME_WARN
    try:
        users = active_users()
        for u in users:
            uid = u["user_id"]
            if u["role"] >= 1:   # admins have unlimited time
                _low_time_warned.discard(uid)
                continue
            secs = _row_access_secs(u)
            if 0 < secs < LOW_TIME_WARN:
                if uid not in _low_time_warned:
                    _low_time_warned.add(uid)
                    _safe(bot.send_message, uid,
                          f"⚠️ *Low time warning!*\n\n"
                          f"Only *{fmt_time(secs)}* remaining on NightVi.\n"
                          f"`{time_bar(secs)}`\n\n"
                          f"📸 Photo → +1 min  ·  📹 1 MB video → +5 min\n"
                          f"🔗 Or invite a friend with your referral link for +2h",
                          parse_mode="Markdown",
                          reply_markup=user_main_keyboard(),
                          target_uid=uid)
            elif secs >= LOW_TIME_WARN:
                _low_time_warned.discard(uid)   # recharged — allow future warnings
    except Exception as e:
        log.warning("check_low_time_users error: %s", e)


# ── /id ───────────────────────────────────────────────────────────────────

def cmd_id(msg: types.Message):
    row = get_user(msg.from_user.id)
    if not row or not row["active"]:
        bot.reply_to(msg, "You're not in the network. Send /start to join.")
        return
    name = row["display_name"] or row["random_id"]
    secs = get_access_seconds(msg.from_user.id) if row["role"] == 0 else -1
    time_info = (
        f"\n⏱ Time: *{fmt_time(secs)}*\n`{time_bar(secs)}`"
        if secs >= 0 else "\n🛡 Access: Unlimited"
    )
    bot.reply_to(msg,
        f"🆔 ID: `{row['random_id']}`\n"
        f"📛 Name: *{md(name)}*\n"
        f"👥 Members: {user_count()}{time_info}",
        parse_mode="Markdown",
    )


# ── /referral ─────────────────────────────────────────────────────────────

def cmd_referral(msg: types.Message):
    uid = msg.from_user.id
    row = get_user(uid)
    if not row or not row["active"]:
        bot.reply_to(msg, "Join first with /start.")
        return
    code  = get_referral_code(uid)
    if not code:
        bot.reply_to(msg, "❌ Could not generate your referral link. Try again.")
        return
    ref_count = get_referral_count(uid)
    me = bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    text = (
        "🔗 *Your Referral Link*\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"`{link}`\n\n"
        f"🎁 *How it works:*\n"
        f"  • Share your link with friends\n"
        f"  • When they join using it, you *both* get +2 hours\n"
        f"  • No limits on how many friends you can invite!\n\n"
        f"📊 Successful referrals: *{ref_count}*\n"
        f"⏰ Total earned: *{fmt_time(ref_count * REFERRAL_REWARD_SECS)}*"
    )
    bot.reply_to(msg, text, parse_mode="Markdown", reply_markup=referral_keyboard())


# ── /leave ─────────────────────────────────────────────────────────────────

def cmd_leave(msg: types.Message):
    row = get_user(msg.from_user.id)
    if not row or not row["active"]:
        bot.reply_to(msg, "You're not in the network.")
        return
    deactivate_user(msg.from_user.id)
    bot.reply_to(msg, "✅ You have left the network. Send /start to rejoin anytime.")
    log.info("User %s left", msg.from_user.id)


# ── /name ─────────────────────────────────────────────────────────────────

def cmd_name(msg: types.Message):
    row = get_user(msg.from_user.id)
    if not row or not row["active"]:
        bot.reply_to(msg, "Join first with /start.")
        return
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg, "Usage: `/name YourNewName`", parse_mode="Markdown")
        return
    name = parts[1].strip()[:32]
    if set_display_name(msg.from_user.id, name):
        bot.reply_to(msg, f"✅ Display name set to: *{md(name)}*", parse_mode="Markdown")
    else:
        bot.reply_to(msg, f"❌ The name '{name}' is already taken.")


# ── /admin ─────────────────────────────────────────────────────────────────

def cmd_admin(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    bot.reply_to(msg, admin_panel_text(stats()),
                 parse_mode="Markdown",
                 reply_markup=admin_keyboard(msg.from_user.id))


# ── /users ─────────────────────────────────────────────────────────────────

def cmd_users(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    _, total = all_users_paged(0)
    bot.reply_to(msg,
        f"👥 *User List*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Total: *{total}* members\n\n"
        f"🟢 Active  🔴 Banned  💤 Inactive  🛡️ Admin  👑 Main Admin",
        parse_mode="Markdown",
        reply_markup=users_keyboard(0),
    )


# ── /ban ──────────────────────────────────────────────────────────────────

def cmd_ban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    if msg.reply_to_message:
        row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
        if not row:
            bot.reply_to(msg, "❌ Could not find the original sender. (Only works on relayed messages.)")
            return
        target_uid = row["sender_uid"]
        if target_uid == MAIN_ADMIN_ID:
            bot.reply_to(msg, "❌ Cannot ban the main admin.")
            return
        # Parse optional reason
        parts  = msg.text.strip().split(maxsplit=1)
        reason = parts[1].strip() if len(parts) > 1 else None
        ban_user(target_uid)
        u    = get_user(target_uid)
        name = (u["display_name"] or u["random_id"]) if u else str(target_uid)
        bot.reply_to(msg, f"✅ User *{md(name)}* has been banned.", parse_mode="Markdown")
        # Notify banned user
        threading.Thread(
            target=_notify_banned, args=(target_uid, reason), daemon=True
        ).start()
        log.info("Admin %s banned user %s (reason=%s)", msg.from_user.id, target_uid, reason)
    else:
        _, total = all_users_paged(0)
        bot.reply_to(msg,
            f"👥 *Select a user to ban*\n━━━━━━━━━━━━━━━━━\nTotal: *{total}* members",
            parse_mode="Markdown",
            reply_markup=users_keyboard(0),
        )


# ── /unban ─────────────────────────────────────────────────────────────────

def cmd_unban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    bot.reply_to(msg, "👥 *Select a user to unban:*",
                 parse_mode="Markdown",
                 reply_markup=banned_users_keyboard(0))


# ── /mute ─────────────────────────────────────────────────────────────────

def cmd_mute(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    if not msg.reply_to_message:
        bot.reply_to(msg,
            "Reply to a relayed message with:\n"
            "`/mute 10min` — mute for 10 minutes\n"
            "`/mute 2h` — mute for 2 hours\n"
            "`/mute 1d` — mute for 1 day\n"
            "`/mute 30min Spamming` — mute with reason",
            parse_mode="Markdown",
        )
        return
    row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
    if not row:
        bot.reply_to(msg, "❌ Could not find the original sender.")
        return
    target_uid = row["sender_uid"]
    if target_uid == MAIN_ADMIN_ID:
        bot.reply_to(msg, "❌ Cannot mute the main admin.")
        return

    # Parse: /mute [duration] [optional reason]
    raw = msg.text.strip()
    args_str = raw[len("/mute"):].strip()

    # Try to extract duration from start of args
    duration_secs = None
    reason        = None
    if args_str:
        # Find first duration token
        import re
        m = re.match(
            r"(\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds"
            r"|m|min|mins|minute|minutes"
            r"|h|hr|hrs|hour|hours"
            r"|d|day|days))\s*(.*)?",
            args_str, re.IGNORECASE
        )
        if m:
            duration_secs = parse_duration(m.group(1))
            reason        = m.group(2).strip() or None
        else:
            # No valid duration — show usage
            bot.reply_to(msg,
                "❌ Invalid duration.\n"
                "Examples: `/mute 10min`, `/mute 2h`, `/mute 1d`, `/mute 30min Spamming`",
                parse_mode="Markdown",
            )
            return
    else:
        duration_secs = MUTE_SECONDS  # default 5 min

    if not duration_secs or duration_secs <= 0:
        bot.reply_to(msg, "❌ Duration must be greater than 0.")
        return

    set_mute(target_uid, duration_secs)
    u    = get_user(target_uid)
    name = (u["display_name"] or u["random_id"]) if u else str(target_uid)
    dur_str = fmt_time(duration_secs)
    reply_text = f"🔇 User *{md(name)}* muted for *{dur_str}*"
    if reason:
        reply_text += f"\n📝 Reason: _{md(reason)}_"
    bot.reply_to(msg, reply_text, parse_mode="Markdown")

    # Notify the muted user via PM
    threading.Thread(
        target=_notify_muted, args=(target_uid, duration_secs, reason), daemon=True
    ).start()
    log.info("Admin %s muted user %s for %ss (reason=%s)",
             msg.from_user.id, target_uid, duration_secs, reason)


# ── /addadmin ─────────────────────────────────────────────────────────────

def cmd_addadmin(msg: types.Message):
    if not is_main_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ Only the main admin can add admins.")
        return
    if msg.reply_to_message:
        row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
        if not row:
            bot.reply_to(msg, "❌ Reply to a relayed message to promote its sender.")
            return
        target_uid = row["sender_uid"]
        u = get_user(target_uid)
        if not u:
            bot.reply_to(msg, "❌ User not found.")
            return
        set_role(target_uid, 1)
        name = u["display_name"] or u["random_id"]
        bot.reply_to(msg, f"✅ *{md(name)}* is now an admin.", parse_mode="Markdown")
    else:
        bot.reply_to(msg,
            "Reply to a relayed message with /addadmin to promote that user to admin.")


# ── /del ──────────────────────────────────────────────────────────────────

def cmd_del(msg: types.Message):
    global _del_running
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 2:
        countdown = parse_del_time(parts[1])
        if countdown is None or countdown <= 0:
            bot.reply_to(msg,
                "❌ Invalid time format. Examples:\n"
                "`/del 5sec` — 5 seconds\n"
                "`/del 2min` — 2 minutes\n"
                "`/del 30s` — 30 seconds",
                parse_mode="Markdown",
            )
            return
    else:
        countdown = DEL_COUNTDOWN

    with _del_lock:
        if _del_running:
            bot.reply_to(msg, "⏳ A deletion is already scheduled. Please wait.")
            return
        _del_running = True
        _del_cancel_evt.clear()   # reset any previous cancel signal

    started  = False
    mins     = countdown // 60
    secs_rem = countdown % 60
    time_str = (
        f"{mins}m {secs_rem}s" if secs_rem
        else (f"{mins} min" if mins else f"{countdown}s")
    )
    notice = (
        f"⚠️ *Warning:* Save any media — all messages will be deleted in {time_str}."
        if countdown > 300
        else f"🗑 All messages will be deleted in {time_str}."
    )
    for u in active_users():
        _safe(bot.send_message, u["user_id"], notice,
              parse_mode="Markdown", target_uid=u["user_id"])
    bot.reply_to(msg, f"✅ All users notified. Deletion starts in {time_str}.")
    log.info("Admin %s triggered /del (countdown=%ss)", msg.from_user.id, countdown)

    def _do_delete():
        global _del_running
        try:
            cancelled = _del_cancel_evt.wait(timeout=countdown)
            if cancelled:
                log.info("/del cancelled by admin before deletion started")
                return
            rows = get_all_relay_msgs()
            for row in rows:
                _safe(bot.delete_message, row["target_uid"], row["message_id"])
                time.sleep(0.03)
            delete_relay_log_all()
            delete_media_rewards_all()
            log.info("/del completed — %d messages deleted", len(rows))
        except Exception as e:
            log.error("/del thread error: %s", e, exc_info=True)
        finally:
            with _del_lock:
                _del_running = False

    try:
        threading.Thread(target=_do_delete, daemon=True).start()
        started = True
    finally:
        if not started:
            with _del_lock:
                _del_running = False


# ── /cancelDel ────────────────────────────────────────────────────────────

def cmd_cancel_del(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    with _del_lock:
        if not _del_running:
            bot.reply_to(msg, "ℹ️ No deletion is currently scheduled.")
            return
    _del_cancel_evt.set()   # wake the sleeping thread → it will abort
    bot.reply_to(msg, "✅ Deletion cancelled.")
    cancel_notice = "✅ *Deletion cancelled.* Messages are safe."
    for u in active_users():
        _safe(bot.send_message, u["user_id"], cancel_notice,
              parse_mode="Markdown", target_uid=u["user_id"])
    log.info("Admin %s cancelled pending /del", msg.from_user.id)


# ── /pin ──────────────────────────────────────────────────────────────────

def cmd_pin(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    if not msg.reply_to_message:
        bot.reply_to(msg, "Reply to a relayed message with /pin to pin it in every chat it was delivered to.")
        return
    row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
    if not row:
        bot.reply_to(msg, "❌ Could not find that message in the relay log. (Only works on relayed messages.)")
        return
    batch_id = row["batch_id"]
    msgs_    = get_batch_msgs(batch_id)
    if not msgs_:
        bot.reply_to(msg, "❌ No delivered copies found for this message.")
        return

    def _pin_one(m):
        _safe(bot.pin_chat_message, m["target_uid"], m["message_id"],
              disable_notification=True, target_uid=m["target_uid"])

    _parallel_dispatch(msgs_, _pin_one)

    # Muted/expired users are excluded from normal relay/broadcast delivery,
    # so they never got a copy of this message in the first place. When an
    # admin pins something, deliver it to them too (from the admin's own
    # copy) and pin it there — same batch_id, so /delete still cleans it up.
    already  = {m["target_uid"] for m in msgs_}
    already.add(msg.from_user.id)
    src_chat = msg.from_user.id
    src_mid  = msg.reply_to_message.message_id
    missing  = [u for u in all_reachable_users()
                if not u["is_banned"] and u["user_id"] not in already
                and (_row_is_muted(u) or not _row_has_access(u))]

    delivered = []

    def _deliver_and_pin(u):
        tid = u["user_id"]
        m = _safe(bot.copy_message, chat_id=tid, from_chat_id=src_chat,
                  message_id=src_mid, target_uid=tid)
        if m and hasattr(m, "message_id"):
            log_relay(batch_id, msg.from_user.id, tid, m.message_id)
            _safe(bot.pin_chat_message, tid, m.message_id,
                  disable_notification=True, target_uid=tid)
            delivered.append(tid)

    if missing:
        _parallel_dispatch(missing, _deliver_and_pin)

    total = len(msgs_) + len(delivered)
    extra_note = f" (+{len(delivered)} muted/expired user(s) notified & pinned)" if delivered else ""
    bot.reply_to(msg, f"📌 Pinned in {total} chat(s).{extra_note}")
    log.info("Admin %s pinned batch %s (%d chats, %d extra muted/expired)",
              msg.from_user.id, batch_id, len(msgs_), len(delivered))


# ── /broadcast ────────────────────────────────────────────────────────────

def cmd_broadcast(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    parts = msg.text.split(maxsplit=1)
    body  = parts[1].strip() if len(parts) > 1 else ""
    if not body and msg.reply_to_message:
        body = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not body:
        bot.reply_to(msg,
            "Usage: `/broadcast Your announcement here`\n"
            "Or reply to a message with `/broadcast`.",
            parse_mode="Markdown",
        )
        return
    _run_broadcast(msg.from_user.id, body)
    bot.reply_to(msg, "✅ Broadcast is being sent.")


def _run_broadcast(admin_uid: int, body: str):
    text = broadcast_message_text(body)

    def _do():
        # Broadcasts are staff announcements, not relayed content — every
        # reachable user gets them regardless of ban/mute/expired status.
        # Relay/welcome-media eligibility rules don't apply here.
        targets = all_reachable_users()
        batch   = str(uuid.uuid4())

        def _send_one(u):
            tid = u["user_id"]
            m = _safe(bot.send_message, tid, text, target_uid=tid)
            if m and hasattr(m, "message_id"):
                # Log every delivered copy so /pin and /delete (which look
                # messages up by batch) also work on broadcast announcements.
                log_relay(batch, admin_uid, tid, m.message_id)

        _parallel_dispatch(targets, _send_one)
        _safe(bot.send_message, admin_uid,
              f"📢 Broadcast delivered to *{len(targets)}* user(s).",
              parse_mode="Markdown", target_uid=admin_uid)
        log.info("Admin %s broadcast to %d users", admin_uid, len(targets))

    threading.Thread(target=_do, daemon=True).start()


# ── /delete ───────────────────────────────────────────────────────────────

def cmd_delete(msg: types.Message):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return
    if not msg.reply_to_message:
        bot.reply_to(msg, "Reply to a relayed message with /delete to remove it from all chats.")
        return
    row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
    if not row:
        bot.reply_to(msg, "❌ Message not found in relay log.")
        return
    batch_id   = row["batch_id"]
    sender_uid = row["sender_uid"]
    msgs_      = get_batch_msgs(batch_id)
    for m in msgs_:
        _safe(bot.delete_message, m["target_uid"], m["message_id"])
        time.sleep(0.02)
    delete_relay_log_batch(batch_id)

    # If this media earned its sender a time reward, reverse it now and say
    # exactly what was deleted and how much was deducted.
    reward = get_media_reward(batch_id)
    delete_media_reward(batch_id)

    sender      = get_user(sender_uid)
    sender_name = (sender["display_name"] or sender["random_id"]) if sender else str(sender_uid)
    media_label = {"photo": "📸 Photo", "video": "📹 Video"}.get(
        reward["media_type"] if reward else None, "🎞 Media"
    )

    lines = [
        f"🗑 *Deleted media from* *{md(sender_name)}*",
        f"Removed from *{len(msgs_)}* chat(s).",
    ]
    if reward and reward["earned_secs"] > 0:
        new_secs = subtract_access_time(sender_uid, reward["earned_secs"])
        lines.append("")
        lines.append(f"{media_label} reward reversed: *-{fmt_time(reward['earned_secs'])}*")
        lines.append(f"⏳ {md(sender_name)}'s new balance: *{fmt_time(new_secs)}*")
        threading.Thread(
            target=_notify_media_deleted,
            args=(sender_uid, reward["media_type"], reward["earned_secs"], new_secs),
            daemon=True,
        ).start()
    else:
        lines.append("")
        lines.append("ℹ️ No time reward was linked to this media — nothing deducted.")

    bot.reply_to(msg, "\n".join(lines), parse_mode="Markdown")
    log.info("Admin %s deleted batch %s (%d msgs, reward=%s)",
              msg.from_user.id, batch_id, len(msgs_), dict(reward) if reward else None)


# ── /prof ─────────────────────────────────────────────────────────────────

def cmd_prof(msg: types.Message):
    """
    Open a user's admin profile panel two ways besides the Users list:
      /prof username   — look up by @username
      (reply) /prof     — look up the sender of the relayed message replied to
    """
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ You don't have admin access.")
        return

    parts = msg.text.strip().split(maxsplit=1)
    arg   = parts[1].strip() if len(parts) > 1 else None

    target_uid = None
    if arg:
        uname = arg.lstrip("@").strip()
        if not uname:
            bot.reply_to(msg,
                "Usage:\n"
                "`/prof username` — view a profile by username\n"
                "Or reply to a relayed message with `/prof`.",
                parse_mode="Markdown",
            )
            return
        u = get_user_by_username(uname)
        if not u:
            bot.reply_to(msg, f"❌ No user found with username @{md(uname)}.", parse_mode="Markdown")
            return
        target_uid = u["user_id"]
    elif msg.reply_to_message:
        row = get_batch_by_msg(msg.from_user.id, msg.reply_to_message.message_id)
        if not row:
            bot.reply_to(msg, "❌ Could not find the original sender. (Only works when replying to a relayed message.)")
            return
        target_uid = row["sender_uid"]
    else:
        bot.reply_to(msg,
            "Usage:\n"
            "`/prof username` — view a profile by username\n"
            "Or reply to a relayed message with `/prof`.",
            parse_mode="Markdown",
        )
        return

    u = get_user(target_uid)
    if not u:
        bot.reply_to(msg, "❌ User not found.")
        return
    ref_count   = get_referral_count(target_uid)
    media_count = get_user_media_count(target_uid)
    bot.reply_to(msg, user_info_text(u, ref_count, media_count),
                 parse_mode="Markdown", reply_markup=user_action_keyboard(target_uid))


# ── /stats ─────────────────────────────────────────────────────────────────

def cmd_stats(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    bot.reply_to(msg, admin_panel_text(stats()), parse_mode="Markdown")


# ── Unknown command ────────────────────────────────────────────────────────

def cmd_unknown(msg: types.Message):
    bot.reply_to(msg, "❓ Unknown command. Use /help for the list of commands.")


# ── Callback handler ───────────────────────────────────────────────────────

def on_callback(call: types.CallbackQuery):
    uid  = call.from_user.id
    data = call.data

    if data == "noop":
        bot.answer_callback_query(call.id)
        return

    # ── Time balance ────────────────────────────────────────────────────────
    if data in ("time:check", "time:refresh"):
        if is_admin(uid):
            text  = "🛡 *Unlimited Access*\nYou are an admin — no time restrictions."
            kb    = None
        else:
            secs = get_access_seconds(uid)
            if secs > 0:
                warn = (
                    "\n\n⚠️ *Running low!* Send a photo or video to top up."
                    if secs < LOW_TIME_WARN else ""
                )
                text = (
                    "🔮 *Time Balance*\n"
                    "━━━━━━━━━━━━━━━━━\n\n"
                    f"⏱ Remaining: *{fmt_time(secs)}*\n"
                    f"`{time_bar(secs)}`{warn}\n\n"
                    "📸 Photo = +1 min  |  📹 1 MB video = +5 min\n"
                    "🔗 Invite friends for +2h each"
                )
            else:
                text = (
                    "❌ *No time remaining!*\n"
                    "━━━━━━━━━━━━━━━━━\n\n"
                    "📸 Photo = +1 min  |  📹 1 MB video = +5 min\n"
                    "🔗 Use /referral to invite friends for +2h"
                )
            kb = user_time_keyboard_refresh()
        try:
            bot.edit_message_text(text, call.message.chat.id,
                                  call.message.message_id,
                                  parse_mode="Markdown", reply_markup=kb)
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "time:howto":
        bot.answer_callback_query(
            call.id,
            "📸 Photo = +1 min\n📹 1 MB video = +5 min\n🔗 Referral = +2h for both",
            show_alert=True,
        )
        return

    # ── Referral ────────────────────────────────────────────────────────────
    if data in ("ref:link", "ref:stats"):
        row = get_user(uid)
        if not row:
            bot.answer_callback_query(call.id, "Please /start first."); return
        code      = get_referral_code(uid)
        ref_count = get_referral_count(uid)
        me   = bot.get_me()
        link = f"https://t.me/{me.username}?start={code}"
        text = (
            "🔗 *Your Referral Link*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            f"`{link}`\n\n"
            f"📊 Successful referrals: *{ref_count}*\n"
            f"⏰ Total earned: *{fmt_time(ref_count * REFERRAL_REWARD_SECS)}*\n\n"
            "Share the link — you *both* get +2h when someone joins!"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id,
                                  call.message.message_id,
                                  parse_mode="Markdown", reply_markup=referral_keyboard())
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown", reply_markup=referral_keyboard())
        bot.answer_callback_query(call.id)
        return

    # ── Admin back / refresh ────────────────────────────────────────────────
    if data == "admin:back":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            bot.edit_message_text(
                admin_panel_text(stats()),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=admin_keyboard(uid),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if data == "admin:stats":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        back_kb = types.InlineKeyboardMarkup()
        back_kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin:back"))
        try:
            bot.edit_message_text(
                admin_panel_text(stats()),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=back_kb,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Admin: backups ──────────────────────────────────────────────────────
    if data == "admin:backups":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        total_b, by_type = get_backup_stats()
        lines = [f"💾 *Media Backup Stats*\n━━━━━━━━━━━━━━━━━\n\nTotal: *{total_b}* files\n"]
        for r in by_type:
            sz_mb = (r["total_size"] or 0) / 1_048_576
            lines.append(f"  • {r['file_type']}: *{r['cnt']}* ({sz_mb:.1f} MB)")
        text = "\n".join(lines)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=backups_keyboard())
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown", reply_markup=backups_keyboard())
        bot.answer_callback_query(call.id)
        return

    # ── Users list ──────────────────────────────────────────────────────────
    if data.startswith("admin:users:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        _, total = all_users_paged(page)
        try:
            bot.edit_message_text(
                f"👥 *User List*\n━━━━━━━━━━━━━━━━━\nTotal: *{total}* members",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=users_keyboard(page),
            )
        except Exception as e:
            log.error("admin:users edit failed: %s", e)
        bot.answer_callback_query(call.id)
        return

    # ── User info ───────────────────────────────────────────────────────────
    if data.startswith("admin:userinfo:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        u = get_user(target)
        if not u:
            bot.answer_callback_query(call.id, "User not found"); return
        ref_count   = get_referral_count(target)
        media_count = get_user_media_count(target)
        try:
            bot.edit_message_text(
                user_info_text(u, ref_count, media_count),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=user_action_keyboard(target),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Ban ─────────────────────────────────────────────────────────────────
    if data.startswith("admin:ban:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        if target == MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "Cannot ban the main admin"); return
        ban_user(target)
        u    = get_user(target)
        name = (u["display_name"] or u["random_id"]) if u else str(target)
        bot.answer_callback_query(call.id, f"🔴 {name} banned")
        threading.Thread(target=_notify_banned, args=(target,), daemon=True).start()
        ref_count   = get_referral_count(target)
        media_count = get_user_media_count(target)
        try:
            bot.edit_message_text(
                user_info_text(get_user(target), ref_count, media_count),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=user_action_keyboard(target),
            )
        except Exception:
            pass
        return

    # ── Unban ───────────────────────────────────────────────────────────────
    if data.startswith("admin:unban:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        unban_user(target)
        u    = get_user(target)
        name = (u["display_name"] or u["random_id"]) if u else str(target)
        bot.answer_callback_query(call.id, f"🟢 {name} unbanned")
        threading.Thread(target=_notify_unbanned, args=(target,), daemon=True).start()
        ref_count   = get_referral_count(target)
        media_count = get_user_media_count(target)
        try:
            bot.edit_message_text(
                user_info_text(get_user(target), ref_count, media_count),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=user_action_keyboard(target),
            )
        except Exception:
            pass
        return

    # ── Mute — open the nested unit + stepper builder ───────────────────────
    if data.startswith("admin:mute:") and not data.startswith("admin:mutefor:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        if target == MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "Cannot mute the main admin"); return
        unit, val = "m", 5
        try:
            bot.edit_message_text(
                mute_builder_text(get_user(target), unit, val),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=mute_builder_keyboard(target, unit, val),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Mute builder — unit cycle / stepper / confirm / back ────────────────
    if data.startswith("admin:mu:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            _, _, target_s, unit, val_s, action = data.split(":")
            target = int(target_s)
            val    = int(val_s)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id); return

        target_row = get_user(target)
        if not target_row:
            bot.answer_callback_query(call.id, "User not found"); return

        if action == "back":
            ref_count   = get_referral_count(target)
            media_count = get_user_media_count(target)
            try:
                bot.edit_message_text(
                    user_info_text(target_row, ref_count, media_count),
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=user_action_keyboard(target),
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id)
            return

        if action == "cycle":
            idx  = MUTE_UNIT_CYCLE.index(unit) if unit in MUTE_UNIT_CYCLE else 0
            unit = MUTE_UNIT_CYCLE[(idx + 1) % len(MUTE_UNIT_CYCLE)]
            val  = _clamp_mute_val(unit, val)
        elif action == "inc":
            val = _clamp_mute_val(unit, val + _MUTE_STEP[unit])
        elif action == "dec":
            val = _clamp_mute_val(unit, val - _MUTE_STEP[unit])
        elif action == "apply":
            if target == MAIN_ADMIN_ID:
                bot.answer_callback_query(call.id, "Cannot mute the main admin"); return
            secs = val * _UNIT_SECONDS[unit]
            set_mute(target, secs)
            name = target_row["display_name"] or target_row["random_id"]
            bot.answer_callback_query(call.id, f"🔇 {name} muted for {fmt_time(secs)}")
            threading.Thread(target=_notify_muted, args=(target, secs, None), daemon=True).start()
            ref_count   = get_referral_count(target)
            media_count = get_user_media_count(target)
            try:
                bot.edit_message_text(
                    user_info_text(get_user(target), ref_count, media_count),
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=user_action_keyboard(target),
                )
            except Exception:
                pass
            return
        else:
            bot.answer_callback_query(call.id); return

        try:
            bot.edit_message_text(
                mute_builder_text(target_row, unit, val),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=mute_builder_keyboard(target, unit, val),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Unmute ──────────────────────────────────────────────────────────────
    if data.startswith("admin:unmute:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        clear_mute(target)
        u    = get_user(target)
        name = (u["display_name"] or u["random_id"]) if u else str(target)
        bot.answer_callback_query(call.id, f"🔊 {name} unmuted")
        threading.Thread(target=_notify_unmuted, args=(target,), daemon=True).start()
        ref_count   = get_referral_count(target)
        media_count = get_user_media_count(target)
        try:
            bot.edit_message_text(
                user_info_text(get_user(target), ref_count, media_count),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=user_action_keyboard(target),
            )
        except Exception:
            pass
        return

    # ── Usage Time — open the nested direction + unit + stepper builder ────
    if data.startswith("admin:usagetime:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        target_row = get_user(target)
        if not target_row:
            bot.answer_callback_query(call.id, "User not found"); return
        direction, unit, val = "add", "m", 30
        try:
            bot.edit_message_text(
                usage_time_builder_text(target_row, direction, unit, val),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
                reply_markup=usage_time_builder_keyboard(target, direction, unit, val),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Usage Time builder — direction / unit / stepper / confirm / back ───
    if data.startswith("admin:ut:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            _, _, target_s, direction, unit, val_s, action = data.split(":")
            target = int(target_s)
            val    = int(val_s)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id); return

        target_row = get_user(target)
        if not target_row:
            bot.answer_callback_query(call.id, "User not found"); return

        if action == "back":
            ref_count   = get_referral_count(target)
            media_count = get_user_media_count(target)
            try:
                bot.edit_message_text(
                    user_info_text(target_row, ref_count, media_count),
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=user_action_keyboard(target),
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id)
            return

        if action == "dir":
            direction = "sub" if direction == "add" else "add"
        elif action == "unit":
            unit = "h" if unit == "m" else "m"
            val  = _clamp_ut_val(unit, val)
        elif action == "inc":
            val = _clamp_ut_val(unit, val + _UT_STEP[unit])
        elif action == "dec":
            val = _clamp_ut_val(unit, val - _UT_STEP[unit])
        elif action == "apply":
            secs = val * (60 if unit == "m" else 3600)
            name = target_row["display_name"] or target_row["random_id"]
            if direction == "add":
                add_access_time(target, secs)
                sign, verb = "+", "added to"
            else:
                subtract_access_time(target, secs)
                sign, verb = "-", "removed from"
            new_secs = get_access_seconds(target)
            bot.answer_callback_query(call.id, f"⏰ {sign}{fmt_time(secs)} {verb} {name}")
            threading.Thread(
                target=_notify_time_adjusted, args=(target, direction, secs, new_secs), daemon=True
            ).start()
            ref_count   = get_referral_count(target)
            media_count = get_user_media_count(target)
            try:
                bot.edit_message_text(
                    user_info_text(get_user(target), ref_count, media_count),
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=user_action_keyboard(target),
                )
            except Exception:
                pass
            return
        else:
            bot.answer_callback_query(call.id); return

        try:
            bot.edit_message_text(
                usage_time_builder_text(target_row, direction, unit, val),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
                reply_markup=usage_time_builder_keyboard(target, direction, unit, val),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Direct Message — compose ────────────────────────────────────────────
    if data.startswith("admin:dm:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        t    = get_user(target)
        if not t:
            bot.answer_callback_query(call.id, "User not found"); return
        name = t["display_name"] or t["random_id"]
        with _awaiting_lock:
            _awaiting[uid] = {"action": "dm_compose", "target": target}
        bot.answer_callback_query(call.id)
        bot.send_message(
            uid,
            f"✉️ *Direct Message*\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"Type the message you want to send privately to *{md(name)}*.\n"
            f"It will not appear in the shared network chat — only they will see it.",
            parse_mode="Markdown",
        )
        return

    # ── Direct Message — send confirmed preview ─────────────────────────────
    if data.startswith("admin:dmsend:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            target = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        with _dm_pending_lock:
            pending = _dm_pending.pop(uid, None)
        if not pending or pending.get("target") != target:
            bot.answer_callback_query(call.id, "This preview has expired.", show_alert=True)
            return
        text_body = pending["text"]
        threading.Thread(target=_notify_direct_message, args=(target, text_body), daemon=True).start()
        t    = get_user(target)
        name = (t["display_name"] or t["random_id"]) if t else str(target)
        bot.answer_callback_query(call.id, f"✅ Sent to {name}")
        try:
            bot.edit_message_text(
                f"✅ *Direct message sent to {md(name)}.*",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
        log.info("Admin %s sent a direct message to %s", uid, target)
        return

    # ── Direct Message — cancel preview ─────────────────────────────────────
    if data.startswith("admin:dmcancel:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        with _dm_pending_lock:
            _dm_pending.pop(uid, None)
        bot.answer_callback_query(call.id, "Cancelled")
        try:
            bot.edit_message_text(
                "❌ *Direct message cancelled.*",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # ── Banned list ─────────────────────────────────────────────────────────
    if data == "admin:banned":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            bot.edit_message_text(
                "🔴 *Banned Users*\n━━━━━━━━━━━━━━━━━",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=banned_users_keyboard(0),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:banned_page:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            bot.answer_callback_query(call.id); return
        try:
            bot.edit_message_text(
                "🔴 *Banned Users*\n━━━━━━━━━━━━━━━━━",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=banned_users_keyboard(page),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Muted list ──────────────────────────────────────────────────────────
    if data == "admin:muted":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Access denied"); return
        rows = get_muted_users()
        if not rows:
            bot.answer_callback_query(call.id, "No muted users right now.", show_alert=True)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for u in rows:
            name = u["display_name"] or u["random_id"]
            remaining = mute_remaining_secs(u["user_id"])
            kb.add(types.InlineKeyboardButton(
                f"🔇 {name}  ·  {fmt_time(remaining)} left",
                callback_data=f"admin:userinfo:{u['user_id']}",
            ))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin:back"))
        try:
            bot.edit_message_text(
                f"🔇 *Muted Users* ({len(rows)} total)\n━━━━━━━━━━━━━━━━━",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ── Welcome media collection ──────────────────────────────────────────────
    if data == "welcome:start":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Admins only"); return
        with _collecting_welcome_lock:
            _collecting_welcome.add(uid)
        bot.answer_callback_query(call.id)
        current = count_welcome_media()
        # Clear any leftover reply keyboard from an older version of this flow
        # (this is what caused the "✅ Done" button to look permanently stuck
        # for some admins) before showing the new inline "Done" button.
        _safe(bot.send_message, uid, "⏳ Preparing upload session…",
              reply_markup=remove_keyboard(), target_uid=uid)
        bot.send_message(
            uid,
            "🎬 *Welcome Media Setup*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            f"Currently cached: *{current}* item(s)\n\n"
            "Send photos, videos, or GIFs one by one — each will be added to "
            "the pool new users get greeted with.\n"
            "These files are only cached for the welcome flow and are *never* "
            "relayed to the network.\n"
            "Tap *✅ Done* below when you're finished.",
            parse_mode="Markdown",
            reply_markup=welcome_collect_keyboard(),
        )
        return

    # ── Welcome media collection — finish (inline "✅ Done" button) ───────────
    if data == "welcome:done":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Admins only"); return
        with _collecting_welcome_lock:
            was_collecting = uid in _collecting_welcome
            _collecting_welcome.discard(uid)
        bot.answer_callback_query(call.id, "Done ✅")
        total = count_welcome_media()
        text = (
            f"✅ *Welcome media setup finished.*\n\n"
            f"Cached items: *{total}*\n"
            f"New users will now receive {min(WELCOME_MEDIA_COUNT, total)} random item(s) on /start."
        ) if was_collecting else (
            f"ℹ️ Upload session was already closed.\nCached items: *{total}*"
        )
        try:
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown")
        return

    # ── Broadcast (button flow) ───────────────────────────────────────────────
    if data == "broadcast:start":
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Admins only"); return
        with _awaiting_lock:
            _awaiting[uid] = {"action": "broadcast_msg"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            uid,
            "📢 *Broadcast*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "Type the message you want to send to every eligible user.\n"
            "It will be sent with attention emojis and large bold text automatically.",
            parse_mode="Markdown",
        )
        return

    # ── Add admin ───────────────────────────────────────────────────────────
    if data == "admin:addadmin":
        if not is_main_admin(uid):
            bot.answer_callback_query(call.id, "Main admin only"); return
        bot.answer_callback_query(call.id)
        bot.send_message(
            uid,
            "Reply to any relayed message with /addadmin to promote that user to admin.",
        )
        return

    # ── Media settings ──────────────────────────────────────────────────────
    if data == "media:show":
        if not is_main_admin(uid):
            bot.answer_callback_query(call.id, "Main admin only"); return
        ms = get_media_settings()
        try:
            bot.edit_message_text(
                media_settings_text(ms), call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=media_keyboard(ms),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if data.startswith("media:toggle:"):
        if not is_main_admin(uid):
            bot.answer_callback_query(call.id, "Main admin only"); return
        field = data.split(":")[-1]
        ms    = get_media_settings()
        set_media_field(field, 0 if ms.get(field) else 1)
        ms = get_media_settings()
        try:
            bot.edit_message_text(
                media_settings_text(ms), call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=media_keyboard(ms),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if data.startswith("media:setsize:"):
        if not is_main_admin(uid):
            bot.answer_callback_query(call.id, "Main admin only"); return
        which = data.split(":")[-1]
        with _awaiting_lock:
            _awaiting[uid] = {"action": f"video_{which}"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            uid,
            f"Send the {'minimum' if which == 'min' else 'maximum'} video size in MB "
            f"(e.g. `5` for 5 MB). Send `0` to remove the limit.",
            parse_mode="Markdown",
        )
        return

    # ── User main menu ───────────────────────────────────────────────────────
    if data == "user:menu":
        row = get_user(uid)
        if not row:
            bot.answer_callback_query(call.id, "Please /start first."); return
        n    = user_count()
        secs = get_access_seconds(uid) if row["role"] == 0 else -1
        if is_admin(uid):
            time_line = "🛡 Admin — unlimited access"
            kb        = admin_keyboard(uid)
        elif secs > 0:
            time_line = f"⏳ Time balance: *{fmt_time(secs)}*\n`{time_bar(secs)}`"
            kb        = user_main_keyboard()
        else:
            time_line = "⏳ Time balance: *0 min* — send media to earn time"
            kb        = user_main_keyboard()
        text = (
            f"✦ *NightVi*\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Your ID: `{row['random_id']}`\n"
            f"👥 Members: *{n}*\n"
            f"{time_line}\n\n"
            f"📨 Every message is delivered anonymously.\n"
            f"📸 Photo → *+1 min*  ·  📹 1 MB video → *+5 min*"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=kb)
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    # ── Profile view ─────────────────────────────────────────────────────────
    if data == "profile:show":
        row = get_user(uid)
        if not row:
            bot.answer_callback_query(call.id, "Please /start first."); return
        name = row["display_name"] or row["random_id"]
        secs = get_access_seconds(uid) if row["role"] == 0 else -1
        ref_count = get_referral_count(uid)
        if secs >= 0:
            time_line = f"⏱ *{fmt_time(secs)}* remaining\n`{time_bar(secs)}`"
        else:
            time_line = "🛡 Unlimited (Admin)"
        text = (
            f"👤 *Profile*\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 ID: `{row['random_id']}`\n"
            f"📛 Name: *{md(name)}*\n\n"
            f"{time_line}\n\n"
            f"🔗 Referrals: *{ref_count}* invited\n\n"
            f"_To change your name, tap the button below._"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=profile_keyboard())
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown", reply_markup=profile_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "profile:setname":
        with _awaiting_lock:
            _awaiting[uid] = {"action": "set_name"}
        bot.answer_callback_query(call.id)
        bot.send_message(uid,
            "✏️ *Set Display Name*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "Just type and send your new display name:\n"
            "_Max 32 characters. Must be unique._",
            parse_mode="Markdown",
        )
        return

    # ── Leave network (with confirmation) ────────────────────────────────────
    if data == "user:leave":
        row = get_user(uid)
        if not row or not row["active"]:
            bot.answer_callback_query(call.id, "You're not in the network."); return
        text = (
            "🚪 *Leave NightVi?*\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "Your messages will no longer be relayed.\n"
            "You can rejoin anytime with /start."
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=leave_confirm_keyboard())
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown",
                             reply_markup=leave_confirm_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "user:leave_confirm":
        row = get_user(uid)
        if not row or not row["active"]:
            bot.answer_callback_query(call.id, "You're not in the network."); return
        deactivate_user(uid)
        text = "✅ *You've left NightVi.*\n\nSend /start anytime to rejoin."
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  parse_mode="Markdown")
        except Exception:
            bot.send_message(uid, text, parse_mode="Markdown")
        bot.answer_callback_query(call.id, "You have left NightVi.")
        log.info("User %s left via button", uid)
        return

    # ── Help (inline) ────────────────────────────────────────────────────────
    if data == "user:help":
        back_kb = types.InlineKeyboardMarkup()
        back_kb.add(types.InlineKeyboardButton("🔙   Back to Menu", callback_data="user:menu"))
        try:
            bot.edit_message_text(
                _help_text(is_admin(uid)),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=back_kb,
            )
        except Exception:
            bot.send_message(uid, _help_text(is_admin(uid)), parse_mode="Markdown",
                             reply_markup=back_kb)
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


# ── Main message handler ───────────────────────────────────────────────────

def handle_message(msg: types.Message):
    uid = msg.from_user.id if msg.from_user else None
    if not uid:
        return
    if msg.text and msg.text.startswith("/"):
        return

    row = get_user(uid)

    # ── Welcome media collection mode (admin-only, separate from relay) ──────
    with _collecting_welcome_lock:
        collecting = uid in _collecting_welcome
    if collecting:
        if not is_admin(uid):
            # Safety net: admin role revoked mid-session. This branch used to
            # fall through to the relay/broadcast path below after discarding
            # the user from collection mode — that's exactly why the welcome
            # media (and any leftover "✅ Done" button press) could end up
            # relayed to every user in the network. We now always stop here
            # instead of letting anything from a collection-mode session leak
            # into the normal message flow.
            with _collecting_welcome_lock:
                _collecting_welcome.discard(uid)
            bot.reply_to(msg,
                "ℹ️ Your admin access changed, so welcome-media upload mode was closed.",
                reply_markup=remove_keyboard(),
            )
            return
        elif msg.text and msg.text.strip() == "✅ Done":
            # Legacy fallback only: older clients may still have the retired
            # reply-keyboard "✅ Done" button stuck on screen. Honor it once,
            # finish the same way the new inline "Done ✅" button does, and
            # explicitly clear the stuck reply keyboard so it stops appearing.
            with _collecting_welcome_lock:
                _collecting_welcome.discard(uid)
            total = count_welcome_media()
            bot.reply_to(msg,
                f"✅ *Welcome media setup finished.*\n\n"
                f"Cached items: *{total}*\n"
                f"New users will now receive {min(WELCOME_MEDIA_COUNT, total)} random item(s) on /start.",
                parse_mode="Markdown",
                reply_markup=remove_keyboard(),
            )
            return
        else:
            file_id = None
            file_type = None
            if msg.photo:
                file_id, file_type = msg.photo[-1].file_id, "photo"
            elif msg.video:
                file_id, file_type = msg.video.file_id, "video"
            elif msg.animation:
                file_id, file_type = msg.animation.file_id, "animation"

            if file_id:
                add_welcome_media(file_id, file_type, uid)
                bot.reply_to(msg,
                    "✅ This file has been added to your list✔️\n"
                    "Send a new file, or tap *Done* to finish.",
                    parse_mode="Markdown",
                    reply_markup=welcome_collect_keyboard(),
                )
            else:
                bot.reply_to(msg,
                    "⚠️ Please send a photo, video, or GIF — or tap *Done* to finish.",
                    parse_mode="Markdown",
                    reply_markup=welcome_collect_keyboard(),
                )
            return

    # ── Awaiting input (admin video size setting, broadcast, etc.) ───────────
    with _awaiting_lock:
        aw = _awaiting.pop(uid, None) if msg.text else None
    if aw is not None:
        if aw["action"] == "set_name":
            name = (msg.text or "").strip()[:32]
            if not name:
                bot.reply_to(msg, "❌ Name cannot be empty. Try again.")
            elif set_display_name(uid, name):
                bot.reply_to(msg, f"✅ Display name set to *{md(name)}*",
                             parse_mode="Markdown")
            else:
                bot.reply_to(msg,
                    f"❌ The name *{md(name)}* is already taken. Try a different name.",
                    parse_mode="Markdown")
        elif aw["action"] == "broadcast_msg":
            body = (msg.text or "").strip()
            if not body:
                bot.reply_to(msg, "❌ Broadcast text cannot be empty.")
            else:
                _run_broadcast(uid, body)
                bot.reply_to(msg, "✅ Broadcast is being sent.")
        elif aw["action"] == "dm_compose":
            target    = aw.get("target")
            text_body = (msg.text or "").strip()
            if not text_body:
                bot.reply_to(msg, "❌ Message cannot be empty. Try again.")
                with _awaiting_lock:
                    _awaiting[uid] = aw
            else:
                with _dm_pending_lock:
                    _dm_pending[uid] = {"target": target, "text": text_body}
                t        = get_user(target)
                tgt_name = (t["display_name"] or t["random_id"]) if t else str(target)
                preview_kb = types.InlineKeyboardMarkup(row_width=2)
                preview_kb.add(
                    types.InlineKeyboardButton("✅ Send", callback_data=f"admin:dmsend:{target}"),
                    types.InlineKeyboardButton("❌ Cancel", callback_data=f"admin:dmcancel:{target}"),
                )
                bot.reply_to(msg,
                    f"✉️ *Preview — Direct Message to {md(tgt_name)}*\n"
                    f"━━━━━━━━━━━━━━━━━\n\n"
                    f"{md(text_body)}\n\n"
                    f"Send this message?",
                    parse_mode="Markdown",
                    reply_markup=preview_kb,
                )
        else:
            try:
                mb    = float(msg.text.strip())
                field = "min_video_bytes" if aw["action"] == "video_min" else "max_video_bytes"
                set_media_field(field, int(mb * 1048576))
                label = "minimum" if aw["action"] == "video_min" else "maximum"
                bot.reply_to(msg,
                    f"✅ Video {label} size set to {mb:.0f} MB."
                    if mb > 0 else
                    f"✅ Video {label} size limit removed."
                )
            except ValueError:
                bot.reply_to(msg, "❌ Please send a number (e.g. `5`).", parse_mode="Markdown")
        return

    if not row or not row["active"]:
        bot.reply_to(msg, "You're not in the network. Send /start to join.")
        return

    if row["is_banned"]:
        bot.reply_to(msg, "🚫 You are banned.")
        return

    if is_muted(uid):
        remaining = mute_remaining_secs(uid)
        if remaining > 0:
            bot.reply_to(msg, f"🔇 You are muted. Unmute in: *{fmt_time(remaining)}*",
                         parse_mode="Markdown")
        return

    if check_spam(uid):
        set_mute(uid, MUTE_SECONDS)
        bot.reply_to(msg, "⚠️ You're sending too fast. You have been muted for 30 seconds.")
        return

    touch_user(uid)

    # ── Duplicate media check ────────────────────────────────────────────────
    has_media = (msg.photo or msg.video or msg.document or msg.audio
                 or msg.voice or msg.animation or msg.sticker or msg.video_note)
    if has_media and is_duplicate_media(msg):
        log.info("Duplicate media blocked from uid=%s", uid)
        bot.reply_to(msg,
            "⚠️ *Duplicate media*\n\n"
            "This file was already sent previously and cannot be relayed again.",
            parse_mode="Markdown",
        )
        return

    # This batch_id is generated up front (rather than inside relay_message)
    # so the media_rewards row recorded below and the relay_log rows written
    # during relay share the same id — that's how /delete later finds out
    # exactly how much earned time to reverse for a given piece of media.
    batch_id = str(uuid.uuid4())

    # ── Time-earning from photos ──────────────────────────────────────────────
    if msg.photo and not is_admin(uid):
        was_expired = add_access_time_returning_was_expired(uid, PHOTO_REWARD_SECS)
        remaining = get_access_seconds(uid)
        if PHOTO_REWARD_SECS > 0:
            record_media_reward(batch_id, uid, "photo", PHOTO_REWARD_SECS)
        bot.reply_to(msg,
            f"📸 *+{fmt_time(PHOTO_REWARD_SECS)}* added!\n"
            f"⏳ Balance: *{fmt_time(remaining)}*\n"
            f"`{time_bar(remaining)}`",
            parse_mode="Markdown",
            reply_markup=user_time_keyboard_refresh(),
        )
        if was_expired:
            _greet_returning_user(uid)

    # ── Time-earning from videos ──────────────────────────────────────────────
    elif msg.video and not is_admin(uid):
        size_bytes  = msg.video.file_size or 0
        mb          = size_bytes / BYTES_PER_MIN
        earned_secs = int(mb * VIDEO_REWARD_PER_MB)

        # ── Small-video streak check ────────────────────────────────────────
        # Warns the user if they send too many small videos (under the
        # threshold) back-to-back — resets whenever a normal-size video
        # breaks the streak, and resets again right after warning.
        if mb < SMALL_VIDEO_MB_THRESHOLD:
            with _small_video_streak_lock:
                streak = _small_video_streak.get(uid, 0) + 1
                if streak >= SMALL_VIDEO_STREAK_LIMIT:
                    _small_video_streak[uid] = 0
                    fire_warning = True
                else:
                    _small_video_streak[uid] = streak
                    fire_warning = False
            if fire_warning:
                bot.reply_to(msg,
                    f"⚠️ *Notice:* You've sent {SMALL_VIDEO_STREAK_LIMIT} small videos "
                    f"(under {SMALL_VIDEO_MB_THRESHOLD} MB) in a row.\n"
                    "Please avoid sending too many low-size videos back-to-back.",
                    parse_mode="Markdown",
                )
        else:
            with _small_video_streak_lock:
                _small_video_streak[uid] = 0

        if earned_secs > 0:
            was_expired = add_access_time_returning_was_expired(uid, earned_secs)
            remaining = get_access_seconds(uid)
            record_media_reward(batch_id, uid, "video", earned_secs)
            bot.reply_to(msg,
                f"📹 *+{fmt_time(earned_secs)}* added!\n"
                f"⏳ Balance: *{fmt_time(remaining)}*\n"
                f"`{time_bar(remaining)}`",
                parse_mode="Markdown",
                reply_markup=user_time_keyboard_refresh(),
            )
            if was_expired:
                _greet_returning_user(uid)
        else:
            bot.reply_to(msg,
                "ℹ️ Video too small to earn time.\nMinimum: 1 MB = 5 minutes.",
                parse_mode="Markdown",
            )

    # ── Access check ──────────────────────────────────────────────────────────
    if not is_admin(uid) and not has_access(uid):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔮 My Balance", callback_data="time:check"))
        kb.add(types.InlineKeyboardButton("🔗 Get Referral Link", callback_data="ref:link"))
        bot.reply_to(msg,
            "❌ *Your access time has expired!*\n\n"
            "📸 Photo → +1 min  |  📹 1 MB video → +5 min\n"
            "🔗 Invite friends → +2h for both of you",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    # ── Low time warning ──────────────────────────────────────────────────────
    if not is_admin(uid) and not (msg.photo or msg.video):
        secs = get_access_seconds(uid)
        if 0 < secs < LOW_TIME_WARN:
            bot.reply_to(msg,
                f"⚠️ Only *{fmt_time(secs)}* left!\n"
                f"`{time_bar(secs)}`\n"
                "Send a photo or video to top up.",
                parse_mode="Markdown",
                reply_markup=user_time_keyboard_refresh(),
            )

    # ── Media policy ──────────────────────────────────────────────────────────
    ms  = get_media_settings()
    err = _check_media_allowed(msg, ms)
    if err:
        bot.reply_to(msg, f"❌ {err}")
        return

    # ── Automatic media backup (non-blocking) ─────────────────────────────────
    if has_media:
        backup_message_media(bot, msg, uid)

    # ── Relay ─────────────────────────────────────────────────────────────────
    # Filter once, up front, so the confirmation count below always matches
    # exactly who receives the message — banned/muted/expired users are
    # excluded here and never even handed to relay_message.
    targets      = [t for t in active_users(exclude_id=uid) if _is_eligible_recipient(t)]
    target_count = len(targets)
    threading.Thread(
        target=relay_message,
        args=(uid, msg.chat.id, msg, targets, batch_id),
        daemon=True,
    ).start()

    # Confirmation for admins (regular users already see time-earn replies)
    if is_admin(uid):
        _safe(bot.reply_to, msg, f"✓ Relayed to *{target_count}* member(s).",
              parse_mode="Markdown", target_uid=uid)


# ── Media policy check ────────────────────────────────────────────────────

def _check_media_allowed(message, ms):
    if message.text:
        return None if ms["allow_text"] else "Text messages are not allowed."
    if message.photo:
        return None if ms["allow_photo"] else "Photos are not allowed."
    if message.animation:
        return None if ms["allow_animation"] else "GIFs are not allowed."
    if message.sticker:
        return None if ms["allow_sticker"] else "Stickers are not allowed."
    if message.voice:
        return None if ms["allow_voice"] else "Voice messages are not allowed."
    if message.audio:
        return None if ms["allow_audio"] else "Audio files are not allowed."
    if message.document:
        return None if ms["allow_document"] else "Files are not allowed."
    if message.video:
        if not ms["allow_video"]:
            return "Videos are not allowed."
        size   = message.video.file_size or 0
        mn, mx = ms["min_video_bytes"], ms["max_video_bytes"]
        if mn and size < mn:
            return f"Video too small (min {mn // 1048576} MB)."
        if mx and size > mx:
            return f"Video too large (max {mx // 1048576} MB)."
    return None
