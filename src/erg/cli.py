import argparse
import logging

from sqlalchemy import select

from erg.config import get_settings
from erg.db import session_scope
from erg.models import OAuthToken
from erg.services import client_for_athlete
from erg.sync import backfill


def _resolve_athlete(athlete_id: int | None) -> int:
    if athlete_id is not None:
        return athlete_id
    with session_scope() as s:
        ids = s.execute(select(OAuthToken.athlete_id)).scalars().all()
    if len(ids) != 1:
        raise SystemExit(f"{len(ids)} authorized athletes found; pass --athlete-id")
    return ids[0]


def main() -> None:
    parser = argparse.ArgumentParser(prog="erg")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="page all rower results into the database")
    bf.add_argument("--athlete-id", type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "backfill":
        athlete_id = _resolve_athlete(args.athlete_id)
        client = client_for_athlete(settings, athlete_id)
        try:
            with session_scope() as s:
                stats = backfill(s, client, settings)
        finally:
            client.close()
        print(
            f"fetched={stats.fetched} inserted={stats.inserted} updated={stats.updated} "
            f"unchanged={stats.unchanged} conflicts={len(stats.conflicts)} "
            f"pending_stroke_fetch={len(stats.pending_stroke_fetch)}"
        )
        if stats.conflicts:
            print(f"conflicting result ids: {stats.conflicts}")
