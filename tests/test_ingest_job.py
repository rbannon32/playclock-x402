"""Tests for the ingest entrypoint and task router."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from api.core.store import Store
from ingest import job


@pytest.fixture
def tasks(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace the task table with recorders; returns the call log."""
    ran: dict[str, list[str]] = {"ok": []}

    def recorder(name: str):
        async def run(store: Store, settings: Any, args: argparse.Namespace) -> dict[str, Any]:
            ran["ok"].append(name)
            return {"task": name, "season": args.season}

        return run

    async def explode(store: Store, settings: Any, args: argparse.Namespace) -> dict[str, Any]:
        ran["ok"].append("stats-attempted")
        raise RuntimeError("nflverse exploded")

    monkeypatch.setitem(job.TASKS, "nightly", recorder("nightly"))
    monkeypatch.setitem(job.TASKS, "stats", explode)
    monkeypatch.setitem(job.TASKS, "trending", recorder("trending"))
    return ran


def test_parser_defaults_to_all_tasks() -> None:
    args = job.build_parser().parse_args([])

    assert args.task == job.ALL_TASKS
    assert args.season is None
    assert args.log_level == "INFO"
    # None, not a literal: the default lives with the task, next to its rationale.
    assert args.refresh_window is None


def test_parser_rejects_an_unknown_task() -> None:
    with pytest.raises(SystemExit):
        job.build_parser().parse_args(["--task", "nonsense"])


def test_selected_tasks_expands_all_in_dependency_order() -> None:
    assert job.selected_tasks(job.ALL_TASKS) == [
        "nightly",
        "stats",
        "backtest",
        "trending",
        "precompute",
    ]
    assert job.selected_tasks("stats") == ["stats"]
    assert job.selected_tasks("schedule") == ["schedule"]


def test_precompute_runs_after_every_data_task() -> None:
    """It caches analysis built from the other tasks' output.

    Warming before them would publish a board built from yesterday's numbers and
    then serve it, unchallenged, for the whole TTL.
    """
    order = job.selected_tasks(job.ALL_TASKS)
    assert order[-1] == "precompute"


async def test_one_failing_task_does_not_stop_the_others(
    store: Store, tasks: dict[str, list[str]]
) -> None:
    exit_code = await job.main(["--task", "all", "--season", "2026"])

    # 'stats' blew up in the middle; 'trending' still ran, and the job failed.
    assert tasks["ok"] == ["nightly", "stats-attempted", "trending"]
    assert exit_code == 1


async def test_successful_run_exits_zero(store: Store, tasks: dict[str, list[str]]) -> None:
    exit_code = await job.main(["--task", "trending"])

    assert tasks["ok"] == ["trending"]
    assert exit_code == 0


async def test_run_tasks_reports_per_task_status(
    store: Store, settings: Any, tasks: dict[str, list[str]]
) -> None:
    args = job.build_parser().parse_args(["--season", "2026"])

    results = await job.run_tasks(["nightly", "stats"], store, settings, args)

    assert results["nightly"] == {"status": "ok", "result": {"task": "nightly", "season": 2026}}
    assert results["stats"]["status"] == "failed"
    assert "nflverse exploded" in results["stats"]["error"]


async def test_unknown_task_names_are_reported_not_raised(
    store: Store, settings: Any, tasks: dict[str, list[str]]
) -> None:
    args = job.build_parser().parse_args([])

    results = await job.run_tasks(["nope", "trending"], store, settings, args)

    assert results["nope"]["status"] == "failed"
    assert results["trending"]["status"] == "ok"


def test_task_table_covers_every_documented_task() -> None:
    assert set(job.TASKS) == {
        "nightly",
        "stats",
        "backtest",
        "schedule",
        "trending",
        "precompute",
    }


async def test_a_cold_board_fails_the_run(store: Store, settings: Any) -> None:
    """A board precompute could not warm has to reach the exit code.

    The exit code is the whole alert: a scheduled run that warmed nothing but
    exited zero looks identical to a healthy one, while every paid call quietly
    falls back to the slow path the warming exists to remove. ``store`` is empty
    here, so every board is blocked on missing data.
    """
    args = job.build_parser().parse_args(["--task", "precompute", "--week", "4"])

    results = await job.run_tasks(["precompute"], store, settings, args)

    assert results["precompute"]["status"] == "failed"
    assert "left cold" in results["precompute"]["error"]


async def test_refresh_window_is_passed_through_in_seconds(
    store: Store, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is hours (what a cron interval is reasoned about in); the API is seconds."""
    seen: dict[str, Any] = {}

    async def capture(*_: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"warmed": 0}

    monkeypatch.setattr(job, "warm_response_cache", capture)
    args = job.build_parser().parse_args(["--task", "precompute", "--refresh-window", "3"])

    await job.task_precompute(store, settings, args)

    assert seen["refresh_window"] == 3 * 3600


# -- task chains -------------------------------------------------------------


def test_parser_accepts_a_comma_separated_chain_in_the_order_given() -> None:
    args = job.build_parser().parse_args(["--task", "stats,backtest"])
    assert job.selected_tasks(args.task) == ["stats", "backtest"]
    args = job.build_parser().parse_args(["--task", " precompute , trending "])
    assert job.selected_tasks(args.task) == ["precompute", "trending"]


def test_parser_rejects_a_chain_with_an_unknown_task() -> None:
    with pytest.raises(SystemExit):
        job.build_parser().parse_args(["--task", "stats,nonsense"])
    with pytest.raises(SystemExit):
        job.build_parser().parse_args(["--task", ","])


def test_parser_carries_the_stats_only_flag() -> None:
    assert job.build_parser().parse_args([]).stats_only is False
    args = job.build_parser().parse_args(["--task", "stats", "--season", "2025", "--stats-only"])
    assert args.stats_only is True and args.season == 2025


async def test_backtest_is_skipped_when_stats_failed_in_the_same_run(
    store: Store, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoring against a half-written week is permanent; not scoring costs a run."""
    ran: list[str] = []

    async def explode(*args: Any) -> dict[str, Any]:
        raise RuntimeError("nflverse 503")

    async def record(*args: Any) -> dict[str, Any]:
        ran.append("backtest")
        return {}

    monkeypatch.setitem(job.TASKS, "stats", explode)
    monkeypatch.setitem(job.TASKS, "backtest", record)
    results = await job.run_tasks(
        ["stats", "backtest"], store, settings, argparse.Namespace(season=None, week=None)
    )

    assert results["stats"]["status"] == "failed"
    assert results["backtest"] == {"status": "skipped", "error": "stats failed"}
    assert ran == []


async def test_chained_precompute_is_skipped_when_stats_failed_but_runs_alone(
    store: Store, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-stats re-warm follows stats; it is not clock-driven any more."""
    ran: list[str] = []

    async def explode(*args: Any) -> dict[str, Any]:
        raise RuntimeError("nflverse 503")

    def recorder(name: str):
        async def run(*args: Any) -> dict[str, Any]:
            ran.append(name)
            return {}

        return run

    monkeypatch.setitem(job.TASKS, "stats", explode)
    monkeypatch.setitem(job.TASKS, "backtest", recorder("backtest"))
    monkeypatch.setitem(job.TASKS, "precompute", recorder("precompute"))
    args = argparse.Namespace(season=None, week=None)

    chained = await job.run_tasks(["stats", "backtest", "precompute"], store, settings, args)
    assert chained["precompute"] == {"status": "skipped", "error": "stats failed"}
    assert ran == []

    alone = await job.run_tasks(["precompute"], store, settings, args)
    assert alone["precompute"]["status"] == "ok"
    assert ran == ["precompute"]


async def test_backtest_runs_after_a_successful_stats_in_the_same_run(
    store: Store, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def recorder(name: str):
        async def run(*args: Any) -> dict[str, Any]:
            order.append(name)
            return {}

        return run

    monkeypatch.setitem(job.TASKS, "stats", recorder("stats"))
    monkeypatch.setitem(job.TASKS, "backtest", recorder("backtest"))
    results = await job.run_tasks(
        ["stats", "backtest"], store, settings, argparse.Namespace(season=None, week=None)
    )

    assert order == ["stats", "backtest"]
    assert {name: r["status"] for name, r in results.items()} == {"stats": "ok", "backtest": "ok"}


async def test_only_limits_precompute_to_the_named_targets(
    store: Store, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_warm(store_: Any, settings_: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"warmed": 0}

    monkeypatch.setattr(job, "warm_response_cache", fake_warm)
    args = job.build_parser().parse_args(["--task", "precompute", "--only", "draft_board"])
    await job.task_precompute(store, settings, args)
    assert [t.endpoint_key for t in seen["targets"]] == ["draft_board"]

    with pytest.raises(ValueError):
        await job.task_precompute(
            store, settings, job.build_parser().parse_args(["--only", "nope"])
        )
