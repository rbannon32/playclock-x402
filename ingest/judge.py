"""An LLM judge for the boards we are about to sell.

:mod:`api.evals.quality` catches the failures a rule can name — a board that
cites only add counts, a name with no id, a padded list. It cannot tell a
sharp briefing from a bland one. This can, roughly: one Gemini call per warmed
board scores it against a fixed rubric and writes the score next to the
cached body, so the question "did the boards get worse this week" has a
number and a log line instead of a feeling.

It runs inside the ingest job, where Vertex credentials already exist and
latency is free, never on a paid request. It never blocks a warm: a judge
failure is logged and the board is served. What it does do is flag — a score
under :data:`FLAG_THRESHOLD` is logged at WARNING on the ``board quality``
line ``infra/deploy.md`` §6 alerts on.

The rubric is deliberately short and asks the four things a payer would:

* **specificity** — does every callout carry a number or a reported event, or
  is it adjectives?
* **actionability** — could a manager act on this today without a second
  source?
* **beyond_the_crowd** — does it say anything Sleeper's free trending list
  does not, and does it disagree with the crowd where the data supports it?
* **grounding** — does the prose match the cited numbers, with nothing
  asserted that the body does not contain?

Scores are 1-5. The judge never sees the store; it judges the body as a
payer would, and grounding is judged against the body's own ``stats_cited``.
"""

from __future__ import annotations

import abc
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from api.agents.vertex import genai_client, thinking_config
from api.core.config import Settings

logger = logging.getLogger(__name__)

#: A mean rubric score below this flags the board.
FLAG_THRESHOLD = 3.0

#: How much of a body the judge reads. A 200-row draft board is over 50,000
#: characters; the first cap (40,000) cut it at player 162 and the judge
#: docked it for "cutting off mid-sentence". Gemini Flash reads a million
#: tokens; this is a guard against a pathological body, not a budget.
MAX_BODY_CHARS = 400_000


class JudgeScore(BaseModel):
    """One board's rubric scores, 1 (poor) to 5 (excellent)."""

    specificity: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)
    beyond_the_crowd: int = Field(ge=1, le=5)
    grounding: int = Field(ge=1, le=5)
    critique: str = Field(description="One or two sentences: the single biggest weakness.")

    @property
    def mean(self) -> float:
        """Unweighted mean of the four scores."""
        return round(
            (self.specificity + self.actionability + self.beyond_the_crowd + self.grounding) / 4.0,
            2,
        )

    @property
    def flagged(self) -> bool:
        """Whether this board falls under :data:`FLAG_THRESHOLD`."""
        return self.mean < FLAG_THRESHOLD


RUBRIC = (
    "You are grading a paid NFL fantasy football analysis that a stranger just "
    "bought for a few cents. Grade it as that buyer, not as its author. Score "
    "each dimension 1-5:\n"
    "- specificity: 5 = every callout carries a number or a reported event; "
    "1 = adjectives and generalities.\n"
    "- actionability: 5 = a manager could act on this today with no second "
    "source; 1 = nothing here changes a decision.\n"
    "- beyond_the_crowd: 5 = it says things the free Sleeper trending list does "
    "not and disagrees with the crowd where the numbers support it; 1 = it "
    "re-narrates the most-added players and their add counts.\n"
    "- grounding: 5 = every number in the prose appears in stats_cited and "
    "nothing is asserted that the body does not contain; 1 = claims float free "
    "of the data. When meta.engine is 'narrated', the per-row notes and every "
    "rank, market_rank and value_delta were computed by code from the data "
    "and are grounded by construction — judge grounding on the verdict and "
    "reasoning only.\n"
    "Return ONLY a JSON object with integer fields specificity, actionability, "
    "beyond_the_crowd, grounding and a one-or-two-sentence string field critique "
    "naming the single biggest weakness."
)


class Judge(abc.ABC):
    """Scores one response body against :data:`RUBRIC`."""

    name: str = "abstract"

    @abc.abstractmethod
    async def score(self, endpoint_key: str, body: dict[str, Any]) -> JudgeScore:
        """Return the rubric scores for ``body``. May raise; callers degrade."""


class GeminiJudge(Judge):
    """The real judge: one structured-output Gemini call on Vertex AI.

    ``google.genai`` is imported lazily so this module, and the ingest job's
    import graph, stay credential-free until a score is actually requested.
    """

    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = genai_client(self._settings)
        return self._client

    async def score(self, endpoint_key: str, body: dict[str, Any]) -> JudgeScore:
        from google.genai import types  # noqa: PLC0415

        payload = json.dumps(_judged_view(body), default=str)[:MAX_BODY_CHARS]
        prompt = f"{RUBRIC}\n\nEndpoint: {endpoint_key}\nResponse body:\n{payload}"
        response = await self._get_client().aio.models.generate_content(
            model=self._settings.model_id,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JudgeScore,
                temperature=0.0,
                thinking_config=thinking_config(self._settings),
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, JudgeScore):
            return parsed
        text = getattr(response, "text", None) or ""
        return JudgeScore.model_validate_json(text)


def _judged_view(body: dict[str, Any]) -> dict[str, Any]:
    """The body as the judge should see it: the answer, plus which engine wrote it.

    Timestamps and freshness markers are noise to a reader; ``engine`` and
    ``model`` are not, because the rubric grades a narrated body's computed
    rows differently from a synthesizer's prose.
    """
    meta = body.get("meta") or {}
    view = {key: value for key, value in body.items() if key != "meta"}
    if isinstance(meta, dict):
        view["meta"] = {k: meta.get(k) for k in ("engine", "model") if meta.get(k) is not None}
    return view


async def judge_board(
    judge: Judge, endpoint_key: str, body: dict[str, Any]
) -> dict[str, Any] | None:
    """Score one board, degrading to ``None`` on any failure.

    Returns a JSON-able dict — the four scores, the mean, ``flagged`` and the
    critique — suitable for the ``quality/`` document precompute writes.
    """
    try:
        score = await judge.score(endpoint_key, body)
    except Exception as exc:  # noqa: BLE001 - the judge never blocks a warm
        logger.warning("board judge failed for %s (%s: %s)", endpoint_key, type(exc).__name__, exc)
        return None
    result = {
        **score.model_dump(),
        "mean": score.mean,
        "flagged": score.flagged,
        "judge": judge.name,
    }
    if score.flagged:
        logger.warning(
            "board quality flagged: %s scored %.2f/5 by %s — %s",
            endpoint_key,
            score.mean,
            judge.name,
            score.critique,
        )
    else:
        logger.info("board quality: %s scored %.2f/5 by %s", endpoint_key, score.mean, judge.name)
    return result
