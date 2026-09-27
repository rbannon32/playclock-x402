"""Golden-query evaluation suite (tech spec §5).

Twenty property-asserted queries over a hermetic fixture season. The
deterministic run is the CI gate — it proves the response contracts hold and
that no cited number was invented — and the same suite runs against the real ADK
pipeline before a weekly deploy.

    uv run python -m api.evals.run_evals
"""

from __future__ import annotations

from api.evals.golden import GOLDEN_CASES, GoldenCase, check_citations_traceable, seed_store

# api.evals.run_evals is deliberately NOT imported here: importing it eagerly
# makes `python -m api.evals.run_evals` warn about a doubly-loaded module.
# Import it directly (`from api.evals.run_evals import run_evals`) instead.

__all__ = [
    "GOLDEN_CASES",
    "GoldenCase",
    "check_citations_traceable",
    "seed_store",
]
