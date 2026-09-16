from __future__ import annotations

import asyncpg
from data.config import DATABASE_URL, FLEET_TZ
from utils.driver_names import tidy_name

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
                -- Nothing writes this. It used to hold the unit number read
                -- off the video, which is gone (2026-09-13); filling it from
                -- the group's registered unit instead would switch the
                -- previous-inspection history back on for the model, which the
                -- fleet chose to leave off. Two readers tolerate the NULL: the
                -- report prefers the group's unit anyway, and the history
                -- filter in handlers/groups/pti.py drops every row, which is
                -- what keeps the history off. Kept so that turning it on is
                -- one line rather than a migration.
                unit_number        TEXT,
                result_json        TEXT,
                result_text        TEXT,
                media_signature    TEXT
            );

            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS media_signature TEXT;

            -- Tables this code no longer reads or writes, left in place on the
            -- live databases rather than dropped (a deploy should not delete
            -- fleet history on its own): active_units (the pasted unit list,
            -- removed 2026-08-31), pending_proposals + proposal_votes (the
            -- 3-vote vehicle-change flow), driver_verify (a one-off
            -- verification queue), pti_retry_queue (the failed-inspection
            -- retry, removed 2026-09-13). None is created on a fresh database.
            --
            -- Same for three groups columns -- truck_plate, trailer_unit,
            -- trailer_plate -- and pti_log.plate. A PTI no longer reads the
            -- vehicle it was filmed on (2026-09-13), so nothing fills them;
            -- they are not added to a fresh database and nothing reads them on
            -- an old one.

            ALTER TABLE groups ADD COLUMN IF NOT EXISTS setup_nag_count INT DEFAULT 0;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_setup_nag_at TIMESTAMP;


            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS driver_name TEXT;

            ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

            ALTER TABLE pti_log ADD COLUMN IF NOT EXISTS content_signature TEXT;

            -- Consecutive "unreachable" sends. A single failure is not proof a
            -- group is gone: the local Bot API server loses its chat state on
            -- restart and answers "chat not found" for chats it has not seen
            -- since. Only a sustained streak deactivates. See mark_unreachable.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS unreachable_strikes INT DEFAULT 0;

            -- "The bot is in this group but is not allowed to post in it."
            -- Stored rather than recomputed so the admins are told once, when
            -- it starts, and once more when it is fixed -- an hourly loop over
            -- 150 groups would otherwise repeat the same alert all week. See
            -- utils/group_health.py.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS post_blocked BOOLEAN DEFAULT FALSE;

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
            -- overdue_reminded_at: when the first "3 days, no PTI" notice went out (#9).
            -- last_escalation_at: when the last daily escalation notice went out (#9).
            -- last_reminder_at: the last reminder of ANY kind, which is what the
            --   one-per-24-hours rule is measured against. Kept separate from the
            --   two above because those drive the state machine (which message
            --   is due) while this one only answers "has this unit been told
            --   today" -- including for reminders sent by the compliance loop.
            -- last_weekly_reminder_on (DATE) is a leftover column from the removed
            -- twice-weekly nudge (#8, dropped 2026-08-20) -- unused, not recreated.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS overdue_reminded_at TIMESTAMP;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_escalation_at TIMESTAMP;
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP;

            -- People confirmed *not* to be drivers, fleet-wide: dispatchers,
            -- safety staff, owners. They sit in many driver groups, so once an
            -- admin has passed over someone during onboarding there is no point
            -- offering them again in the next group. Hiding is reversible —
            -- picking someone as a driver clears their row, and /nondrivers
            -- clear empties the table.
            CREATE TABLE IF NOT EXISTS non_drivers (
                user_id   BIGINT PRIMARY KEY,
                name      TEXT,
                marked_at TIMESTAMP DEFAULT NOW()
            );

            -- Chat title cache, refreshed opportunistically (group_title
            -- middleware + web panel fetches) so listing groups doesn't need a
            -- get_chat round-trip per group.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS title TEXT;

            -- How many HUMAN messages landed in each group each day
            -- (middlewares/group_activity.py). Daily buckets rather than one row
            -- per message: the question is only ever "how many in the last few
            -- days", so a counter per group per day answers it with one small
            -- row instead of thousands, and pruning is a single DELETE.
            -- Bot chatter is excluded at the middleware, so a nagged-but-dead
            -- group can't look alive. See utils/group_activity.py.
            CREATE TABLE IF NOT EXISTS group_message_days (
                group_id  BIGINT NOT NULL,
                day       DATE   NOT NULL,
                msg_count INT    NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, day)
            );
            CREATE INDEX IF NOT EXISTS idx_gmd_day ON group_message_days (day);

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
        """)
        # Driver names typed before tidy_name guarded the writes -- the same
        # one-time, idempotent cleanup as the unit numbers above. Done in Python
        # rather than SQL so there is exactly one definition of the shape:
        # Postgres' initcap() does not agree with str.title() on every name.
        rows = await conn.fetch("SELECT id, name FROM group_drivers")
        fixes = untidy_driver_names(rows)
        if fixes:
            await conn.executemany(
                "UPDATE group_drivers SET name = $2 WHERE id = $1", fixes)


def untidy_driver_names(rows) -> list[tuple[int, str]]:
    """(id, tidy name) for every stored name not already in its one shape."""
    out = []
    for r in rows:
        tidy = tidy_name(r["name"])
        # A blank name stays for a person to fill in: there is nothing better
        # to put there, and "" is no fix for "  ".
        if tidy and tidy != r["name"]:
            out.append((r["id"], tidy))
    return out


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


# ---------- title sweep ----------


async def apply_title_sweep(
    renames: list[tuple[int, str]], group_ids: list[int],
) -> tuple[int, int]:
    """Re-file and retire groups from what their titles now say.

    Returns ``(refiled_count, deactivated_count)``.

    One transaction, so the summary DM'd to the admin describes what actually
    landed rather than what was attempted. ``unit_number`` only, never
    ``setup_complete`` -- a group being re-filed is already configured, and
    flipping that flag would hide an un-onboarded group from the setup nag.
    """
    pool = _pool_check()
    refiled = deactivated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for gid, new_unit in renames:
                rows = await conn.fetch(
                    """UPDATE groups SET unit_number = $1
                        WHERE group_id = $2 AND COALESCE(is_active, TRUE) = TRUE
                    RETURNING group_id""",
                    normalize_unit(new_unit), gid,
                )
                refiled += len(rows)
            if group_ids:
                rows = await conn.fetch(
                    """UPDATE groups SET is_active = FALSE
                        WHERE group_id = ANY($1::bigint[])
                          AND COALESCE(is_active, TRUE) = TRUE
                    RETURNING group_id""",
                    group_ids,
                )
                deactivated = len(rows)
    return refiled, deactivated


# ---------- non-drivers (fleet-wide picker exclusions) ----------

async def get_non_driver_ids() -> set[int]:
    rows = await _pool_check().fetch("SELECT user_id FROM non_drivers")
    return {r["user_id"] for r in rows}


async def mark_non_drivers(people: list[tuple[int, str]]) -> int:
    """Remember these people as not-drivers. Returns how many were newly added."""
    if not people:
        return 0
    before = await get_non_driver_ids()
    await _pool_check().executemany(
        """INSERT INTO non_drivers (user_id, name) VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name""",
        [(uid, name) for uid, name in people],
    )
    return len({uid for uid, _ in people} - before)


async def unmark_non_drivers(user_ids: list[int]) -> None:
    """Undo the exclusion — called whenever someone is picked as a driver.

    Being chosen as a driver is the strongest possible signal that a previous
    "not a driver" was wrong, so it silently wins over the stored row.
    """
    if not user_ids:
        return
    await _pool_check().execute(
        "DELETE FROM non_drivers WHERE user_id = ANY($1::bigint[])", list(user_ids)
    )


async def clear_non_drivers() -> int:
    """Empty the table. Returns how many rows were dropped."""
    count = len(await get_non_driver_ids())
    await _pool_check().execute("DELETE FROM non_drivers")
    return count


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


async def migrate_group_id(old_id: int, new_id: int) -> bool:
    """Move a group's whole history onto the chat id Telegram gave it.

    A basic group upgraded to a supergroup keeps none of its id: Telegram issues
    a fresh one and answers every send to the old one with ``MigrateToChat``.
    Until the rows follow, the unit is broken in both directions -- reminders go
    to a dead id, and a PTI posted in the new chat finds no ``groups`` row, so
    ``_group_ready`` refuses it *silently*. Three groups sat like that on
    2026-08-25.

    One transaction, because a half-moved group is worse than either end of it:
    the unit would exist twice, and the compliance pass reads both.

    Returns False and changes nothing when the new id already has a row --
    merging two configured groups is a decision, not a repair.

    The ``groups`` row is copied through ``to_jsonb`` rather than a column list
    so that columns added by a later ALTER come across on their own; forgetting
    one here would lose a group's unit or its notification settings.
    """
    if old_id == new_id:
        return False
    pool = _pool_check()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM groups WHERE group_id = $1", new_id)
            if exists:
                return False
            moved = await conn.fetchval(
                """INSERT INTO groups
                   SELECT (jsonb_populate_record(
                               NULL::groups,
                               to_jsonb(g) || jsonb_build_object('group_id', $2::bigint))).*
                     FROM groups g
                    WHERE g.group_id = $1
                   RETURNING group_id""",
                old_id, new_id,
            )
            if moved is None:
                return False
            # Children first: both FKs point at groups(group_id), and the old
            # parent row can only go once nothing references it any more.
            for table in ("group_drivers", "pti_log"):
                await conn.execute(
                    f"UPDATE {table} SET group_id = $2 WHERE group_id = $1",
                    old_id, new_id,
                )
            # Activity counts are per (group, day) and the middleware has been
            # counting under the new id since the moment of the upgrade, so the
            # two sides are summed rather than one overwriting the other.
            await conn.execute(
                """INSERT INTO group_message_days (group_id, day, msg_count)
                   SELECT $2, day, msg_count FROM group_message_days WHERE group_id = $1
                   ON CONFLICT (group_id, day) DO UPDATE
                       SET msg_count = group_message_days.msg_count + EXCLUDED.msg_count""",
                old_id, new_id,
            )
            await conn.execute(
                "DELETE FROM group_message_days WHERE group_id = $1", old_id)
            await conn.execute("DELETE FROM groups WHERE group_id = $1", old_id)
    return True


async def mark_group_inactive(group_id: int):
    await _pool_check().execute(
        "UPDATE groups SET is_active = FALSE WHERE group_id = $1", group_id,
    )


# How many consecutive unreachable sends before a group is really deactivated.
UNREACHABLE_LIMIT = 3


async def mark_unreachable(group_id: int) -> int:
    """Record one unreachable send and return the new strike count.

    Deliberately does NOT deactivate on its own. One failure is unreliable
    evidence -- the local Bot API server forgets every chat when it restarts
    and then reports "chat not found" for groups the bot is still in, which is
    how a fleet of healthy groups got deactivated after a redeploy.
    """
    return await _pool_check().fetchval(
        """UPDATE groups
              SET unreachable_strikes = COALESCE(unreachable_strikes, 0) + 1
            WHERE group_id = $1
        RETURNING unreachable_strikes""",
        group_id,
    )


async def clear_unreachable(group_id: int):
    """Reset the strike counter after any successful send."""
    await _pool_check().execute(
        "UPDATE groups SET unreachable_strikes = 0"
        " WHERE group_id = $1 AND COALESCE(unreachable_strikes, 0) <> 0",
        group_id,
    )


async def set_post_blocked(group_id: int, blocked: bool) -> bool:
    """Record whether the bot may post in this group. True if that just changed.

    The return value is the whole point: it is what stops the alert repeating.
    The UPDATE only matches a row whose stored answer disagrees, so the caller
    tells the admins exactly on the transitions -- started, and fixed.
    """
    row = await _pool_check().fetchval(
        """UPDATE groups SET post_blocked = $1
            WHERE group_id = $2 AND COALESCE(post_blocked, FALSE) IS DISTINCT FROM $1
        RETURNING group_id""",
        blocked, group_id,
    )
    return row is not None


async def get_post_blocked_groups() -> list[dict]:
    rows = await _pool_check().fetch(
        "SELECT group_id, unit_number, title FROM groups"
        " WHERE COALESCE(post_blocked, FALSE) AND COALESCE(is_active, TRUE)"
        " ORDER BY unit_number",
    )
    return [dict(r) for r in rows]


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
             -- One prompt per group, full stop. The prompt is a DM to an admin
             -- with a member picker in it; re-sending turns their chat into a
             -- stack of identical prompts, all but the newest already dead
             -- (the pending state is per-process). A group that never got one
             -- is reachable with /onboard <group_id>.
             AND COALESCE(g.setup_nag_count, 0) < 1"""
    )
    return [dict(r) for r in rows]


async def get_unconfigured_groups() -> list[dict]:
    """Active groups with no unit and no drivers, whether they were nagged or not.

    The nag query above deliberately stops after one prompt per group; this one
    is what scripts/setup_groups.py sweeps, for the case that prompt was sent
    before the drivers were even in the chat.
    """
    rows = await _pool_check().fetch(
        """SELECT * FROM groups
            WHERE setup_complete = FALSE
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY group_id"""
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


# ---------- the group's unit number ----------

def normalize_unit(unit: str | None) -> str:
    """Bare unit number: drop the <angle brackets> and stray whitespace that
    came in with imported/typed units (e.g. "<1304 >" → "1304"). Every unit
    write below runs through this so the junk can't reappear."""
    if not unit:
        return ""
    return unit.replace("<", "").replace(">", "").strip()


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
    """Returns False if driver already registered.

    Like every write to `group_drivers.name` below, the name is stored in its
    one shape (`tidy_name`), whoever typed it.
    """
    try:
        await _pool_check().execute(
            "INSERT INTO group_drivers (group_id, user_id, name) VALUES ($1, $2, $3)",
            group_id, user_id, tidy_name(name),
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


async def get_driver_memberships(user_id: int) -> list[dict]:
    """Every group this user is a registered driver of, newest group last.

    Used by /whois: a phone number resolves to a user_id, and the first thing
    worth knowing about that user_id is whether the fleet already has them.
    """
    rows = await _pool_check().fetch(
        """SELECT gd.group_id, gd.name, g.unit_number, g.title,
                  COALESCE(g.is_active, TRUE) AS is_active
             FROM group_drivers gd
             LEFT JOIN groups g ON g.group_id = gd.group_id
            WHERE gd.user_id = $1
            ORDER BY g.unit_number NULLS LAST, gd.group_id""",
        user_id,
    )
    return [dict(r) for r in rows]


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
                    group_id, int(d["user_id"]), tidy_name(d["name"]),
                )
            await conn.execute(
                "UPDATE groups SET setup_complete = TRUE WHERE group_id = $1", group_id
            )


async def swap_driver(group_id: int, old_user_id: int, new_user_id: int,
                      name: str) -> bool:
    """Replace one driver with another in a single transaction.

    "The wrong person is registered here" is one correction, not two, and doing
    it as remove-then-add leaves a window where the group has one driver -- long
    enough for the hourly compliance pass to read it and report a unit that is
    short a driver. Returns False if `old_user_id` was not registered here, so
    a stale panel can't add someone by pretending to replace a driver who has
    already gone.
    """
    pool = _pool_check()
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await conn.execute(
                "DELETE FROM group_drivers WHERE group_id = $1 AND user_id = $2",
                group_id, old_user_id,
            )
            if removed == "DELETE 0":
                return False
            await conn.execute(
                """INSERT INTO group_drivers (group_id, user_id, name)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (group_id, user_id) DO UPDATE SET name = $3""",
                group_id, new_user_id, tidy_name(name),
            )
    return True


async def set_driver_names(updates: list[tuple[int, int, str]]) -> int:
    """Rename registered drivers: [(group_id, user_id, name), ...] -> rows changed.

    One transaction, because this is a fleet-wide backfill (/fixnames) and a
    half-applied one leaves the admin's report describing a state that never
    existed. A driver whose name already matches is not counted -- the report
    says how many names actually moved.
    """
    if not updates:
        return 0
    pool = _pool_check()
    changed = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for group_id, user_id, name in updates:
                # Tidied before the comparison too, so renaming "Saintil Fedj"
                # to "SAINTIL, FEDJ" is the no-op it is, not a counted change.
                name = tidy_name(name)
                rows = await conn.fetch(
                    """UPDATE group_drivers SET name = $3
                        WHERE group_id = $1 AND user_id = $2
                          AND name IS DISTINCT FROM $3
                    RETURNING id""",
                    group_id, user_id, name,
                )
                changed += len(rows)
    return changed


# ---------- pti log ----------

async def log_pti(
    group_id: int,
    user_id: int,
    passed: bool,
    severity: str,
    result_json: str,
    result_text: str,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
    driver_name: str | None = None,
    content_signature: str | None = None,
) -> int:
    """Record one inspection. ``unit_number`` is deliberately left unwritten --
    see the column's comment in ``init_db``."""
    row = await _pool_check().fetchrow(
        """INSERT INTO pti_log
           (group_id, user_id, replied_message_id, passed, severity,
            result_json, result_text, media_signature,
            driver_name, content_signature)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING id""",
        group_id, user_id, replied_message_id,
        passed, severity, result_json, result_text,
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


async def get_registered_driver_ids() -> set[int]:
    """Everyone this fleet has registered as a driver, in any group.

    The automatic setup records the rest of a group's roster as non-drivers,
    and this is what it checks first: a driver who sits in two chats -- a team
    driver who changed trucks, someone added to a group before their own -- is
    left alone rather than hidden fleet-wide.
    """
    rows = await _pool_check().fetch("SELECT DISTINCT user_id FROM group_drivers")
    return {r["user_id"] for r in rows}


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


async def deactivate_group_ids(group_ids: list[int]) -> int:
    """Bulk-deactivate arbitrary groups. Returns how many were actually flipped.

    For a manual, admin-confirmed bulk action (e.g. /titlecheck), kept apart
    from the daily sweep's own transaction in ``apply_title_sweep``.
    """
    if not group_ids:
        return 0
    rows = await _pool_check().fetch(
        """UPDATE groups SET is_active = FALSE
            WHERE group_id = ANY($1::bigint[])
              AND COALESCE(is_active, TRUE) = TRUE
        RETURNING group_id""",
        group_ids,
    )
    return len(rows)


async def bump_group_message_count(group_id: int) -> None:
    """Count one human message against today's bucket for this group.

    Written from the message middleware on every human message, so it must stay
    a single cheap upsert. The day is the DB's own UTC date, matching the naive
    UTC timestamps the rest of the app compares against.
    """
    await _pool_check().execute(
        """INSERT INTO group_message_days (group_id, day, msg_count)
                VALUES ($1, (NOW() AT TIME ZONE 'UTC')::date, 1)
           ON CONFLICT (group_id, day)
           DO UPDATE SET msg_count = group_message_days.msg_count + 1""",
        group_id,
    )


async def get_group_message_counts(days: int) -> dict[int, int]:
    """Human messages per group over the last ``days`` days, today included.

    Groups with no traffic are simply absent — the caller treats a missing
    group as zero, so a chat that has never spoken still reports as quiet.
    """
    rows = await _pool_check().fetch(
        """SELECT group_id, SUM(msg_count)::int AS total
             FROM group_message_days
            WHERE day > (NOW() AT TIME ZONE 'UTC')::date - $1::int
         GROUP BY group_id""",
        days,
    )
    return {r["group_id"]: r["total"] for r in rows}


async def group_activity_since():
    """The oldest day we still hold message counts for, or None if we hold none.

    This is how the quiet report knows whether it has a full window to judge on.
    Counting only ever runs forward, so for the first few days after the feature
    ships every group looks silent — and that report sits next to the decision
    that retires trucks. Comparing against this date turns "no data yet" into an
    honest "still collecting" instead of a fleet-wide false alarm.
    """
    row = await _pool_check().fetchrow("SELECT MIN(day) AS since FROM group_message_days")
    return row["since"] if row else None


async def prune_group_message_days(keep_days: int = 14) -> int:
    """Drop buckets older than the reporting window needs."""
    rows = await _pool_check().fetch(
        """DELETE FROM group_message_days
            WHERE day < (NOW() AT TIME ZONE 'UTC')::date - $1::int
        RETURNING group_id""",
        keep_days,
    )
    return len(rows)




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


async def mark_reminder_sent(group_id: int, at) -> None:
    """Stamp the group's shared 24-hour reminder slot.

    Every sender calls this after a reminder actually goes out — the weekly
    nudge, the overdue notice, the escalation and the compliance loop alike —
    because the one-per-24-hours rule counts reminders the *unit* received, not
    reminders of one particular kind.
    """
    await _pool_check().execute(
        "UPDATE groups SET last_reminder_at = $1 WHERE group_id = $2", at, group_id,
    )


async def reset_group_reminders(group_id: int) -> None:
    """Clear overdue/escalation state — called when a fresh PTI lands so the
    daily nags stop immediately instead of waiting for the next pass.

    ``last_reminder_at`` is deliberately left alone: it records that the group
    was messaged, which a new PTI doesn't undo, and clearing it would let a
    second reminder go out within the 24 hours."""
    await _pool_check().execute(
        """UPDATE groups
           SET overdue_reminded_at = NULL, last_escalation_at = NULL
           WHERE group_id = $1""",
        group_id,
    )
