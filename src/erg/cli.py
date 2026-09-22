import argparse
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from erg.classify import TEST_DISTANCES
from erg.config import Settings, get_settings
from erg.eligibility import METRICS
from erg.db import session_scope
from erg.models import OAuthToken
from erg.metrics_runner import compute_load, compute_workout_metrics
from erg.pipeline import classify_all, set_override, set_profile
from erg.summary import week_summary
from erg.services import client_for_athlete
from erg.sync import BackfillStats, StrokeFetchStats, backfill, fetch_strokes, renormalize


def _resolve_athlete(athlete_id: int | None) -> int:
    if athlete_id is not None:
        return athlete_id
    with session_scope() as s:
        ids = s.execute(select(OAuthToken.athlete_id)).scalars().all()
    if len(ids) != 1:
        raise SystemExit(f"{len(ids)} authorized athletes found; pass --athlete-id")
    return ids[0]


def _print_backfill(stats: BackfillStats) -> None:
    print(
        f"workouts: fetched={stats.fetched} inserted={stats.inserted} updated={stats.updated} "
        f"unchanged={stats.unchanged} conflicts={len(stats.conflicts)} "
        f"pending_stroke_fetch={len(stats.pending_stroke_fetch)}"
    )
    if stats.conflicts:
        print(f"  conflicting result ids: {stats.conflicts}")


def _print_strokes(stats: StrokeFetchStats) -> None:
    print(
        f"strokes: fetched={stats.fetched} strokes={stats.strokes} missing={len(stats.missing)} "
        f"errors={len(stats.errors)} warnings={len(stats.warnings)}"
    )
    for wid, msg in {**stats.warnings, **stats.errors}.items():
        print(f"  {wid}: {msg}")


def _run(settings: Settings, athlete_id: int, *, workouts: bool, strokes: bool, limit: int | None = None) -> int:
    client = client_for_athlete(settings, athlete_id)
    try:
        if workouts:
            with session_scope() as s:
                _print_backfill(backfill(s, client, settings))
        if strokes:
            with session_scope() as s:
                stats = fetch_strokes(s, client, athlete_id, limit=limit)
            _print_strokes(stats)
            return 1 if stats.errors else 0
    finally:
        client.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="erg")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sy = sub.add_parser("sync", help="pull new/edited workouts, then fetch their strokes")
    sy.add_argument("--athlete-id", type=int)
    bf = sub.add_parser("backfill", help="page all rower results into the database")
    bf.add_argument("--athlete-id", type=int)
    fs = sub.add_parser("fetch-strokes", help="drain the stroke fetch queue")
    fs.add_argument("--athlete-id", type=int)
    fs.add_argument("--limit", type=int, help="max workouts to process this run")
    cl = sub.add_parser("classify", help="classify sessions and flag metric eligibility (no API calls)")
    cl.add_argument("--athlete-id", type=int)
    ov = sub.add_parser("override", help="set the class of one workout by hand")
    ov.add_argument("workout_id", type=int)
    ov.add_argument("workout_class", choices=sorted(TEST_DISTANCES.values()) + ["interval", "steady", "short_piece", "unknown"])
    ov.add_argument("--note")
    pr = sub.add_parser("profile", help="override C2 profile values (max HR, weight) for this athlete")
    pr.add_argument("--athlete-id", type=int)
    pr.add_argument("--max-hr", type=int)
    pr.add_argument("--weight-lb", type=Decimal)
    pr.add_argument("--resting-hr", type=int, help="used for TRIMP; assumed 60 otherwise")
    wk = sub.add_parser("week", help="print the weekly summary (same payload as /summary/week)")
    wk.add_argument("--athlete-id", type=int)
    wk.add_argument("--date", type=date.fromisoformat, help="any day in the week; defaults to the latest")
    me = sub.add_parser("metrics", help="compute derived metrics, daily load and rolling windows (no API calls)")
    me.add_argument("--athlete-id", type=int)
    sub.add_parser("renormalize", help="re-derive normalized columns + interval splits from stored raw payloads")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "sync":
        raise SystemExit(_run(settings, _resolve_athlete(args.athlete_id), workouts=True, strokes=True))
    elif args.cmd == "backfill":
        _run(settings, _resolve_athlete(args.athlete_id), workouts=True, strokes=False)
    elif args.cmd == "fetch-strokes":
        raise SystemExit(
            _run(settings, _resolve_athlete(args.athlete_id), workouts=False, strokes=True, limit=args.limit)
        )
    elif args.cmd == "classify":
        athlete_id = _resolve_athlete(args.athlete_id)
        with session_scope() as s:
            stats = classify_all(s, athlete_id)
        print(f"classified {stats.workouts} workouts ({stats.overridden} manual overrides)")
        for name, n in stats.classes.most_common():
            print(f"  {name:12} {n}")
        print("eligible for:")
        for metric in METRICS:
            print(f"  {metric:12} {stats.eligible[metric]}/{stats.workouts}")
        if stats.low_confidence:
            print(f"low confidence ({len(stats.low_confidence)}) — review and override if wrong:")
            for wid, cls, conf, reason in stats.low_confidence:
                print(f"  {wid} -> {cls} ({conf:.0%}): {reason}")

    elif args.cmd == "override":
        with session_scope() as s:
            set_override(s, args.workout_id, args.workout_class, args.note)
        print(f"workout {args.workout_id} -> {args.workout_class} (rerun `erg classify` to refresh eligibility)")

    elif args.cmd == "profile":
        athlete_id = _resolve_athlete(args.athlete_id)
        with session_scope() as s:
            a = set_profile(s, athlete_id, max_hr=args.max_hr, weight_lb=args.weight_lb, resting_hr=args.resting_hr)
            print(
                f"athlete {a.id}: max HR {a.effective_max_heart_rate} "
                f"(C2 profile: {a.max_heart_rate}), weight {a.effective_weight_g / 1000:.1f} kg "
                f"/ {a.effective_weight_g / 453.59237:.0f} lb (C2 profile: {a.weight_g / 1000:.1f} kg)"
            )
        print("rerun `erg classify` to apply")

    elif args.cmd == "week":
        athlete_id = _resolve_athlete(args.athlete_id)
        with session_scope() as s:
            summary = week_summary(s, athlete_id, args.date)
        t = summary["totals"]
        print(f"week {summary['week_start']} .. {summary['week_end']}")
        print(
            f"  {t['sessions']} sessions on {t['days_trained']} days, "
            f"{t['work_distance_m'] / 1000:.1f} km, {t['work_time_s'] / 3600:.1f} h"
            + (f", {t['kj']:.0f} kJ" if t["kj"] else "")
        )
        for name, b in sorted(summary["by_class"].items(), key=lambda kv: -kv[1]["work_distance_m"]):
            print(f"  {name:12} {b['sessions']} sessions, {b['work_distance_m'] / 1000:.1f} km")
        ef = summary["ef"]
        if ef["mean"]:
            change = f" ({ef['change_pct']:+.1f}% vs prior {ef['baseline_weeks']}w)" if ef["change_pct"] else ""
            print(f"  EF {ef['mean']:.3f} over {ef['sessions']} sessions{change}")
        for d in summary["decoupling"]:
            print(f"  decoupling {d['pct']:+.1f}% ({d['date']})")
        for h in summary["hr_recovery"]:
            print(f"  HR recovery {h['bpm']:.0f} bpm after {h['rest_s']:.0f}s rest ({h['date']})")
        load = summary["load"]
        if load["acwr"]:
            print(f"  load: {load['kj_7d']:.0f} kJ/day 7d vs {load['kj_28d']:.0f} 28d, ACWR {load['acwr']:.2f}")
        q = summary["data_quality"]
        if q["sessions_without_hr"]:
            print(f"  {q['sessions_without_hr']} session(s) without usable HR")

    elif args.cmd == "metrics":
        athlete_id = _resolve_athlete(args.athlete_id)
        with session_scope() as s:
            stats = compute_workout_metrics(s, athlete_id)
            load = compute_load(s, athlete_id)
        print(f"metrics for {stats.workouts} workouts:")
        for name in METRICS:
            print(f"  {name:12} {stats.computed[name]}")
        print(f"daily load: {load.days} days, rolling: {load.rolling_rows} rows")

    elif args.cmd == "renormalize":
        with session_scope() as s:
            print(f"renormalized {renormalize(s, settings)} workouts")
