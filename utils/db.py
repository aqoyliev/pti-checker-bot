from __future__ import annotations

import json

import asyncpg
from data.config import DATABASE_URL

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
        """)


def _pool_check() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


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

async def set_truck_plate(group_id: int, plate: str):
    await _pool_check().execute(
        "UPDATE groups SET truck_plate = $1 WHERE group_id = $2", plate, group_id,
    )


async def set_trailer(group_id: int, unit: str | None, plate: str | None):
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


async def get_recent_ptis(group_id: int, limit: int = 5) -> list[dict]:
    rows = await _pool_check().fetch(
        """SELECT * FROM pti_log WHERE group_id = $1
           ORDER BY submitted_at DESC LIMIT $2""",
        group_id, limit,
    )
    return [dict(r) for r in rows]


async def get_pti_count_this_week(group_id: int, user_id: int) -> int:
    row = await _pool_check().fetchrow(
        """SELECT COUNT(*) FROM pti_log
           WHERE group_id = $1 AND user_id = $2
           AND submitted_at >= date_trunc('week', NOW())""",
        group_id, user_id,
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


