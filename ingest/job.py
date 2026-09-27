"""Ingest entrypoint and task router.

Usage::

    python -m ingest.job --task all
    python -m ingest.job --task stats --season 2026
    python -m ingest.job --task trending --log-level DEBUG
    python -m ingest.job --task precompute --force

Tasks run in dependency order (``nightly`` before ``stats``, because ``stats``
translates nflverse gsis ids through the ``id_map`` the nightly task writes;
``backtest`` right after ``stats``, because it scores last week's claims against
the lines ``stats`` just wrote; ``precompute`` last, because it caches analysis
built from everything above) and
are **isolated**: each one is wrapped in its own try/except, so a Sleeper outage
cannot stop the nflverse pull. The process exits non-zero if any requested task
failed — that exit code is the Cloud Run Job failure signal Cloud Monitoring
alerts on (tech spec §7).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from api.core.config import Settings, get_settings
from api.core.store import Store, get_store
from ingest.backtest import run_backtest
from ingest.common import configure_logging
from ingest.judge import Judge
from ingest.nflverse_ingest import ingest_nflverse, ingest_schedule, sleeper_gsis_bridge
from ingest.precompute import WARM_TARGETS, warm_response_cache
from ingest.sleeper_players import sync_players
from ingest.trending import refresh_trending

logger = logging.getLogger("ingest.job")

#: Selector meaning "every task, in order".
ALL_TASKS = "all"

TaskFn = Callable[[Store, Settings, "argparse.Namespace"], Awaitable[dict[str, Any]]]


async def task_nightly(
    store: Store, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    """Sleeper player dump -> ``players/``, ``player_index/``, ``id_map/``.

    The nflverse cross-reference is pulled here rather than inside
    ``sync_players`` so that module stays free of nflreadpy: Sleeper omits
    ``gsis_id`` for most fantasy-relevant players, and without it none of them
    join to a game log (DESIGN_NOTES §23).
    """
    return await sync_players(store, gsis_bridge=sleeper_gsis_bridge())


async def task_stats(store: Store, settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    """nflverse pulls -> weekly stats, usage trends, def-vs-pos, schedules.

    ``--stats-only`` (with ``--season``) is the prior-season rebuild: the three
    stat-derived datasets and nothing that describes the current season.
    """
    return await ingest_nflverse(
        store,
        season=getattr(args, "season", None),
        settings=settings,
        stats_only=bool(getattr(args, "stats_only", False)),
    )


async def task_backtest(
    store: Store, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    """Score archived claims against the stat lines ``stats`` just wrote.

    Sits between ``stats`` and ``trending``: it needs the former and nothing
    after it needs this. A week whose lines nflverse has not published yet is
    reported as waiting, not failed — the claims keep until the numbers land.
    """
    return await run_backtest(
        store,
        settings,
        season=getattr(args, "season", None),
        week=getattr(args, "week", None),
    )


async def task_schedule(
    store: Store, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    """Advance schedule/week metadata before the new season's stats exist."""
    return await ingest_schedule(store, season=getattr(args, "season", None), settings=settings)


async def task_trending(
    store: Store, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    """Sleeper trending add/drop -> ``trending/{add,drop}``."""
    return await refresh_trending(store)


def build_judge(settings: Settings) -> Judge | None:
    """The board judge, or ``None`` when ``QUALITY_JUDGE`` is off."""
    if not settings.quality_judge:
        return None
    from ingest.judge import GeminiJudge  # noqa: PLC0415 - credential-bearing, built on demand

    return GeminiJudge(settings)


async def task_precompute(
    store: Store, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    """Generate the league-wide boards and warm ``response_cache``.

    Runs last on purpose: it reads what the other tasks just wrote, so warming
    before them would cache a board built from yesterday's numbers.

    Raises ``PrecomputeError`` when a board is left cold, which :func:`run_tasks`
    turns into a non-zero exit — a board nothing warmed is silent otherwise, and
    the endpoint quietly falls back to the slow request path.
    """
    kwargs: dict[str, Any] = {}
    window = getattr(args, "refresh_window", None)
    if window is not None:
        kwargs["refresh_window"] = window * 3600
    only = getattr(args, "only", None)
    if only:
        wanted = {name.strip() for name in str(only).split(",") if name.strip()}
        unknown = wanted - {target.endpoint_key for target in WARM_TARGETS}
        if unknown:
            raise ValueError(f"--only names no warm target: {sorted(unknown)}")
        kwargs["targets"] = tuple(t for t in WARM_TARGETS if t.endpoint_key in wanted)
    return await warm_response_cache(
        store,
        settings,
        week=getattr(args, "week", None),
        force=getattr(args, "force", False),
        **kwargs,
        judge=build_judge(settings),
    )


#: Task name -> coroutine. Ordered; ``--task all`` runs them top to bottom.
#: Module-level and mutable on purpose: tests swap entries to check isolation.
TASKS: dict[str, TaskFn] = {
    "nightly": task_nightly,
    "stats": task_stats,
    "backtest": task_backtest,
    "schedule": task_schedule,
    "trending": task_trending,
    "precompute": task_precompute,
}

# ``schedule`` is an explicit rollover/recovery task. A normal full ingest gets
# the same data through ``stats`` and should not download and rewrite it twice.
ALL_TASK_ORDER = ("nightly", "stats", "backtest", "trending", "precompute")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="ingest.job", description="Play Clock data ingest")
    parser.add_argument(
        "--task",
        default=ALL_TASKS,
        type=task_list,
        help=(
            "Which ingest task(s) to run, in order, comma-separated (default: all). "
            "`stats,backtest` is how the scheduler scores claims: in the same run, "
            "after the lines are written."
        ),
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help=(
            "stats only: rebuild weekly stats, usage trends and def-vs-pos for --season "
            "and write nothing that describes the current season (no schedule, no "
            "preseason-gap marker, no injuries, no depth charts). The prior-season "
            "rebuild path; see ingest_nflverse."
        ),
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help=(
            "Override the NFL season (default: SEASON for stats/schedule; the ingested "
            "season, then SEASON, for backtest). nflreadpy.get_current_season() is only "
            "a cross-check that warns when it disagrees; it never picks the season."
        ),
    )
    parser.add_argument(
        "--week",
        type=int,
        default=None,
        help=(
            "Week to warm (precompute) or to score (backtest); default: the resolved "
            "current week, or every week with unscored claims."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate cache entries that are already warm (precompute only).",
    )
    parser.add_argument(
        "--only",
        default=None,
        help=(
            "Comma-separated endpoint keys to warm (precompute only), e.g. "
            "--only draft_board. Default: every warm target."
        ),
    )
    parser.add_argument(
        "--refresh-window",
        type=float,
        default=None,
        help=(
            "Hours of remaining life below which a warm entry is regenerated "
            "(precompute only; default: ingest.precompute.REFRESH_WINDOW_SECONDS). "
            "Must exceed this job's scheduling interval or a board can expire "
            "between two runs."
        ),
    )
    parser.add_argument("--log-level", default="INFO", help="Root log level (default: INFO).")
    return parser


def task_list(value: str) -> str:
    """argparse type for ``--task``: ``all`` or a comma-separated list of known tasks."""
    if value == ALL_TASKS:
        return value
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in TASKS]
    if not names or unknown:
        choices = ", ".join([*TASKS, ALL_TASKS])
        raise argparse.ArgumentTypeError(
            f"unknown task(s) {unknown or [value]}; choose from {choices}, comma-separated"
        )
    return ",".join(names)


def selected_tasks(task: str) -> list[str]:
    """Expand the ``--task`` value into the ordered list of task names.

    ``all`` expands to :data:`ALL_TASK_ORDER`; a comma-separated list runs in
    the order given.
    """
    if task == ALL_TASKS:
        return list(ALL_TASK_ORDER)
    return [name.strip() for name in task.split(",") if name.strip()]


#: Tasks that must not run in a chain after one of their prerequisites failed.
#: ``backtest`` scores the lines ``stats`` just wrote; after a failed stats run
#: the collection may be half-written, and a scored claim is never revisited.
#: ``precompute`` chained after ``stats`` exists to rebuild boards from what
#: ``stats`` just wrote; after a
#: failed stats run the live boards are the better answer, so it is skipped and
#: the keep-warm loop carries on. Only a *chained* prerequisite counts:
#: ``--task precompute`` on its own never looks for ``stats``.
DEPENDS_ON: dict[str, tuple[str, ...]] = {"backtest": ("stats",), "precompute": ("stats",)}


async def run_tasks(
    names: Sequence[str],
    store: Store,
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    """Run ``names`` in order, isolating failures.

    Returns:
        ``{task: {"status": "ok"|"failed", "result"|"error": ...}}``.
    """
    results: dict[str, dict[str, Any]] = {}
    for name in names:
        runner = TASKS.get(name)
        if runner is None:
            results[name] = {"status": "failed", "error": f"unknown task {name!r}"}
            logger.error("unknown ingest task", extra={"task": name})
            continue
        broken = [
            dep
            for dep in DEPENDS_ON.get(name, ())
            if results.get(dep, {}).get("status") == "failed"
        ]
        if broken:
            results[name] = {"status": "skipped", "error": f"{', '.join(broken)} failed"}
            logger.error("task skipped: prerequisite failed", extra={"task": name, "after": broken})
            continue
        logger.info("task starting", extra={"task": name})
        try:
            result = await runner(store, settings, args)
        except Exception as exc:
            results[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            logger.exception("task failed", extra={"task": name})
        else:
            results[name] = {"status": "ok", "result": result}
            logger.info("task finished", extra={"task": name, "result": result})
    return results


async def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, run the requested tasks, return the process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    settings = get_settings()
    store = get_store(settings)
    names = selected_tasks(args.task)
    logger.info(
        "ingest run starting",
        extra={"tasks": names, "store_backend": settings.store_backend, "season": args.season},
    )

    try:
        results = await run_tasks(names, store, settings, args)
    finally:
        await store.close()

    failed = sorted(name for name, r in results.items() if r["status"] != "ok")
    logger.info(
        "ingest run complete",
        extra={"tasks": names, "failed": failed, "results": results},
    )
    return 1 if failed else 0


def cli() -> None:
    """Console entrypoint: run :func:`main` and exit with its status."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    cli()
