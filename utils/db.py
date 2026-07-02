from __future__ import annotations

import json

import asyncpg
from data.config import DATABASE_URL, FLEET_TZ

_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id       BIGINT PRIMARY KEY,
                unit_number    TEXT,
                setup_complete BOOLEAN DEFAULT FALSE,
                created_at     TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS group_drivers (
                id       BIGSERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL REFERENCES groups(group_id),
                user_id  BIGINT NOT NULL,
                name     TEXT NOT NULL,
                UNIQUE(group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS pti_log (
                id                 BIGSERIAL PRIMARY KEY,
                group_id           BIGINT NOT NULL REFERENCES groups(group_id),
                user_id            BIGINT NOT NULL,
                replied_message_id BIGINT,
                submitted_at       TIMESTAMP DEFAULT NOW(),
                passed             BOOLEAN,
                severity           TEXT,
                unit_number        TEXT,
                plate              TEXT,
                result_json        TEXT,
                result_text        TEXT,
                media_signature    TEXT
            );

            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS media_signature TEXT;

            CREATE TABLE IF NOT EXISTS pending_proposals (
                id            BIGSERIAL PRIMARY KEY,
                group_id      BIGINT NOT NULL,
                proposal_type TEXT NOT NULL,
                payload       JSONB NOT NULL,
                proposer_id   BIGINT,
                message_id    BIGINT,
                status        TEXT NOT NULL DEFAULT 'open',
                created_at    TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS proposal_votes (
                proposal_id BIGINT NOT NULL REFERENCES pending_proposals(id) ON DELETE CASCADE,
                user_id     BIGINT NOT NULL,
                vote        TEXT NOT NULL,
                voted_at    TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (proposal_id, user_id)
            );

            ALTER TABLE pending_proposals ADD COLUMN IF NOT EXISTS reminder_count INT DEFAULT 0;

            ALTER TABLE groups ADD COLUMN IF NOT EXISTS setup_nag_count INT DEFAULT 0;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_setup_nag_at TIMESTAMP;

            ALTER TABLE groups ADD COLUMN IF NOT EXISTS truck_plate TEXT;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS trailer_unit TEXT;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS trailer_plate TEXT;

            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS driver_name TEXT;

            ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS content_signature TEXT;

            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            );

            -- Bot admins (separate from group admins). is_super_admin can manage
            -- other admins; every admin can open the panel and disable a group's
            -- notifications.
            CREATE TABLE IF NOT EXISTS admins (
                user_id        BIGINT PRIMARY KEY,
                is_super_admin BOOLEAN DEFAULT FALSE,
                added_at       TIMESTAMP DEFAULT NOW()
            );

            -- Per-group kill-switch for all bot reminders (#10). When TRUE the
            -- reminder engine skips the group entirely.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS notifications_disabled BOOLEAN DEFAULT FALSE;

            -- Reminder-engine bookkeeping (utils/reminders.py).
            -- last_weekly_reminder_on: date of the last twice-weekly nudge (#8).
            -- overdue_reminded_at: when the first "3 days, no PTI" notice went out (#9).
            -- last_escalation_at: when the last every-3-hours escalation notice went out (#9).
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_weekly_reminder_on DATE;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS overdue_reminded_at TIMESTAMP;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_escalation_at TIMESTAMP;

            -- Driver-verification queue (handlers/admin/verify.py). Seeded from a
            -- userbot member snapshot: one row per group, walked in `ord` order.
            -- `members` is the group's member list [{user_id, name, username}];
            -- `status` is pending | done | skipped.
            CREATE TABLE IF NOT EXISTS driver_verify (
                group_id    BIGINT PRIMARY KEY,
                ord         BIGSERIAL,
                unit        TEXT,
                title       TEXT,
                members     JSONB NOT NULL DEFAULT '[]'::jsonb,
                status      TEXT NOT NULL DEFAULT 'pending',
                verified_at TIMESTAMP
            );

            -- Chat title cache, refreshed opportunistically (group_title
            -- middleware + web panel fetches) so listing groups doesn't need a
            -- get_chat round-trip per group.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS title TEXT;

            -- pti_log is queried three ways on every hot path: recent-per-group
            -- (results/reminders), per-driver weekly counts (compliance), and
            -- signature lookups (recycled-video dedup on every submission).
            -- Without these, each is a full-table scan that degrades as the log
            -- grows (~300 rows/week fleet-wide).
            CREATE INDEX IF NOT EXISTS idx_pti_log_group_time
                ON pti_log (group_id, submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pti_log_group_user_time
                ON pti_log (group_id, user_id, submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pti_log_media_sig
                ON pti_log (group_id, media_signature) WHERE media_signature IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_pti_log_content_sig
                ON pti_log (group_id, content_signature) WHERE content_signature IS NOT NULL;

            -- Unit numbers imported with <angle brackets>/stray spaces (e.g.
            -- '<1304 >'). One-time cleanup, idempotent: normalize_unit() keeps
            -- new writes clean, this fixes rows that predate it.
            UPDATE groups
               SET unit_number = NULLIF(BTRIM(TRANSLATE(unit_number, '<>', '')), '')
             WHERE unit_number IS DISTINCT FROM
                   NULLIF(BTRIM(TRANSLATE(unit_number, '<>', '')), '');
            UPDATE groups
               SET trailer_unit = NULLIF(BTRIM(TRANSLATE(trailer_unit, '<>', '')), '')
             WHERE trailer_unit IS DISTINCT FROM
                   NULLIF(BTRIM(TRANSLATE(trailer_unit, '<>', '')), '');
            UPDATE driver_verify
               SET unit = NULLIF(BTRIM(TRANSLATE(unit, '<>', '')), '')
             WHERE unit IS DISTINCT FROM
                   NULLIF(BTRIM(TRANSLATE(unit, '<>', '')), '');
        """)


def _pool_check() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


# ---------- app settings (generic key/value) ----------

async def get_setting(key: str) -> str | None:
    row = await _pool_check().fetchrow(
        "SELECT value FROM app_settings WHERE key = $1", key
    )
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    await _pool_check().execute(
        """INSERT INTO app_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
        key, value,
    )


# ---------- admins ----------

async def seed_super_admins(user_ids: list[int]) -> None:
    """Ensure each id is an admin with is_super_admin=TRUE (run at startup from
    config.ADMINS). Never demotes — promoting an existing row to super is safe."""
    for uid in user_ids:
        await _pool_check().execute(
            """INSERT INTO admins (user_id, is_super_admin) VALUES ($1, TRUE)
               ON CONFLICT (user_id) DO UPDATE SET is_super_admin = TRUE""",
            int(uid),
        )


async def get_admin(user_id: int) -> dict | None:
    row = await _pool_check().fetchrow(
        "SELECT * FROM admins WHERE user_id = $1", user_id
    )
    return dict(row) if row else None


async def get_admins() -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT * FROM admins ORDER BY is_super_admin DESC, added_at ASC"
    )
    return [dict(r) for r in rows]


async def add_admin(user_id: int, is_super_admin: bool = False) -> None:
    await _pool_check().execute(
        """INSERT INTO admins (user_id, is_super_admin) VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET is_super_admin = $2""",
        int(user_id), is_super_admin,
    )


async def remove_admin(user_id: int) -> bool:
    res = await _pool_check().execute(
        "DELETE FROM admins WHERE user_id = $1 AND is_super_admin = FALSE", int(user_id)
    )
    return res.endswith("1")


# ---------- groups ----------

async def get_group(group_id: int) -> dict | None:
    row = await _pool_check().fetchrow(
        "SELECT * FROM groups WHERE group_id = $1", group_id
    )
    return dict(row) if row else None


async def upsert_group(group_id: int):
    await _pool_check().execute(
        """INSERT INTO groups (group_id, last_setup_nag_at)
           VALUES ($1, NOW())
           ON CONFLICT (group_id) DO UPDATE SET is_active = TRUE""",
        group_id,
    )


async def mark_group_inactive(group_id: int):
    await _pool_check().execute(
        "UPDATE groups SET is_active = FALSE WHERE group_id = $1", group_id,
    )


async def set_group_title(group_id: int, title: str) -> None:
    """Cache the chat title. Written opportunistically (message middleware, web
    panel fetches); the IS DISTINCT FROM guard makes unchanged-title calls free."""
    await _pool_check().execute(
        "UPDATE groups SET title = $1 WHERE group_id = $2 AND title IS DISTINCT FROM $1",
        title, group_id,
    )


async def get_groups_needing_setup_nag() -> list[dict]:
    rows = await _pool_check().fetch(
        """SELECT g.* FROM groups g
           WHERE g.setup_complete = FALSE
             AND COALESCE(g.is_active, TRUE) = TRUE
             AND COALESCE(g.setup_nag_count, 0) < 3
             AND NOT EXISTS (
               SELECT 1 FROM pending_proposals p
               WHERE p.group_id = g.group_id AND p.status = 'open'
             )"""
    )
    return [dict(r) for r in rows]


async def bump_setup_nag(group_id: int):
    await _pool_check().execute(
        """UPDATE groups
           SET setup_nag_count = COALESCE(setup_nag_count, 0) + 1,
               last_setup_nag_at = NOW()
           WHERE group_id = $1""",
        group_id,
    )


async def reset_setup_nag(group_id: int):
    await _pool_check().execute(
        "UPDATE groups SET setup_nag_count = 0, last_setup_nag_at = NULL WHERE group_id = $1",
        group_id,
    )


# ---------- vehicle info ----------

def normalize_unit(unit: str | None) -> str:
    """Bare unit number: drop the <angle brackets> and stray whitespace that
    came in with imported/typed units (e.g. "<1304 >" → "1304"). Every unit
    write below runs through this so the junk can't reappear."""
    if not unit:
        return ""
    return unit.replace("<", "").replace(">", "").strip()


async def set_truck_plate(group_id: int, plate: str):
    await _pool_check().execute(
        "UPDATE groups SET truck_plate = $1 WHERE group_id = $2", plate, group_id,
    )


async def set_trailer(group_id: int, unit: str | None, plate: str | None):
    if unit is not None:
        unit = normalize_unit(unit)
    if unit is not None and plate is not None:
        await _pool_check().execute(
            "UPDATE groups SET trailer_unit = $1, trailer_plate = $2 WHERE group_id = $3",
            unit, plate, group_id,
        )
    elif unit is not None:
        await _pool_check().execute(
            "UPDATE groups SET trailer_unit = $1 WHERE group_id = $2", unit, group_id,
        )
    elif plate is not None:
        await _pool_check().execute(
            "UPDATE groups SET trailer_plate = $1 WHERE group_id = $2", plate, group_id,
        )


async def set_truck_unit(group_id: int, unit: str, plate: str | None):
    """Replace truck unit (and optionally plate) without flipping setup_complete."""
    unit = normalize_unit(unit)
    if plate is not None:
        await _pool_check().execute(
            "UPDATE groups SET unit_number = $1, truck_plate = $2 WHERE group_id = $3",
            unit, plate, group_id,
        )
    else:
        await _pool_check().execute(
            "UPDATE groups SET unit_number = $1 WHERE group_id = $2", unit, group_id,
        )


async def find_open_vehicle_change(group_id: int, kind: str) -> dict | None:
    row = await _pool_check().fetchrow(
        """SELECT * FROM pending_proposals
           WHERE group_id = $1
             AND proposal_type = 'vehicle_change'
             AND status = 'open'
             AND payload->>'kind' = $2
           ORDER BY created_at DESC LIMIT 1""",
        group_id, kind,
    )
    if not row:
        return None
    d = dict(row)
    payload = d["payload"]
    if isinstance(payload, str):
        d["payload"] = json.loads(payload)
    return d


async def mark_pti_failed(pti_log_id: int):
    await _pool_check().execute(
        "UPDATE pti_log SET passed = FALSE WHERE id = $1", pti_log_id,
    )


async def set_group_unit(group_id: int, unit_number: str):
    unit_number = normalize_unit(unit_number)
    await _pool_check().execute(
        "UPDATE groups SET unit_number = $1, setup_complete = TRUE WHERE group_id = $2",
        unit_number, group_id,
    )


# ---------- drivers ----------

async def get_drivers(group_id: int) -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT * FROM group_drivers WHERE group_id = $1", group_id
    )
    return [dict(r) for r in rows]


async def add_driver(group_id: int, user_id: int, name: str) -> bool:
    """Returns False if driver already registered."""
    try:
        await _pool_check().execute(
            "INSERT INTO group_drivers (group_id, user_id, name) VALUES ($1, $2, $3)",
            group_id, user_id, name,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def remove_driver(group_id: int, user_id: int) -> bool:
    """Returns False if driver was not registered."""
    result = await _pool_check().execute(
        "DELETE FROM group_drivers WHERE group_id = $1 AND user_id = $2",
        group_id, user_id,
    )
    return result != "DELETE 0"


async def is_registered_driver(group_id: int, user_id: int) -> bool:
    row = await _pool_check().fetchrow(
        "SELECT 1 FROM group_drivers WHERE group_id = $1 AND user_id = $2",
        group_id, user_id,
    )
    return row is not None


async def replace_drivers(group_id: int, drivers: list[dict]) -> None:
    """Overwrite a group's drivers with `drivers` ([{user_id, name}, ...]) in one
    transaction and flip setup_complete. Used by the verification flow to commit
    the admin's confirmed driver + co-driver."""
    pool = _pool_check()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM group_drivers WHERE group_id = $1", group_id)
            for d in drivers:
                await conn.execute(
                    """INSERT INTO group_drivers (group_id, user_id, name)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (group_id, user_id) DO UPDATE SET name = $3""",
                    group_id, int(d["user_id"]), d["name"],
                )
            await conn.execute(
                "UPDATE groups SET setup_complete = TRUE WHERE group_id = $1", group_id
            )


# ---------- driver verification queue ----------

async def get_verify_progress() -> tuple[int, int]:
    """(handled, total) where handled = done + skipped."""
    row = await _pool_check().fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status <> 'pending') AS handled, COUNT(*) AS total "
        "FROM driver_verify"
    )
    return (row["handled"], row["total"]) if row else (0, 0)


async def get_next_verify() -> dict | None:
    """Next pending group to verify, in `ord` order. `members` comes back as a
    Python list (asyncpg returns JSONB as a str, so decode it here)."""
    row = await _pool_check().fetchrow(
        "SELECT group_id, unit, title, members FROM driver_verify "
        "WHERE status = 'pending' ORDER BY ord ASC LIMIT 1"
    )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("members"), str):
        d["members"] = json.loads(d["members"])
    return d


async def set_verify_status(group_id: int, status: str) -> None:
    await _pool_check().execute(
        "UPDATE driver_verify SET status = $2, verified_at = NOW() WHERE group_id = $1",
        group_id, status,
    )


# ---------- pti log ----------

async def log_pti(
    group_id: int,
    user_id: int,
    passed: bool,
    severity: str,
    unit_number: str | None,
    plate: str | None,
    result_json: str,
    result_text: str,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
    driver_name: str | None = None,
    content_signature: str | None = None,
) -> int:
    row = await _pool_check().fetchrow(
        """INSERT INTO pti_log
           (group_id, user_id, replied_message_id, passed, severity,
            unit_number, plate, result_json, result_text, media_signature,
            driver_name, content_signature)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
           RETURNING id""",
        group_id, user_id, replied_message_id,
        passed, severity, unit_number, plate, result_json, result_text,
        media_signature, driver_name, content_signature,
    )
    return row["id"]


async def get_cached_check(
    group_id: int,
    media_signature: str | None,
    content_signature: str | None = None,
) -> dict | None:
    """Return the most recent cached PTI matching either signature.

    ``media_signature`` is based on Telegram ``file_unique_id`` (catches forwards).
    ``content_signature`` is based on ``(file_size, duration)`` of video items
    (catches byte-identical re-uploads where ``file_unique_id`` changes).

    Returns a dict with ``user_id``, ``driver_name``, and ``result_text``.
    """
    if not media_signature and not content_signature:
        return None
    row = await _pool_check().fetchrow(
        """SELECT user_id, driver_name, result_text FROM pti_log
           WHERE group_id = $1
             AND (
               ($2::text IS NOT NULL AND media_signature = $2)
               OR ($3::text IS NOT NULL AND content_signature = $3)
             )
           ORDER BY submitted_at DESC LIMIT 1""",
        group_id, media_signature, content_signature,
    )
    return dict(row) if row else None


async def get_pti_log(pti_log_id: int) -> dict | None:
    row = await _pool_check().fetchrow("SELECT * FROM pti_log WHERE id = $1", pti_log_id)
    return dict(row) if row else None


async def get_all_drivers_by_group() -> dict[int, list[dict]]:
    """All registered drivers keyed by group_id — one query for the web panel's
    groups list instead of a per-group fan-out."""
    rows = await _pool_check().fetch(
        "SELECT group_id, user_id, name FROM group_drivers ORDER BY id"
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["group_id"], []).append(
            {"user_id": r["user_id"], "name": r["name"]}
        )
    return out


async def get_last_pti_per_group() -> dict[int, dict]:
    """Most recent PTI (passed + submitted_at) per group, in one query."""
    rows = await _pool_check().fetch(
        """SELECT DISTINCT ON (group_id) group_id, passed, submitted_at
           FROM pti_log ORDER BY group_id, submitted_at DESC"""
    )
    return {r["group_id"]: dict(r) for r in rows}


async def get_recent_ptis(group_id: int, limit: int = 5) -> list[dict]:
    rows = await _pool_check().fetch(
        """SELECT * FROM pti_log WHERE group_id = $1
           ORDER BY submitted_at DESC LIMIT $2""",
        group_id, limit,
    )
    return [dict(r) for r in rows]


async def get_pti_count_this_week(group_id: int, user_id: int) -> int:
    # The quota week starts at midnight Monday in FLEET_TZ, not UTC. Reading the
    # boundary expression inside-out: NOW() in fleet-local time → truncate to
    # Monday 00:00 local → back to an absolute instant → rendered as naive UTC
    # to match how submitted_at is stored. DST is handled by the tz database.
    row = await _pool_check().fetchrow(
        """SELECT COUNT(*) FROM pti_log
           WHERE group_id = $1 AND user_id = $2
           AND submitted_at >=
               (date_trunc('week', NOW() AT TIME ZONE $3) AT TIME ZONE $3)
                   AT TIME ZONE 'UTC'""",
        group_id, user_id, FLEET_TZ,
    )
    return row["count"] if row else 0


async def get_last_pti(group_id: int, user_id: int) -> dict | None:
    row = await _pool_check().fetchrow(
        """SELECT * FROM pti_log
           WHERE group_id = $1 AND user_id = $2
           ORDER BY submitted_at DESC LIMIT 1""",
        group_id, user_id,
    )
    return dict(row) if row else None


async def get_weekly_pti_stats() -> dict[tuple[int, int], dict]:
    """Per (group_id, user_id): PTIs submitted this week + most recent submission,
    in one query. Fleet-wide compliance views use this instead of two queries per
    driver (hundreds of round-trips across ~150 groups)."""
    # Same FLEET_TZ week boundary as get_pti_count_this_week (see comment there).
    rows = await _pool_check().fetch(
        """SELECT group_id, user_id,
                  COUNT(*) FILTER (WHERE submitted_at >=
                      (date_trunc('week', NOW() AT TIME ZONE $1) AT TIME ZONE $1)
                          AT TIME ZONE 'UTC')
                      AS week_count,
                  MAX(submitted_at) AS last_at
           FROM pti_log
           GROUP BY group_id, user_id""",
        FLEET_TZ,
    )
    return {
        (r["group_id"], r["user_id"]): {"week_count": r["week_count"], "last_at": r["last_at"]}
        for r in rows
    }


async def get_all_registered_groups() -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT * FROM groups WHERE setup_complete = TRUE AND COALESCE(is_active, TRUE) = TRUE"
    )
    return [dict(r) for r in rows]


async def get_all_groups() -> list[dict]:
    """Every group the bot has ever been added to, regardless of setup/active state.

    Ordered so the admin panel shows live, configured groups first.
    """
    rows = await _pool_check().fetch(
        """SELECT * FROM groups
           ORDER BY COALESCE(is_active, TRUE) DESC, setup_complete DESC, created_at ASC"""
    )
    return [dict(r) for r in rows]


async def get_active_group_ids() -> list[int]:
    """Group ids the bot can currently message — broadcast targets."""
    rows = await _pool_check().fetch(
        "SELECT group_id FROM groups WHERE COALESCE(is_active, TRUE) = TRUE ORDER BY group_id"
    )
    return [r["group_id"] for r in rows]


async def set_group_active(group_id: int, active: bool):
    await _pool_check().execute(
        "UPDATE groups SET is_active = $1 WHERE group_id = $2", active, group_id,
    )


# ---------- reminders / notifications (#8/#9/#10) ----------

async def set_group_notifications(group_id: int, disabled: bool) -> None:
    """Toggle the per-group reminder kill-switch (#10)."""
    await _pool_check().execute(
        "UPDATE groups SET notifications_disabled = $1 WHERE group_id = $2",
        disabled, group_id,
    )


async def get_groups_for_reminders() -> list[dict]:
    """Active, fully-configured groups whose notifications are NOT disabled —
    the candidate set for the reminder engine."""
    rows = await _pool_check().fetch(
        """SELECT * FROM groups
           WHERE setup_complete = TRUE
             AND COALESCE(is_active, TRUE) = TRUE
             AND COALESCE(notifications_disabled, FALSE) = FALSE"""
    )
    return [dict(r) for r in rows]


async def get_last_pti_for_group(group_id: int) -> dict | None:
    """Most recent PTI in the group, across all its drivers (for overdue checks)."""
    row = await _pool_check().fetchrow(
        """SELECT * FROM pti_log WHERE group_id = $1
           ORDER BY submitted_at DESC LIMIT 1""",
        group_id,
    )
    return dict(row) if row else None


async def mark_weekly_reminder(group_id: int, on_date) -> None:
    await _pool_check().execute(
        "UPDATE groups SET last_weekly_reminder_on = $1 WHERE group_id = $2",
        on_date, group_id,
    )


async def mark_overdue_reminded(group_id: int, at) -> None:
    await _pool_check().execute(
        "UPDATE groups SET overdue_reminded_at = $1 WHERE group_id = $2",
        at, group_id,
    )


async def mark_escalation_reminded(group_id: int, at) -> None:
    await _pool_check().execute(
        "UPDATE groups SET last_escalation_at = $1 WHERE group_id = $2",
        at, group_id,
    )


async def reset_group_reminders(group_id: int) -> None:
    """Clear overdue/escalation state — called when a fresh PTI lands so the
    every-3-hours nags stop immediately instead of waiting for the next pass."""
    await _pool_check().execute(
        """UPDATE groups
           SET overdue_reminded_at = NULL, last_escalation_at = NULL
           WHERE group_id = $1""",
        group_id,
    )


# ---------- proposals ----------

async def create_proposal(
    group_id: int,
    proposal_type: str,
    payload: dict,
    proposer_id: int | None,
) -> int:
    row = await _pool_check().fetchrow(
        """INSERT INTO pending_proposals (group_id, proposal_type, payload, proposer_id)
           VALUES ($1, $2, $3::jsonb, $4)
           RETURNING id""",
        group_id, proposal_type, json.dumps(payload), proposer_id,
    )
    return row["id"]


async def attach_proposal_message(proposal_id: int, message_id: int):
    await _pool_check().execute(
        "UPDATE pending_proposals SET message_id = $1 WHERE id = $2",
        message_id, proposal_id,
    )


async def get_proposal(proposal_id: int) -> dict | None:
    row = await _pool_check().fetchrow(
        "SELECT * FROM pending_proposals WHERE id = $1", proposal_id
    )
    if not row:
        return None
    d = dict(row)
    payload = d["payload"]
    if isinstance(payload, str):
        d["payload"] = json.loads(payload)
    return d


async def set_proposal_status(proposal_id: int, status: str):
    await _pool_check().execute(
        "UPDATE pending_proposals SET status = $1 WHERE id = $2",
        status, proposal_id,
    )


async def cast_vote(proposal_id: int, user_id: int, vote: str):
    """Insert or update the user's vote on this proposal."""
    await _pool_check().execute(
        """INSERT INTO proposal_votes (proposal_id, user_id, vote)
           VALUES ($1, $2, $3)
           ON CONFLICT (proposal_id, user_id)
           DO UPDATE SET vote = EXCLUDED.vote, voted_at = NOW()""",
        proposal_id, user_id, vote,
    )


async def count_votes(proposal_id: int) -> tuple[int, int]:
    """Return (confirms, rejects) for this proposal."""
    rows = await _pool_check().fetch(
        "SELECT vote, COUNT(*) AS c FROM proposal_votes WHERE proposal_id = $1 GROUP BY vote",
        proposal_id,
    )
    confirms = 0
    rejects = 0
    for r in rows:
        if r["vote"] == "confirm":
            confirms = r["c"]
        elif r["vote"] == "reject":
            rejects = r["c"]
    return confirms, rejects


async def bump_proposal_reminder(proposal_id: int) -> int:
    row = await _pool_check().fetchrow(
        """UPDATE pending_proposals
           SET reminder_count = reminder_count + 1
           WHERE id = $1
           RETURNING reminder_count""",
        proposal_id,
    )
    return row["reminder_count"] if row else 0


async def get_open_proposals() -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT * FROM pending_proposals WHERE status = 'open' ORDER BY id"
    )
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        payload = d["payload"]
        if isinstance(payload, str):
            d["payload"] = json.loads(payload)
        out.append(d)
    return out


