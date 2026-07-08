# config.py — Central configuration for the relay bot

import os

# ── Core ─────────────────────────────────────────────────────────────────────
RELAY_TOKEN       = os.environ.get("RELAY_BOT_TOKEN", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "")          # PostgreSQL URL
MAIN_ADMIN_ID     = int(os.environ.get("MAIN_ADMIN_ID", "7344036138"))
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0"))   # 0 = disabled

# ── Timing ───────────────────────────────────────────────────────────────────
SEND_DELAY        = 0.05
SPAM_WINDOW       = 30
SPAM_LIMIT        = 20
MUTE_SECONDS      = 300
DEL_COUNTDOWN     = 600
GRACE_SECONDS     = 3600
LOW_TIME_WARN     = 600

# ── Time-earning rates ────────────────────────────────────────────────────────
BYTES_PER_MIN         = 1_048_576
PHOTO_REWARD_SECS     = 60
VIDEO_REWARD_PER_MB   = 300
REFERRAL_REWARD_SECS  = 7200

# ── Whitelisted fields for set_media_field ────────────────────────────────────
MEDIA_FIELDS = frozenset({
    "allow_text", "allow_photo", "allow_video", "allow_animation",
    "allow_sticker", "allow_voice", "allow_audio", "allow_document",
    "min_video_bytes", "max_video_bytes",
})
