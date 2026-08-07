"""Flip is_active back to TRUE for groups the bot is still a member of but that
were wrongly deactivated (see utils/reminders.py:71 -- a single ChatNotFound
from the local Bot API server is treated as terminal).

DRY RUN BY DEFAULT. When BOT_TOKEN is available it verifies membership against
the *cloud* Telegram API first, so a group the bot really did leave can't be
reactivated. BOT_TOKEN and the public DB URL live in different Railway
services, so if only the DB is reachable you must pass an explicit,
already-verified --group-ids list.

    railway run --service Postgres py -3.11 scripts/reactivate_groups.py \
        --group-ids -5260263663 -1003910045250 ... --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
import httpx


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--group-ids", nargs="*", type=int,
                    help="restrict to these ids (default: every inactive group)")
    args = ap.parse_args()

    token = os.environ.get("BOT_TOKEN")
    if not token and not args.group_ids:
        raise SystemExit(
            "No BOT_TOKEN, so membership can't be verified here. Pass an explicit "
            "--group-ids list that you have already verified."
        )

    dsn = os.environ.get("DATABASE_PUBLIC_URL") or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(
        "SELECT group_id, unit_number, title FROM groups"
        " WHERE COALESCE(is_active, TRUE) = FALSE ORDER BY unit_number")
    if args.group_ids:
        rows = [r for r in rows if r["group_id"] in set(args.group_ids)]

    to_fix = []
    if token:
        api = f"https://api.telegram.org/bot{token}"
        async with httpx.AsyncClient(timeout=30) as cl:
            me = (await cl.get(f"{api}/getMe")).json()["result"]["id"]
            for r in rows:
                gid = r["group_id"]
                m = (await cl.get(f"{api}/getChatMember",
                                  params={"chat_id": gid, "user_id": me})).json()
                status = m["result"]["status"] if m.get("ok") else "gone"
                ok = status in ("member", "administrator", "creator")
                if ok:
                    to_fix.append(gid)
                print(f"  {str(r['unit_number']):>8}  {gid:>15}  bot={status:<14} "
                      f"{'REACTIVATE' if ok else 'skip'}")
                await asyncio.sleep(0.15)
    else:
        print("!! No BOT_TOKEN -- trusting the --group-ids list without re-verifying.\n")
        for r in rows:
            to_fix.append(r["group_id"])
            print(f"  {str(r['unit_number']):>8}  {r['group_id']:>15}  REACTIVATE (unverified)")

    print(f"\n{len(to_fix)} of {len(rows)} inactive groups will be reactivated.")
    if not args.apply:
        print("DRY RUN -- nothing changed. Re-run with --apply.")
        await conn.close()
        return

    n = await conn.execute(
        "UPDATE groups SET is_active = TRUE WHERE group_id = ANY($1::bigint[])", to_fix)
    print(f"updated: {n}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
