from __future__ import annotations

import json
from datetime import datetime

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

            -- Consecutive "unreachable" sends. A single failure is not proof a
            -- group is gone: the local Bot API server loses its chat state on
            -- restart and answers "chat not found" for chats it has not seen
            -- since. Only a sustained streak deactivates. See mark_unreachable.
            ALTER TABLE groups ADD COLUMN IF NOT EXISTS unreachable_strikes INT DEFAULT 0;

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

            -- The fleet's currently-active unit numbers, replaced wholesale by
            -- an admin (see handlers/admin/units.py). Onboarding only offers a
            -- unit it can find in here, so a group named after a retired truck
            -- reads as "not found" instead of quietly configuring a dead unit.
            -- Empty table = no list supplied yet, which disables the check
            -- rather than rejecting everything.
            CREATE TABLE IF NOT EXISTS active_units (
                unit     TEXT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT NOW()
            );

            -- The trailer the office has assigned to that truck, from the wider
            -- roster paste (utils/fleet_roster.py). Nullable on purpose: the
            -- weekly /units list is unit numbers only, so a unit added by the
            -- sweep simply has no trailer recorded. Never read as a verdict --
            -- what a truck is actually pulling comes from the video.
            ALTER TABLE active_units ADD COLUMN IF NOT EXISTS trailer TEXT;

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

            -- Inspections that failed for a reason that was NOT the driver's
            -- fault -- Gemini out of credit, a retired model, an overload, a
            -- network blip. Before this table those submissions vanished: the
            -- handler returned without writing a pti_log row, so nothing in the
            -- bot knew a PTI had ever been attempted, and on 2026-08-20 two days
            -- of them had to be reconstructed by hand from Telegram history.
            -- Rows are re-analysed by utils/pti_retry.py once the service works
            -- again. file_id is what makes that possible: it stays valid long
            -- after the update is gone, so the bot can re-download the media
            -- itself instead of needing the chat history it cannot read.
            CREATE TABLE IF NOT EXISTS pti_retry_queue (
                id               BIGSERIAL PRIMARY KEY,
                group_id         BIGINT NOT NULL,
                user_id          BIGINT NOT NULL,
                driver_name      TEXT,
                reply_message_id BIGINT NOT NULL,
                items_json       TEXT   NOT NULL,
                media_signature  TEXT,
                content_signature TEXT,
                attempts         INT    NOT NULL DEFAULT 0,
                last_error       TEXT,
                created_at       TIMESTAMP DEFAULT NOW(),
                next_attempt_at  TIMESTAMP DEFAULT NOW(),
                resolved_at      TIMESTAMP,
                outcome          TEXT
            );
            -- The worker only ever asks for unresolved rows that are due.
            CREATE INDEX IF NOT EXISTS idx_pti_retry_due
                ON pti_retry_queue (next_attempt_at)
                WHERE resolved_at IS NULL;
            -- One pending retry per submission, so a driver spamming /check on a
            -- broken bot doesn't queue the same video five times over.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pti_retry_pending_once
                ON pti_retry_queue (group_id, reply_message_id)
                WHERE resolved_at IS NULL;

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


# ---------- active units ----------

ACTIVE_UNITS_UPDATED_KEY = "active_units_updated_at"


async def get_active_units() -> set[str]:
    """Every active unit number. Empty set means "no list supplied yet"."""
    rows = await _pool_check().fetch("SELECT unit FROM active_units")
    return {r["unit"] for r in rows}


async def set_fleet_roster(rows: list[dict]) -> tuple[int, int]:
    """Upsert ``[{"unit", "trailer"}, ...]`` into the active list.

    Returns ``(added, updated)``.

    **Adds and updates only — it never deletes.** Retiring a unit deactivates
    the groups filed under it, and that decision belongs to ``/units``, where an
    admin is shown the casualties and confirms them (see
    ``handlers/admin/units.py``). A roster paste is a statement about the trucks
    it lists, not about the ones it leaves out, so loading one can't retire
    anything by omission.

    One transaction, so a half-loaded roster can't leave the list disagreeing
    with what the caller was told it wrote.
    """
    clean = [(normalize_unit(r["unit"]), (r.get("trailer") or "").strip() or None)
             for r in rows if normalize_unit(r.get("unit"))]
    if not clean:
        return 0, 0

    pool = _pool_check()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = {r["unit"] for r in await conn.fetch("SELECT unit FROM active_units")}
            await conn.executemany(
                """INSERT INTO active_units (unit, trailer) VALUES ($1, $2)
                   ON CONFLICT (unit) DO UPDATE SET trailer = EXCLUDED.trailer""",
                clean,
            )
            await conn.execute(
                """INSERT INTO app_settings (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
                ACTIVE_UNITS_UPDATED_KEY, datetime.utcnow().isoformat(),
            )
    units = {u for u, _ in clean}
    added = len(units - existing)
    return added, len(units) - added


async def get_fleet_roster() -> dict[str, str | None]:
    """``{unit: trailer or None}`` for every active unit."""
    rows = await _pool_check().fetch("SELECT unit, trailer FROM active_units")
    return {r["unit"]: r["trailer"] for r in rows}


async def apply_units_sweep(
    units: list[str], group_ids: list[int],
    renames: list[tuple[int, str]] | None = None,
) -> tuple[set[str], set[str], int, int]:
    """Store the weekly list, re-file renamed groups, retire what fell off it.

    Returns ``(added, removed, deactivated_count, refiled_count)``.

    ``renames`` re-files a group under the unit its title now names, and is
    applied *before* the deactivation so a truck whose number changed this week
    isn't retired under the number it no longer has. It only ever sets
    ``unit_number`` -- never ``setup_complete`` -- because a group being re-filed
    is already configured, and flipping that flag here would hide an
    un-onboarded group from the setup nag forever.

    One transaction on purpose. These were previously two independent writes, so
    a failure between them left the list replaced but no group retired, while the
    admin's screen still read "nothing has been saved yet" — the stored state and
    the reported state disagreeing in the one flow that can deactivate trucks
    fleet-wide. Either both land or neither does.

    The updated-at stamp is written inside the transaction too: rolling back the
    list while leaving the stamp fresh would suppress the next weekly ask.
    """
    pool = _pool_check()
    wanted = {u.strip() for u in units if u.strip()}
    renames = renames or []
    deactivated = refiled = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = {r["unit"] for r in await conn.fetch("SELECT unit FROM active_units")}
            added, removed = wanted - existing, existing - wanted
            if removed:
                await conn.execute("DELETE FROM active_units WHERE unit = ANY($1::text[])",
                                   list(removed))
            if added:
                await conn.executemany("INSERT INTO active_units (unit) VALUES ($1)",
                                       [(u,) for u in added])
            # Re-file before retiring, so a group that moved to a listed unit is
            # judged on its new number rather than the one it just left.
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
            await conn.execute(
                """INSERT INTO app_settings (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
                ACTIVE_UNITS_UPDATED_KEY, datetime.utcnow().isoformat(),
            )
    return added, removed, deactivated, refiled


async def apply_title_sweep(
    renames: list[tuple[int, str]], group_ids: list[int],
) -> tuple[int, int]:
    """Re-file and retire groups from what their titles now say.

    Returns ``(refiled_count, deactivated_count)``.

    The same two writes ``apply_units_sweep`` makes, minus the weekly list --
    this runs on a timer rather than off a pasted message, so there is no list to
    store and no ``updated_at`` stamp to touch. Keeping the stamp out matters:
    bumping it here would suppress the weekly ask for a fresh list.

    One transaction, for the same reason as the weekly sweep: the summary DM'd to
    the admin has to describe what actually landed. ``unit_number`` only, never
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


async def active_units_updated_at() -> datetime | None:
    """When the list was last replaced, or None if it never has been."""
    raw = await get_setting(ACTIVE_UNITS_UPDATED_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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
            for table in ("group_drivers", "pti_log", "pending_proposals",
                          "pti_retry_queue"):
                await conn.execute(
                    f"UPDATE {table} SET group_id = $2 WHERE group_id = $1",
                    old_id, new_id,
                )
            # driver_verify is keyed on group_id alone, so it moves only into a
            # free slot; an onboarding prompt already open on the new id wins.
            await conn.execute(
                """UPDATE driver_verify SET group_id = $2
                    WHERE group_id = $1
                      AND NOT EXISTS (SELECT 1 FROM driver_verify WHERE group_id = $2)""",
                old_id, new_id,
            )
            await conn.execute("DELETE FROM driver_verify WHERE group_id = $1", old_id)
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
             AND COALESCE(g.setup_nag_count, 0) < 1
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
                    group_id, int(d["user_id"]), d["name"],
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
                group_id, new_user_id, name,
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
                rows = await conn.fetch(
                    """UPDATE group_drivers SET name = $3
                        WHERE group_id = $1 AND user_id = $2
                          AND name IS DISTINCT FROM $3
                    RETURNING id""",
                    group_id, user_id, name,
                )
                changed += len(rows)
    return changed


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


# ---------- failed-inspection retry queue ----------
#
# Only infrastructure failures belong here (see utils/pti_retry.py). A video that
# is genuinely unusable -- too long, no media, a response the model refused --
# will fail identically forever, and re-posting that verdict days later is noise.

async def enqueue_pti_retry(
    group_id: int,
    user_id: int,
    reply_message_id: int,
    items_json: str,
    driver_name: str | None = None,
    media_signature: str | None = None,
    content_signature: str | None = None,
    error: str | None = None,
) -> int | None:
    """Remember a submission whose inspection failed on us. Returns the row id.

    Returns ``None`` when this submission is already queued: the partial unique
    index means a driver re-sending /check against a broken bot updates the
    existing row instead of stacking duplicates that would each post a result.
    """
    row = await _pool_check().fetchrow(
        """INSERT INTO pti_retry_queue
           (group_id, user_id, driver_name, reply_message_id, items_json,
            media_signature, content_signature, last_error)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (group_id, reply_message_id) WHERE resolved_at IS NULL
           DO UPDATE SET last_error = EXCLUDED.last_error
           RETURNING id""",
        group_id, user_id, driver_name, reply_message_id, items_json,
        media_signature, content_signature, (error or "")[:500],
    )
    return row["id"] if row else None


async def due_pti_retries(limit: int = 20) -> list[dict]:
    """Unresolved retries whose backoff has elapsed, oldest submission first."""
    rows = await _pool_check().fetch(
        """SELECT * FROM pti_retry_queue
            WHERE resolved_at IS NULL AND next_attempt_at <= NOW()
         ORDER BY created_at
            LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def count_pending_pti_retries() -> int:
    return await _pool_check().fetchval(
        "SELECT COUNT(*) FROM pti_retry_queue WHERE resolved_at IS NULL")


async def resolve_pti_retry(retry_id: int, outcome: str) -> None:
    """Close a retry for good: 'done', 'gave_up', 'superseded' or 'stale'."""
    await _pool_check().execute(
        """UPDATE pti_retry_queue
              SET resolved_at = NOW(), outcome = $2, attempts = attempts + 1
            WHERE id = $1""",
        retry_id, outcome,
    )


async def defer_pti_retry(retry_id: int, delay_seconds: int, error: str) -> int:
    """Push a retry back by `delay_seconds`. Returns the new attempt count."""
    return await _pool_check().fetchval(
        """UPDATE pti_retry_queue
              SET attempts = attempts + 1,
                  last_error = $3,
                  next_attempt_at = NOW() + ($2 || ' seconds')::interval
            WHERE id = $1
        RETURNING attempts""",
        retry_id, str(int(delay_seconds)), (error or "")[:500],
    )


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

    For a manual, admin-confirmed bulk action (e.g. /titlecheck) that isn't tied
    to the weekly active-units list -- it never touches ``active_units`` or its
    ``updated_at`` stamp.
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


