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
                result_text        TEXT
            );
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
        "INSERT INTO groups (group_id) VALUES ($1) ON CONFLICT DO NOTHING", group_id
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
):
    await _pool_check().execute(
        """INSERT INTO pti_log
           (group_id, user_id, replied_message_id, passed, severity,
            unit_number, plate, result_json, result_text)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        group_id, user_id, replied_message_id,
        passed, severity, unit_number, plate, result_json, result_text,
    )


async def get_cached_check(group_id: int, replied_message_id: int) -> str | None:
    row = await _pool_check().fetchrow(
        """SELECT result_text FROM pti_log
           WHERE group_id = $1 AND replied_message_id = $2
           LIMIT 1""",
        group_id, replied_message_id,
    )
    return row["result_text"] if row else None


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
           WHERE group_id = $1 AND user_id = $2 AND passed = TRUE
           AND submitted_at >= date_trunc('week', NOW())""",
        group_id, user_id,
    )
    return row["count"] if row else 0


async def get_last_pti(group_id: int, user_id: int) -> dict | None:
    row = await _pool_check().fetchrow(
        """SELECT * FROM pti_log
           WHERE group_id = $1 AND user_id = $2 AND passed = TRUE
           ORDER BY submitted_at DESC LIMIT 1""",
        group_id, user_id,
    )
    return dict(row) if row else None


async def get_all_registered_groups() -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT * FROM groups WHERE setup_complete = TRUE"
    )
    return [dict(r) for r in rows]
