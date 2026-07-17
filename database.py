# database.py — PostgreSQL version (replaces SQLite)

import threading
import random
import string
import logging
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras

from config import DATABASE_URL, MAIN_ADMIN_ID, GRACE_SECONDS, MEDIA_FIELDS

log = logging.getLogger("relay")

_db_lock = threading.Lock()


# ── Connection ────────────────────────────────────────────────────────────────

def _conn():
    conn = psycopg2.connect(DATABASE_URL,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(k=8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=k))


def _gen_referral_code() -> str:
    return "ref_" + _gen_id(10)


# ── Schema init ───────────────────────────────────────────────────────────────

def init_db():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id        BIGINT  PRIMARY KEY,
                    username       TEXT    DEFAULT '',
                    display_name   TEXT    DEFAULT '',
                    random_id      TEXT    NOT NULL,
                    referral_code  TEXT    UNIQUE,
                    joined_at      TEXT    NOT NULL,
                    active         INTEGER DEFAULT 1,
                    is_banned      INTEGER DEFAULT 0,
                    role           INTEGER DEFAULT 0,
                    muted_until    TEXT    DEFAULT NULL,
                    last_seen      TEXT    DEFAULT NULL,
                    access_until   TEXT    DEFAULT NULL
                )
            """)
            # Tracks the last time we DM'd a time-expired user a reminder, so
            # the 6-hourly reminder job never re-sends more often than that.
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_expired_reminder TEXT DEFAULT NULL
            """)

            # Cheap, indexed eligibility lookups keep the admin panel / relay
            # confirmation counts and the reminder job fast as the users table
            # grows — this matters directly for hosting cost, since Postgres
            # would otherwise scan the whole table on every relayed message.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_active_banned "
                "ON users(active, is_banned)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_access_until "
                "ON users(access_until)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_muted_until "
                "ON users(muted_until)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS relay_log (
                    id          SERIAL  PRIMARY KEY,
                    batch_id    TEXT    NOT NULL,
                    sender_uid  BIGINT  NOT NULL,
                    target_uid  BIGINT  NOT NULL,
                    message_id  INTEGER NOT NULL,
                    sent_at     TEXT    NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_relay_target
                ON relay_log(target_uid, message_id)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_settings (
                    id              INTEGER PRIMARY KEY DEFAULT 1,
                    allow_text      INTEGER DEFAULT 1,
                    allow_photo     INTEGER DEFAULT 1,
                    allow_video     INTEGER DEFAULT 1,
                    allow_animation INTEGER DEFAULT 1,
                    allow_sticker   INTEGER DEFAULT 1,
                    allow_voice     INTEGER DEFAULT 1,
                    allow_audio     INTEGER DEFAULT 1,
                    allow_document  INTEGER DEFAULT 1,
                    min_video_bytes BIGINT  DEFAULT 0,
                    max_video_bytes BIGINT  DEFAULT 0
                )
            """)
            cur.execute("""
                INSERT INTO media_settings (id)
                VALUES (1)
                ON CONFLICT DO NOTHING
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_backup (
                    id             SERIAL  PRIMARY KEY,
                    file_unique_id TEXT    NOT NULL UNIQUE,
                    file_id        TEXT    NOT NULL,
                    file_type      TEXT    NOT NULL,
                    sender_uid     BIGINT  NOT NULL,
                    file_size      BIGINT  DEFAULT 0,
                    backed_up_at   TEXT    NOT NULL,
                    backup_msg_id  INTEGER DEFAULT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_backup_unique
                ON media_backup(file_unique_id)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id          SERIAL  PRIMARY KEY,
                    referrer_id BIGINT  NOT NULL,
                    referred_id BIGINT  NOT NULL UNIQUE,
                    created_at  TEXT    NOT NULL,
                    rewarded    INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_referral_referrer
                ON referrals(referrer_id)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_events (
                    key        TEXT PRIMARY KEY,
                    fired_at   TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS welcome_media (
                    id          SERIAL  PRIMARY KEY,
                    file_id     TEXT    NOT NULL,
                    file_type   TEXT    NOT NULL,
                    added_by    BIGINT  NOT NULL,
                    added_at    TEXT    NOT NULL
                )
            """)

            # Tracks which welcome-media items each user has already received
            # so consecutive "welcome back" batches never repeat the same clip.
            # When a user has seen every item the pool resets automatically.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_welcome_media_seen (
                    user_id   BIGINT  NOT NULL,
                    media_id  INTEGER NOT NULL,
                    seen_at   TEXT    NOT NULL,
                    PRIMARY KEY (user_id, media_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_uwms_user
                ON user_welcome_media_seen(user_id)
            """)

            # Links a relay batch (the copies of one piece of media delivered
            # to every recipient) back to the time reward it earned its
            # sender. This is what lets /delete tell an admin exactly how
            # much time to reverse when a media message is removed.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_rewards (
                    batch_id      TEXT    PRIMARY KEY,
                    sender_uid    BIGINT  NOT NULL,
                    media_type    TEXT    NOT NULL,
                    earned_secs   INTEGER NOT NULL,
                    created_at    TEXT    NOT NULL
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_rewards_sender "
                "ON media_rewards(sender_uid)"
            )

            # Case-insensitive username lookups power /prof <username>.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_username_lower "
                "ON users (LOWER(username))"
            )

            # ── Ensure main admin exists ──────────────────────────────────────
            cur.execute("""
                INSERT INTO users
                    (user_id, username, random_id, referral_code, joined_at, active, role)
                VALUES (%s, 'main_admin', %s, %s, %s, 1, 2)
                ON CONFLICT (user_id) DO UPDATE SET role = 2
            """, (MAIN_ADMIN_ID, _gen_id(), _gen_referral_code(), _now()))

            conn.commit()
            log.info("Database initialised successfully (PostgreSQL).")
        finally:
            conn.close()


# ── User helpers ──────────────────────────────────────────────────────────────

def get_user(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
            return cur.fetchone()
        finally:
            conn.close()


def upsert_user(uid, username, referral_code=None):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT random_id, is_banned, referral_code FROM users WHERE user_id=%s",
                (uid,),
            )
            row = cur.fetchone()
            if row:
                if row["is_banned"]:
                    return None, False
                cur.execute(
                    "UPDATE users SET active=1, username=%s WHERE user_id=%s",
                    (username or "", uid),
                )
                conn.commit()
                return row["random_id"], False

            rid   = _gen_id()
            code  = _gen_referral_code()
            grace = (datetime.now(timezone.utc) + timedelta(seconds=GRACE_SECONDS)).isoformat()
            cur.execute(
                "INSERT INTO users "
                "(user_id, username, random_id, referral_code, joined_at, active, access_until) "
                "VALUES (%s,%s,%s,%s,%s,1,%s)",
                (uid, username or "", rid, code, _now(), grace),
            )

            if referral_code:
                cur.execute(
                    "SELECT user_id FROM users WHERE referral_code=%s", (referral_code,)
                )
                referrer = cur.fetchone()
                if referrer and referrer["user_id"] != uid:
                    try:
                        cur.execute(
                            "INSERT INTO referrals (referrer_id, referred_id, created_at) "
                            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (referrer["user_id"], uid, _now()),
                        )
                    except Exception:
                        pass

            conn.commit()
            return rid, True
        finally:
            conn.close()


def get_user_by_username(username: str):
    """Case-insensitive lookup by the user's *Telegram* username. `username`
    should be passed without a leading '@'. Not used by /prof — see
    get_user_by_bot_username() for the bot-assigned identity lookup."""
    if not username:
        return None
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM users WHERE username != '' AND LOWER(username)=LOWER(%s)",
                (username,),
            )
            return cur.fetchone()
        finally:
            conn.close()


def get_user_by_bot_username(name: str):
    """Case-insensitive lookup used by /prof <username>.

    `name` refers to the identity the bot itself gave the user — never their
    Telegram @username. It matches, in order:
      1. Their bot display name (display_name — set via Profile → Set Name).
      2. Their bot ID (random_id — the `🆔 ID:` shown on their profile).
    """
    if not name:
        return None
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM users WHERE display_name != '' AND LOWER(display_name)=LOWER(%s)",
                (name,),
            )
            row = cur.fetchone()
            if row:
                return row
            cur.execute(
                "SELECT * FROM users WHERE LOWER(random_id)=LOWER(%s)",
                (name,),
            )
            return cur.fetchone()
        finally:
            conn.close()


def get_referral_code(uid) -> str:
    row = get_user(uid)
    return row["referral_code"] if row and row["referral_code"] else ""


def get_pending_referral(referred_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM referrals WHERE referred_id=%s AND rewarded=0", (referred_id,)
            )
            return cur.fetchone()
        finally:
            conn.close()


def mark_referral_rewarded(referral_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE referrals SET rewarded=1 WHERE id=%s", (referral_id,))
            conn.commit()
        finally:
            conn.close()


def get_referral_count(uid) -> int:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id=%s AND rewarded=1",
                (uid,),
            )
            return cur.fetchone()["cnt"]
        finally:
            conn.close()


def set_display_name(uid, name) -> bool:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM users WHERE display_name=%s AND user_id!=%s", (name, uid)
            )
            if cur.fetchone():
                return False
            cur.execute("UPDATE users SET display_name=%s WHERE user_id=%s", (name, uid))
            conn.commit()
            return True
        finally:
            conn.close()


def touch_user(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET last_seen=%s WHERE user_id=%s", (_now(), uid))
            conn.commit()
        finally:
            conn.close()


def ban_user(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_banned=1, active=0 WHERE user_id=%s", (uid,))
            conn.commit()
        finally:
            conn.close()


def unban_user(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_banned=0, active=1 WHERE user_id=%s", (uid,))
            conn.commit()
        finally:
            conn.close()


def deactivate_user(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET active=0 WHERE user_id=%s", (uid,))
            conn.commit()
        finally:
            conn.close()


def set_mute(uid, seconds):
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET muted_until=%s WHERE user_id=%s", (until, uid))
            conn.commit()
        finally:
            conn.close()
    return until


def clear_mute(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET muted_until=NULL WHERE user_id=%s", (uid,))
            conn.commit()
        finally:
            conn.close()


def set_role(uid, role):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET role=%s WHERE user_id=%s", (role, uid))
            conn.commit()
        finally:
            conn.close()


def active_users(exclude_id=None):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            if exclude_id:
                cur.execute(
                    "SELECT * FROM users WHERE active=1 AND is_banned=0 AND user_id!=%s",
                    (exclude_id,),
                )
            else:
                cur.execute("SELECT * FROM users WHERE active=1 AND is_banned=0")
            return cur.fetchall()
        finally:
            conn.close()


def all_reachable_users(exclude_id=None):
    """
    Every user we can still physically message — i.e. hasn't blocked/left the
    bot (active=1). Unlike active_users(), this deliberately includes banned,
    muted, and access-expired users: broadcasts are announcements from staff
    and must reach everyone, regardless of their relay/media eligibility.
    """
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            if exclude_id:
                cur.execute(
                    "SELECT * FROM users WHERE active=1 AND user_id!=%s",
                    (exclude_id,),
                )
            else:
                cur.execute("SELECT * FROM users WHERE active=1")
            return cur.fetchall()
        finally:
            conn.close()


def all_users_paged(page=0, per_page=6):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM users")
            total = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT * FROM users ORDER BY joined_at DESC LIMIT %s OFFSET %s",
                (per_page, page * per_page),
            )
            return cur.fetchall(), total
        finally:
            conn.close()


def is_admin(uid) -> bool:
    r = get_user(uid)
    return bool(r and r["role"] >= 1)


def is_main_admin(uid) -> bool:
    return uid == MAIN_ADMIN_ID


def get_all_admin_ids() -> list:
    """Return user_ids for all active admins (role >= 1, not banned)."""
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT user_id FROM users WHERE role >= 1 AND is_banned=0 AND active=1"
            )
            return [r["user_id"] for r in cur.fetchall()]
        finally:
            conn.close()


# ── Time-access helpers ───────────────────────────────────────────────────────

def _row_access_secs(row) -> int:
    au = row.get("access_until") if isinstance(row, dict) else row["access_until"]
    if not au:
        return 0
    try:
        until = datetime.fromisoformat(au)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return max(0, int((until - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return 0


def get_access_seconds(uid) -> int:
    row = get_user(uid)
    return _row_access_secs(row) if row else 0


def add_access_time(uid, seconds: int):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT access_until FROM users WHERE user_id=%s", (uid,))
            row  = cur.fetchone()
            now  = datetime.now(timezone.utc)
            base = now
            if row and row["access_until"]:
                try:
                    cur_t = datetime.fromisoformat(row["access_until"])
                    if cur_t.tzinfo is None:
                        cur_t = cur_t.replace(tzinfo=timezone.utc)
                    base = max(now, cur_t)
                except Exception:
                    pass
            new_until = (base + timedelta(seconds=seconds)).isoformat()
            cur.execute(
                "UPDATE users SET access_until=%s WHERE user_id=%s", (new_until, uid)
            )
            conn.commit()
        finally:
            conn.close()


def add_access_time_returning_was_expired(uid, seconds: int) -> bool:
    """
    Same effect as add_access_time(uid, seconds), but does the "was this user
    out of time?" check and the balance update inside one _db_lock-protected
    transaction, so a "welcome back" greet can be dispatched exactly once per
    genuine expired→active transition. Calling get_access_seconds(uid) and
    add_access_time(uid, ...) as two separate locked calls (as the caller
    used to) leaves a gap between them where a second concurrent message
    from the same user could also read "expired" and double-fire the greet.
    """
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT access_until FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            now = datetime.now(timezone.utc)
            base = now
            was_expired = True
            if row and row["access_until"]:
                try:
                    cur_t = datetime.fromisoformat(row["access_until"])
                    if cur_t.tzinfo is None:
                        cur_t = cur_t.replace(tzinfo=timezone.utc)
                    was_expired = cur_t <= now
                    base = max(now, cur_t)
                except Exception:
                    pass
            new_until = (base + timedelta(seconds=seconds)).isoformat()
            cur.execute(
                "UPDATE users SET access_until=%s WHERE user_id=%s", (new_until, uid)
            )
            conn.commit()
            return was_expired
        finally:
            conn.close()


def subtract_access_time(uid, seconds: int) -> int:
    """
    Subtract `seconds` from a user's access_until timestamp — the inverse of
    add_access_time(). Used to reverse a time reward (e.g. an admin deletes
    the media that earned it) or to let an admin manually dock balance.
    Returns the user's new remaining access seconds (0 if that's now in the
    past, same convention as get_access_seconds()).
    """
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT access_until FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            if not row or not row["access_until"]:
                return 0
            try:
                cur_t = datetime.fromisoformat(row["access_until"])
                if cur_t.tzinfo is None:
                    cur_t = cur_t.replace(tzinfo=timezone.utc)
            except Exception:
                return 0
            new_until = cur_t - timedelta(seconds=seconds)
            cur.execute(
                "UPDATE users SET access_until=%s WHERE user_id=%s",
                (new_until.isoformat(), uid),
            )
            conn.commit()
            return max(0, int((new_until - datetime.now(timezone.utc)).total_seconds()))
        finally:
            conn.close()


def has_access(uid) -> bool:
    return is_admin(uid) or get_access_seconds(uid) > 0


def _row_has_access(row) -> bool:
    return row["role"] >= 1 or _row_access_secs(row) > 0


def user_count():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE active=1 AND is_banned=0")
            return cur.fetchone()["cnt"]
        finally:
            conn.close()


def stats():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            now = _now()
            cur.execute("SELECT COUNT(*) as cnt FROM users")
            total = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE active=1 AND is_banned=0")
            active = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE is_banned=1")
            banned = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT COUNT(*) as cnt FROM users "
                "WHERE muted_until IS NOT NULL AND muted_until > %s",
                (now,),
            )
            muted = cur.fetchone()["cnt"]
            # "Eligible" = would actually receive a relayed/broadcast message
            # right now: active, not banned, not muted, admin or time left.
            cur.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE active=1 AND is_banned=0 "
                "AND (muted_until IS NULL OR muted_until <= %s) "
                "AND (role>=1 OR (access_until IS NOT NULL AND access_until > %s))",
                (now, now),
            )
            eligible_active = cur.fetchone()["cnt"]
            # Non-admin, in-network, not banned/muted, but out of access time.
            cur.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE active=1 AND is_banned=0 "
                "AND role=0 "
                "AND (muted_until IS NULL OR muted_until <= %s) "
                "AND (access_until IS NULL OR access_until <= %s)",
                (now, now),
            )
            expired = cur.fetchone()["cnt"]
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cur.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE last_seen>%s AND is_banned=0",
                (cutoff,),
            )
            recent = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE role>=1")
            admins = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM media_backup")
            backups = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM referrals WHERE rewarded=1")
            refs = cur.fetchone()["cnt"]
            return {
                "total": total, "active": active, "banned": banned,
                "muted": muted, "eligible_active": eligible_active,
                "expired": expired,
                "recent_24h": recent, "admins": admins,
                "backups": backups, "referrals": refs,
            }
        finally:
            conn.close()


def get_eligible_active_count() -> int:
    """Users who would actually receive a relayed/broadcast message right now."""
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            now = _now()
            cur.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE active=1 AND is_banned=0 "
                "AND (muted_until IS NULL OR muted_until <= %s) "
                "AND (role>=1 OR (access_until IS NOT NULL AND access_until > %s))",
                (now, now),
            )
            return cur.fetchone()["cnt"]
        finally:
            conn.close()


def get_expired_users():
    """Non-admin, not banned/muted users whose access time is up (includes inactive users)."""
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            now = _now()
            cur.execute(
                "SELECT * FROM users WHERE is_banned=0 AND role=0 "
                "AND (muted_until IS NULL OR muted_until <= %s) "
                "AND (access_until IS NULL OR access_until <= %s)",
                (now, now),
            )
            return cur.fetchall()
        finally:
            conn.close()


def get_users_needing_expired_reminder(min_interval_secs: int):
    """
    Same population as get_expired_users(), further limited to users who
    either never got a reminder or got one more than min_interval_secs ago —
    this is what keeps the reminder job from re-DMing the same user constantly.
    """
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            now    = _now()
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=min_interval_secs)).isoformat()
            cur.execute(
                "SELECT * FROM users WHERE active=1 AND is_banned=0 AND role=0 "
                "AND (muted_until IS NULL OR muted_until <= %s) "
                "AND (access_until IS NULL OR access_until <= %s) "
                "AND (last_expired_reminder IS NULL OR last_expired_reminder <= %s)",
                (now, now, cutoff),
            )
            return cur.fetchall()
        finally:
            conn.close()


def mark_expired_reminder_sent(uid):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET last_expired_reminder=%s WHERE user_id=%s",
                (_now(), uid),
            )
            conn.commit()
        finally:
            conn.close()


# ── Relay log helpers ─────────────────────────────────────────────────────────

def log_relay(batch_id, sender_uid, target_uid, message_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO relay_log (batch_id,sender_uid,target_uid,message_id,sent_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (batch_id, sender_uid, target_uid, message_id, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def get_batch_by_msg(target_uid, message_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT batch_id, sender_uid FROM relay_log "
                "WHERE target_uid=%s AND message_id=%s",
                (target_uid, message_id),
            )
            return cur.fetchone()
        finally:
            conn.close()


def get_batch_msgs(batch_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT target_uid, message_id FROM relay_log WHERE batch_id=%s",
                (batch_id,),
            )
            return cur.fetchall()
        finally:
            conn.close()


def get_all_relay_msgs():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT target_uid, message_id FROM relay_log")
            return cur.fetchall()
        finally:
            conn.close()


def delete_relay_log_all():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM relay_log")
            conn.commit()
        finally:
            conn.close()


def delete_relay_log_batch(batch_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM relay_log WHERE batch_id=%s", (batch_id,))
            conn.commit()
        finally:
            conn.close()


def cleanup_old_relay_log():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM relay_log WHERE sent_at<%s", (cutoff,))
            # Keep media_rewards in lockstep with relay_log — once a batch's
            # relay copies are gone there's nothing left for /delete to act
            # on, so its reward record is just dead weight.
            cur.execute("DELETE FROM media_rewards WHERE created_at<%s", (cutoff,))
            conn.commit()
        finally:
            conn.close()


# ── Media reward helpers ──────────────────────────────────────────────────────
# Ties a relay batch to the time reward its sender earned for it, so /delete
# can report and reverse the exact amount when that media is removed.

def record_media_reward(batch_id, sender_uid, media_type, earned_secs):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO media_rewards (batch_id, sender_uid, media_type, earned_secs, created_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (batch_id) DO NOTHING",
                (batch_id, sender_uid, media_type, earned_secs, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def get_media_reward(batch_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM media_rewards WHERE batch_id=%s", (batch_id,))
            return cur.fetchone()
        finally:
            conn.close()


def delete_media_reward(batch_id):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM media_rewards WHERE batch_id=%s", (batch_id,))
            conn.commit()
        finally:
            conn.close()


def delete_media_rewards_all():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM media_rewards")
            conn.commit()
        finally:
            conn.close()


# ── Media settings helpers ────────────────────────────────────────────────────

def get_media_settings():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM media_settings WHERE id=1")
            return dict(cur.fetchone())
        finally:
            conn.close()


def set_media_field(field: str, value):
    if field not in MEDIA_FIELDS:
        log.warning("Rejected unknown media field: %s", field)
        return
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(f"UPDATE media_settings SET {field}=%s WHERE id=1", (value,))
            conn.commit()
        finally:
            conn.close()


# ── Media backup helpers ──────────────────────────────────────────────────────

def get_user_media_count(uid) -> int:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) as cnt FROM media_backup WHERE sender_uid=%s", (uid,)
            )
            return cur.fetchone()["cnt"]
        finally:
            conn.close()


def backup_exists(file_unique_id: str) -> bool:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM media_backup WHERE file_unique_id=%s", (file_unique_id,)
            )
            return bool(cur.fetchone())
        finally:
            conn.close()


def save_backup(file_unique_id, file_id, file_type, sender_uid, file_size=0,
                backup_msg_id=None) -> bool:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO media_backup "
                "(file_unique_id, file_id, file_type, sender_uid, file_size, "
                " backed_up_at, backup_msg_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (file_unique_id, file_id, file_type, sender_uid, file_size,
                 _now(), backup_msg_id),
            )
            inserted = cur.rowcount > 0
            conn.commit()
            return inserted
        except Exception as e:
            log.warning("save_backup error: %s", e)
            return False
        finally:
            conn.close()


def update_backup_msg_id(file_unique_id: str, backup_msg_id: int):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE media_backup SET backup_msg_id=%s WHERE file_unique_id=%s",
                (backup_msg_id, file_unique_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_backup(file_unique_id: str):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM media_backup WHERE file_unique_id=%s", (file_unique_id,)
            )
            return cur.fetchone()
        finally:
            conn.close()


# ── Mute / ban state checks ───────────────────────────────────────────────────

def _row_is_muted(row) -> bool:
    """
    Mute check against an already-fetched row — no DB call. Used for bulk
    filtering (relay/broadcast target lists) so checking N recipients doesn't
    cost N extra queries. Does NOT clear an expired mute (that still happens
    lazily via is_muted()) — it's a read-only, fast-path check.
    """
    mu = row.get("muted_until") if isinstance(row, dict) else row["muted_until"]
    if not mu:
        return False
    try:
        until = datetime.fromisoformat(mu)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def is_muted(uid) -> bool:
    row = get_user(uid)
    if not row or not row["muted_until"]:
        return False
    try:
        until = datetime.fromisoformat(row["muted_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < until:
            return True
    except Exception:
        clear_mute(uid)
        return False
    clear_mute(uid)
    return False


def mute_remaining_secs(uid) -> int:
    row = get_user(uid)
    if not row or not row["muted_until"]:
        return 0
    try:
        until = datetime.fromisoformat(row["muted_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(remaining))
    except Exception:
        return 0


# ── Bot events (one-time startup flags) ──────────────────────────────────────

def has_bot_event(key: str) -> bool:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM bot_events WHERE key=%s", (key,))
            return bool(cur.fetchone())
        finally:
            conn.close()


def set_bot_event(key: str):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO bot_events (key, fired_at) VALUES (%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET fired_at=EXCLUDED.fired_at",
                (key, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def get_banned_users_paged(page=0, per_page=6):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE is_banned=1")
            total = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT * FROM users WHERE is_banned=1 "
                "ORDER BY joined_at DESC LIMIT %s OFFSET %s",
                (per_page, page * per_page),
            )
            return cur.fetchall(), total
        finally:
            conn.close()


def get_muted_users():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM users WHERE muted_until IS NOT NULL AND muted_until > %s",
                (_now(),),
            )
            return cur.fetchall()
        finally:
            conn.close()


def get_backup_stats():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM media_backup")
            total = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT file_type, COUNT(*) as cnt, SUM(file_size) as total_size "
                "FROM media_backup GROUP BY file_type ORDER BY cnt DESC"
            )
            by_type = cur.fetchall()
            return total, by_type
        finally:
            conn.close()


# ── Welcome media helpers ─────────────────────────────────────────────────────
# Only the Telegram `file_id` is ever stored — the bot never downloads the
# actual file, so this carries zero RAM/bandwidth cost (same principle the
# relay itself uses for `copy_message`).

def add_welcome_media(file_id: str, file_type: str, added_by: int):
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO welcome_media (file_id, file_type, added_by, added_at) "
                "VALUES (%s,%s,%s,%s)",
                (file_id, file_type, added_by, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def count_welcome_media() -> int:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM welcome_media")
            return cur.fetchone()["cnt"]
        finally:
            conn.close()


def get_random_welcome_media(limit: int):
    """Legacy: pick `limit` random items globally (no per-user dedup)."""
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT file_id, file_type FROM welcome_media "
                "ORDER BY RANDOM() LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
        finally:
            conn.close()


def get_random_welcome_media_for_user(uid: int, limit: int):
    """
    Pick up to `limit` welcome-media items the user has NOT already received.
    If fewer than `limit` unseen items remain, the user's seen-list is reset
    first so the pool starts fresh — they never get stuck with no videos.
    Returns a list of dicts with keys: id, file_id, file_type.
    """
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()

            # How many unseen items are there for this user?
            cur.execute(
                "SELECT COUNT(*) as cnt FROM welcome_media wm "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM user_welcome_media_seen s "
                "  WHERE s.user_id=%s AND s.media_id=wm.id"
                ")",
                (uid,),
            )
            unseen_count = cur.fetchone()["cnt"]

            # If fewer unseen items than we need, wipe the history so we
            # recycle the full pool rather than sending duplicates.
            if unseen_count < limit:
                cur.execute(
                    "DELETE FROM user_welcome_media_seen WHERE user_id=%s", (uid,)
                )
                conn.commit()

            # Pick `limit` items user hasn't seen (after potential reset).
            cur.execute(
                "SELECT id, file_id, file_type FROM welcome_media wm "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM user_welcome_media_seen s "
                "  WHERE s.user_id=%s AND s.media_id=wm.id"
                ") ORDER BY RANDOM() LIMIT %s",
                (uid, limit),
            )
            return cur.fetchall()
        finally:
            conn.close()


def record_welcome_media_seen(uid: int, media_ids: list):
    """Mark a list of welcome_media.id values as seen for this user."""
    if not media_ids:
        return
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            now = _now()
            for mid in media_ids:
                cur.execute(
                    "INSERT INTO user_welcome_media_seen (user_id, media_id, seen_at) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (uid, mid, now),
                )
            conn.commit()
        finally:
            conn.close()


def clear_welcome_media():
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM welcome_media")
            # Also wipe the seen-history — it references IDs that no longer exist.
            cur.execute("DELETE FROM user_welcome_media_seen")
            conn.commit()
        finally:
            conn.close()


def get_all_non_banned_user_ids() -> list:
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE is_banned=0")
            return [r["user_id"] for r in cur.fetchall()]
        finally:
            conn.close()
