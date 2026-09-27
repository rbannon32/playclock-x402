"""The board judge: scores are recorded, failures degrade, bad boards are loud."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ingest.judge import FLAG_THRESHOLD, Judge, JudgeScore, judge_board


class Scripted(Judge):
    name = "scripted"

    def __init__(self, score: JudgeScore | Exception) -> None:
        self._score = score
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def score(self, endpoint_key: str, body: dict[str, Any]) -> JudgeScore:
        self.calls.append((endpoint_key, body))
        if isinstance(self._score, Exception):
            raise self._score
        return self._score


def _score(**overrides: Any) -> JudgeScore:
    base = {
        "specificity": 4,
        "actionability": 4,
        "beyond_the_crowd": 4,
        "grounding": 5,
        "critique": "Fine.",
    }
    return JudgeScore(**{**base, **overrides})


def test_mean_is_the_unweighted_average() -> None:
    assert _score().mean == 4.25


def test_flagged_below_the_threshold() -> None:
    assert not _score().flagged
    low = _score(specificity=1, actionability=2, beyond_the_crowd=2, grounding=3)
    assert low.mean < FLAG_THRESHOLD
    assert low.flagged


def test_scores_are_bounded() -> None:
    with pytest.raises(ValueError):
        _score(specificity=6)
    with pytest.raises(ValueError):
        _score(grounding=0)


async def test_judge_board_records_the_score_and_who_gave_it() -> None:
    judge = Scripted(_score())
    result = await judge_board(judge, "report", {"verdict": "x", "meta": {"model": "m"}})
    assert result is not None
    assert result["mean"] == 4.25
    assert result["flagged"] is False
    assert result["judge"] == "scripted"
    assert result["critique"] == "Fine."
    assert judge.calls[0][0] == "report"


async def test_a_failing_judge_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    judge = Scripted(RuntimeError("quota"))
    with caplog.at_level(logging.WARNING):
        result = await judge_board(judge, "report", {"verdict": "x"})
    assert result is None
    assert "board judge failed" in caplog.text


async def test_a_flagged_board_is_logged_with_its_critique(
    caplog: pytest.LogCaptureFixture,
) -> None:
    judge = Scripted(
        _score(
            specificity=1,
            actionability=1,
            beyond_the_crowd=1,
            grounding=3,
            critique="Re-narrates adds.",
        )
    )
    with caplog.at_level(logging.WARNING):
        result = await judge_board(judge, "trending", {"verdict": "x"})
    assert result is not None and result["flagged"] is True
    assert "board quality flagged" in caplog.text
    assert "Re-narrates adds." in caplog.text


def test_the_judge_sees_the_engine_but_not_the_timestamps() -> None:
    from ingest.judge import MAX_BODY_CHARS, _judged_view

    view = _judged_view(
        {
            "verdict": "x",
            "meta": {
                "generated_at": "2026-09-03T21:47:29Z",
                "engine": "narrated",
                "model": "m",
                "cache": None,
            },
        }
    )
    assert view == {"verdict": "x", "meta": {"engine": "narrated", "model": "m"}}
    # A 200-row draft board is over 50k characters; the judge must read all of it.
    assert MAX_BODY_CHARS >= 200_000
