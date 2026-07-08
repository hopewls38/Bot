# backup_manager.py — Automatic media backup logic

import logging
import threading
from telebot import types
from database import save_backup, update_backup_msg_id
from config import BACKUP_CHANNEL_ID

log = logging.getLogger("relay")

# Media types we back up, mapped to (file_unique_id_attr, file_id_attr, size_attr)
_MEDIA_MAP = {
    "photo":     ("photo[-1].file_unique_id", "photo[-1].file_id", "photo[-1].file_size"),
    "video":     ("video.file_unique_id",     "video.file_id",     "video.file_size"),
    "document":  ("document.file_unique_id",  "document.file_id",  "document.file_size"),
    "audio":     ("audio.file_unique_id",     "audio.file_id",     "audio.file_size"),
    "voice":     ("voice.file_unique_id",     "voice.file_id",     "voice.file_size"),
    "animation": ("animation.file_unique_id", "animation.file_id", "animation.file_size"),
    "sticker":   ("sticker.file_unique_id",   "sticker.file_id",   "sticker.file_size"),
    "video_note":("video_note.file_unique_id","video_note.file_id","video_note.file_size"),
}


def _get_media_info(message: types.Message):
    """Extract (file_unique_id, file_id, file_type, file_size) from a message, or None."""
    if message.photo:
        p = message.photo[-1]
        return p.file_unique_id, p.file_id, "photo", p.file_size or 0
    if message.video:
        v = message.video
        return v.file_unique_id, v.file_id, "video", v.file_size or 0
    if message.document:
        d = message.document
        return d.file_unique_id, d.file_id, "document", d.file_size or 0
    if message.audio:
        a = message.audio
        return a.file_unique_id, a.file_id, "audio", a.file_size or 0
    if message.voice:
        v = message.voice
        return v.file_unique_id, v.file_id, "voice", v.file_size or 0
    if message.animation:
        a = message.animation
        return a.file_unique_id, a.file_id, "animation", a.file_size or 0
    if message.sticker:
        s = message.sticker
        return s.file_unique_id, s.file_id, "sticker", s.file_size or 0
    if message.video_note:
        vn = message.video_note
        return vn.file_unique_id, vn.file_id, "video_note", vn.file_size or 0
    return None


def backup_message_media(bot, message: types.Message, sender_uid: int):
    """
    Attempt to back up media from a message.
    - Checks for duplicates using file_unique_id.
    - If BACKUP_CHANNEL_ID is configured, forwards to that channel.
    - Stores metadata in the database.
    - Runs in a background thread so it never blocks the bot.
    """
    info = _get_media_info(message)
    if not info:
        return  # No backupable media

    def _do_backup():
        file_unique_id, file_id, file_type, file_size = info
        try:
            # Attempt the DB insert first — the UNIQUE constraint on file_unique_id
            # guarantees exactly-once semantics even under concurrent uploads.
            # save_backup returns True only when a new row was actually inserted.
            inserted = save_backup(
                file_unique_id=file_unique_id,
                file_id=file_id,
                file_type=file_type,
                sender_uid=sender_uid,
                file_size=file_size,
                backup_msg_id=None,  # will update below if channel forward succeeds
            )
            if not inserted:
                log.debug("Backup skipped (duplicate): %s", file_unique_id)
                return

            # Only forward to the backup channel after a confirmed new insert.
            backup_msg_id = None
            if BACKUP_CHANNEL_ID:
                try:
                    sent = bot.forward_message(
                        chat_id=BACKUP_CHANNEL_ID,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                    )
                    backup_msg_id = sent.message_id if sent else None
                    if backup_msg_id:
                        update_backup_msg_id(file_unique_id, backup_msg_id)
                except Exception as fwd_err:
                    log.warning("Backup forward failed: %s", fwd_err)

            log.info("Backed up %s (%d bytes) from uid=%s", file_type, file_size, sender_uid)
        except Exception as e:
            log.error("Backup error for uid=%s: %s", sender_uid, e, exc_info=True)

    threading.Thread(target=_do_backup, daemon=True).start()


def is_duplicate_media(message: types.Message) -> bool:
    """Return True if the media in this message was already backed up."""
    from database import backup_exists
    info = _get_media_info(message)
    if not info:
        return False
    return backup_exists(info[0])
