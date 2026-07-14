# main.py — Entry point for the anonymous relay bot

import os
import sys
import signal
import threading
import logging

import telebot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("relay")

from config import RELAY_TOKEN, DATABASE_URL
from database import init_db, cleanup_old_relay_log

if not RELAY_TOKEN:
    raise RuntimeError("RELAY_BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set — add PostgreSQL to Railway")

bot = telebot.TeleBot(RELAY_TOKEN, threaded=True)

import handlers as h
h.bot = bot

from handlers import (
    cmd_start, cmd_help, cmd_id, cmd_referral, cmd_leave, cmd_name,
    cmd_admin, cmd_users, cmd_ban, cmd_unban, cmd_mute, cmd_addadmin,
    cmd_del, cmd_cancel_del, cmd_delete, cmd_stats, cmd_unknown,
    cmd_pin, cmd_broadcast, cmd_prof,
    on_callback, handle_message,
    prune_memory_state, check_low_time_users, remind_expired_users, _shutdown,
)

bot.register_message_handler(cmd_start,    commands=["start"],               pass_bot=False)
bot.register_message_handler(cmd_help,     commands=["help"],                pass_bot=False)
bot.register_message_handler(cmd_id,       commands=["id", "status", "me"],  pass_bot=False)
bot.register_message_handler(cmd_referral, commands=["referral", "ref"],     pass_bot=False)
bot.register_message_handler(cmd_leave,    commands=["leave"],               pass_bot=False)
bot.register_message_handler(cmd_name,     commands=["name"],                pass_bot=False)
bot.register_message_handler(cmd_admin,    commands=["admin"],               pass_bot=False)
bot.register_message_handler(cmd_users,    commands=["users"],               pass_bot=False)
bot.register_message_handler(cmd_ban,      commands=["ban"],                 pass_bot=False)
bot.register_message_handler(cmd_unban,    commands=["unban"],               pass_bot=False)
bot.register_message_handler(cmd_mute,     commands=["mute"],                pass_bot=False)
bot.register_message_handler(cmd_addadmin, commands=["addadmin"],            pass_bot=False)
bot.register_message_handler(cmd_del,      commands=["del"],                 pass_bot=False)
bot.register_message_handler(cmd_delete,   commands=["delete", "Delete"],    pass_bot=False)
bot.register_message_handler(cmd_pin,      commands=["pin"],                 pass_bot=False)
bot.register_message_handler(cmd_broadcast, commands=["broadcast"],          pass_bot=False)
bot.register_message_handler(cmd_stats,    commands=["stats"],               pass_bot=False)
bot.register_message_handler(cmd_prof,     commands=["prof"],                pass_bot=False)
bot.register_message_handler(
    cmd_unknown,
    func=lambda m: m.text and m.text.startswith("/"),
    pass_bot=False,
)
bot.register_message_handler(
    handle_message,
    func=lambda m: True,
    content_types=telebot.util.content_type_media + ["text"],
    pass_bot=False,
)
bot.register_callback_query_handler(on_callback, func=lambda c: True, pass_bot=False)


def _maintenance_loop():
    tick = 0
    while not _shutdown.is_set():
        try:
            _shutdown.wait(timeout=300)
            if _shutdown.is_set():
                break
            tick += 1
            check_low_time_users()
            if tick % 6 == 0:
                prune_memory_state()
                cleanup_old_relay_log()
                log.info("Maintenance: done")
            # Every ~6h (72 ticks * 300s = 21600s), nudge expired-time users.
            if tick % 72 == 0:
                remind_expired_users()
        except Exception as e:
            log.warning("Maintenance loop error: %s", e)


def _handle_shutdown(signum, frame):
    log.info("Signal %s — shutting down...", signum)
    _shutdown.set()
    try:
        bot.stop_polling()
    except Exception:
        pass
    try:
        from handlers import _greet_executor
        _greet_executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    init_db()
    log.info("Bot starting... token=...%s", RELAY_TOKEN[-6:])

    bot.delete_webhook(drop_pending_updates=False)
    threading.Thread(target=_maintenance_loop, daemon=True).start()

    # Update announcement + legacy user seeding (runs only once, ever)
    from update_notifier import run_update_notifier, run_deploy_notifier
    run_update_notifier(bot)

    # Notify the main admin every time the bot is redeployed/restarted
    run_deploy_notifier(bot)

    log.info("Polling started.")
    backoff = 5
    while not _shutdown.is_set():
        try:
            bot.infinity_polling(
                timeout=20,
                long_polling_timeout=10,
                allowed_updates=["message", "callback_query"],
            )
            backoff = 5
        except Exception as e:
            if _shutdown.is_set():
                break
            log.error("Polling crashed: %s — retry in %ss", e, backoff)
            _shutdown.wait(backoff)
            backoff = min(backoff * 2, 60)

    log.info("Bot stopped.")
