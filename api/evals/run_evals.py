"""Run the golden-query suite. CI gate and pre-deploy check (tech spec §5).

Usage::

    uv run python -m api.evals.run_evals                      # deterministic (default)
    uv run python -m api.evals.run_evals --engine adk         # the real pipeline
    uv run python -m api.evals.run_evals --case player_by_name --verbose

The deterministic run is hermetic — in-memory store, seeded fixture, no network,
no credentials — so it belongs in CI on every commit. The ``adk`` run costs money
and needs Vertex AI credentials; when they are absent it prints why and exits 0
rather than failing a build for a missing secret.

Exit codes: ``0`` all cases passed (or the run was skipped for missing
credentials), ``1`` at least one case failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

from api.agents.engine import AnalysisEngine
from api.core.config import Settings
from api.core.store import MemoryStore, Store
from api.evals.golden import (
    GOLDEN_CASES,
    SEASON,
    WEEK,
    GoldenCase,
    seed_store,
    universal_assertions,
)

#: Env vars that indicate Vertex AI is reachable. Any one is enough — Cloud Run
#: uses the metadata server and sets GOOGLE_CLOUD_PROJECT, a laptop uses ADC.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_API_KEY",
)


@dataclass
class CaseResult:
    """Outcome of one golden query."""

    case: GoldenCase
    failures: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def passed(self) -> bool:
        """Whether the case produced no failures and did not raise."""
        return not self.failures and self.error is None


@dataclass
class EvalReport:
    """Outcome of a whole eval run."""

    engine_name: str
    results: list[CaseResult] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def failures(self) -> list[CaseResult]:
        """Cases that did not pass."""
        return [r for r in self.results if not r.passed]

    @property
    def passed(self) -> bool:
        """Whether every case passed (a skipped run counts as passed)."""
        return not self.failures

    def render(self, verbose: bool = False) -> str:
        """Format the report for a terminal or a CI log."""
        if self.skipped_reason:
            return f"SKIPPED ({self.engine_name}): {self.skipped_reason}"
        lines = [f"Golden queries — engine={self.engine_name}, {len(self.results)} case(s)", ""]
        for result in self.results:
            mark = "PASS" if result.passed else "FAIL"
            lines.append(f"  [{mark}] {result.case.name}  ({result.elapsed_ms:.0f}ms)")
            if verbose:
                lines.append(f"         {result.case.intent}")
            if result.error:
                lines.append(f"         ERROR: {result.error}")
            for failure in result.failures:
                lines.append(f"         - {failure}")
        passed = len(self.results) - len(self.failures)
        lines += ["", f"{passed}/{len(self.results)} passed"]
        return "\n".join(lines)


async def run_case(engine: AnalysisEngine, case: GoldenCase) -> CaseResult:
    """Run one golden query and collect its property failures."""
    started = time.perf_counter()
    try:
        response = await engine.analyze(case.endpoint_key, dict(case.request_context))
    except Exception as exc:  # noqa: BLE001 - a raised engine is a failed case, not a crash
        return CaseResult(
            case=case,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    failures = universal_assertions(response, case)
    for assertion in case.assertions:
        try:
            failures += assertion(response, case)
        except Exception as exc:  # noqa: BLE001 - a broken assertion is a failure too
            failures.append(f"assertion {assertion.__name__} raised {type(exc).__name__}: {exc}")
    return CaseResult(
        case=case, failures=failures, elapsed_ms=(time.perf_counter() - started) * 1000
    )


async def run_evals(
    engine: AnalysisEngine,
    cases: tuple[GoldenCase, ...] = GOLDEN_CASES,
) -> EvalReport:
    """Run every golden query against ``engine``.

    Args:
        engine: The engine under test. Callers own its store — seed it with
            :func:`api.evals.golden.seed_store` first.
        cases: Cases to run; defaults to the full golden set.

    Returns:
        An :class:`EvalReport`. Never raises for a failing case.
    """
    report = EvalReport(engine_name=engine.name)
    for case in cases:
        report.results.append(await run_case(engine, case))
    return report


def eval_settings(engine: str = "deterministic") -> Settings:
    """Settings pinned to the fixture season/week, isolated from the environment."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine=engine,  # type: ignore[arg-type]
        x402_mode="disabled",
        season=SEASON,
        week_override=WEEK,
    )


async def build_engine(engine_name: str, store: Store) -> AnalysisEngine:
    """Construct the engine under test against an already-seeded ``store``."""
    settings = eval_settings(engine_name)
    if engine_name == "adk":
        from api.agents.pipeline import AdkAnalysisEngine  # noqa: PLC0415

        return AdkAnalysisEngine(store=store, settings=settings)
    from api.agents.deterministic import DeterministicAnalysisEngine  # noqa: PLC0415

    return DeterministicAnalysisEngine(store=store, settings=settings)


def missing_credentials() -> str | None:
    """Return why an ADK run cannot happen here, or ``None`` if it can."""
    if any(os.environ.get(var) for var in CREDENTIAL_ENV_VARS):
        return None
    return (
        "no Vertex AI credentials in the environment (set one of "
        + ", ".join(CREDENTIAL_ENV_VARS)
        + "). The deterministic suite is the offline CI gate; run --engine adk "
        "from an authenticated environment before a weekly deploy."
    )


async def main_async(argv: list[str] | None = None) -> int:
    """Parse args, run the suite, print the report, return an exit code."""
    parser = argparse.ArgumentParser(description="Run the Play Clock golden queries.")
    parser.add_argument(
        "--engine",
        choices=("deterministic", "adk"),
        default="deterministic",
        help="Which analysis engine to evaluate. 'adk' needs Vertex AI credentials.",
    )
    parser.add_argument("--case", action="append", default=None, help="Run only the named case(s).")
    parser.add_argument("--verbose", action="store_true", help="Print each case's intent.")
    args = parser.parse_args(argv)

    if args.engine == "adk":
        reason = missing_credentials()
        if reason:
            print(EvalReport(engine_name="adk", skipped_reason=reason).render())
            return 0

    cases = GOLDEN_CASES
    if args.case:
        wanted = set(args.case)
        cases = tuple(case for case in GOLDEN_CASES if case.name in wanted)
        unknown = wanted - {case.name for case in GOLDEN_CASES}
        if unknown:
            print(f"unknown case(s): {sorted(unknown)}", file=sys.stderr)
            return 1

    store = await seed_store(MemoryStore())
    engine = await build_engine(args.engine, store)
    report = await run_evals(engine, cases)
    print(report.render(verbose=args.verbose))
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    """Synchronous entry point (``python -m api.evals.run_evals``)."""
    return asyncio.run(main_async(argv))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
