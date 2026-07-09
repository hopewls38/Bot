# update_notifier.py — one-time update announcement + legacy user seeding

import time
import logging
import threading

log = logging.getLogger("relay")

# ── Settings ─────────────────────────────────────────────────────────────────
UPDATE_EVENT_KEY  = "update_v2_notified"
UPDATE_BONUS_SECS = 10 * 3600   # 10-hour gift

# Legacy user IDs (recovered from Railway logs)
# Note: 1336613182 intentionally excluded — was banned by an admin (log: banned user 1336613182)
SEED_USER_IDS = [
    1369887647, 1369983594, 1519357100, 1624684502,
    1736220898, 5064884202, 5080959393, 5136399172, 5246305930,
    5342331549, 5512702105, 5558703884, 5709329487, 6177571267,
    6356704820, 6701151296, 6840427088, 6996269744, 6998377151,
    7159656944, 7169807767, 7203894120, 7207868101, 7344036138,
    7693592856, 7937682707, 8064636075, 8077963486, 8237475102,
    8270820850, 8332996579, 8499458788, 8556699196, 8654227500,
    8997662540,
]

UPDATE_MESSAGE = (
    "🔔 *The bot has been updated\\!*\n"
    "━━━━━━━━━━━━━━━━━\n\n"
    "Hey 👋 the bot just got a fresh update with new features:\n\n"
    "🔗 *Invite friends = free time*\n"
    "Every successful invite adds 2 hours for you and your friend\n\n"
    "📸 *Every photo = 1 minute*\n"
    "🎬 *Every MB of video = 5 minutes*\n\n"
    "🎨 *Improved design and interface*\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "🎁 As an update gift, *10 hours* were added to your time balance\\!\n"
    "To check your balance: /id"
)
# ─────────────────────────────────────────────────────────────────────────────


def _safe_send(bot, uid: int, text: str) -> bool:
    try:
        bot.send_message(uid, text, parse_mode="MarkdownV2")
        return True
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ("blocked", "deactivated", "not found", "forbidden")):
            log.info("update_notifier: uid %s unreachable", uid)
        else:
            log.warning("update_notifier: send error uid=%s: %s", uid, e)
        return False


def _do_notify(bot):
    try:
        from database import (
            has_bot_event, set_bot_event,
            get_all_non_banned_user_ids,
            add_access_time, upsert_user,
        )

        if has_bot_event(UPDATE_EVENT_KEY):
            log.info("update_notifier: already fired — skip")
            return

        log.info("update_notifier: starting...")

        # Register legacy users in PostgreSQL (if not already present)
        for uid in SEED_USER_IDS:
            try:
                upsert_user(uid, "")
            except Exception as e:
                log.warning("update_notifier: seed error uid=%s: %s", uid, e)

        # Fetch every user id from the DB
        user_ids = get_all_non_banned_user_ids()
        log.info("update_notifier: %d users to notify", len(user_ids))

        sent = 0
        for uid in user_ids:
            try:
                add_access_time(uid, UPDATE_BONUS_SECS)
            except Exception as e:
                log.warning("update_notifier: add_time error uid=%s: %s", uid, e)

            if _safe_send(bot, uid, UPDATE_MESSAGE):
                sent += 1
            time.sleep(0.08)

        set_bot_event(UPDATE_EVENT_KEY)
        log.info("update_notifier: done — sent=%d / %d", sent, len(user_ids))

    except Exception as e:
        log.error("update_notifier: crashed: %s", e, exc_info=True)


def run_update_notifier(bot):
    threading.Thread(target=_do_notify, args=(bot,), daemon=True,
                     name="update-notifier").start()
