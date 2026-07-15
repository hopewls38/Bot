# config.py — Central configuration for the relay bot

import os

# ── Core ─────────────────────────────────────────────────────────────────────
RELAY_TOKEN       = os.environ.get("RELAY_BOT_TOKEN", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "")          # PostgreSQL URL
MAIN_ADMIN_ID     = int(os.environ.get("MAIN_ADMIN_ID", "7344036138"))
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0"))   # 0 = disabled

# ── Timing ───────────────────────────────────────────────────────────────────
SEND_DELAY        = 0.05
SPAM_WINDOW       = 35
SPAM_LIMIT        = 40
MUTE_SECONDS      = 30
DEL_COUNTDOWN     = 600
GRACE_SECONDS     = 3600
LOW_TIME_WARN     = 600

# ── Time-earning rates ────────────────────────────────────────────────────────
BYTES_PER_MIN         = 1_048_576
PHOTO_REWARD_SECS     = 60
VIDEO_REWARD_PER_MB   = 300
REFERRAL_REWARD_SECS  = 7200

# ── Small-video streak warning ────────────────────────────────────────────────
# If a user sends this many videos in a row that are each under the size
# threshold below, the bot sends them an automatic warning notice.
SMALL_VIDEO_MB_THRESHOLD = 5
SMALL_VIDEO_STREAK_LIMIT = 10

# ── Whitelisted fields for set_media_field ────────────────────────────────────
MEDIA_FIELDS = frozenset({
    "allow_text", "allow_photo", "allow_video", "allow_animation",
    "allow_sticker", "allow_voice", "allow_audio", "allow_document",
    "min_video_bytes", "max_video_bytes",
})

# ── Welcome media ────────────────────────────────────────────────────────────
# How many cached welcome media items are sent to a brand-new user.
WELCOME_MEDIA_COUNT = 5

# ── Performance ──────────────────────────────────────────────────────────────
# Number of worker threads used to fan a single message out to many chats at
# once (relay, broadcast, welcome media). Higher = faster delivery, but stays
# well under Telegram's global rate limit (~30 msg/sec) for typical group sizes.
RELAY_WORKERS = 8

# ── Expired-time reminders ───────────────────────────────────────────────────
# Minimum gap between two reminder DMs to the same expired user. Combined
# with the maintenance loop's own 6h cadence, this is a hard per-user floor
# so a clock/tick drift can never turn into a spam loop.
EXPIRED_REMINDER_INTERVAL_SECS = 6 * 3600
