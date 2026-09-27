"""Play Clock ingest job — the only writer of the Firestore read models.

The API service never writes stats: everything under ``players/``,
``player_index/``, ``id_map/``, ``weekly_stats/``, ``usage_trends/``,
``def_vs_pos/``, ``injuries/``, ``depth_charts/``, ``schedules/``, ``trending/``
and ``meta/`` is produced here and read back through
:mod:`api.data.stats_store` (tech spec §4.2, §6). Doc shapes are specified by
that module's docstring; this package matches it exactly.

Tasks
-----
``nightly``
    Sleeper ``/players/nfl`` dump -> ``players/``, ``player_index/``, ``id_map/``.
``stats``
    nflverse (via ``nflreadpy``) -> ``weekly_stats/``, ``usage_trends/``,
    ``def_vs_pos/``, ``injuries/``, ``depth_charts/``, ``schedules/`` and
    ``meta/schedule_weeks``.
``schedule``
    Schedule plus ``meta/schedule_weeks`` only. Use at season rollover when the
    new schedule exists but nflverse has not published weekly player stats yet.
``trending``
    Sleeper trending add/drop -> ``trending/{add,drop}``.
``precompute``
    Generates the four league-wide boards (``trending``, ``sleepers``,
    ``waivers``, ``report``) and warms ``response_cache`` with them, so a paid
    call is a Firestore read instead of a ~72s LLM run. Must run last: it reads
    what the tasks above just wrote. See :mod:`ingest.precompute`.

Every data task stamps ``meta/freshness`` for the datasets it wrote.

Player id policy (important)
----------------------------
nflverse keys on ``gsis_id``; Sleeper (and therefore ``player_index``, which is
how the stats agent resolves a name) keys on ``player_id``. Since
:mod:`api.data.stats_store` documents ``weekly_stats``/``usage_trends`` doc ids
as **Sleeper** player ids, ingest translates through ``id_map/{gsis_id}`` built
by the ``nightly`` task:

* mapped player   -> doc id is the Sleeper ``player_id``; both ``player_id`` and
  ``gsis_id`` are stored as fields.
* unmapped player -> doc id falls back to the ``gsis_id`` (the row is preserved
  and counted, and it still contributes to ``def_vs_pos`` aggregates, but the
  stats agent cannot reach it by name). Unmapped counts are logged, never raised
  (tech spec §4.2).

Run ``nightly`` before ``stats`` on a cold store, or the first ``stats`` run
writes everything under gsis ids.

Running it
----------
Locally::

    uv run python -m ingest.job --task all            # nightly|stats|trending|precompute
    uv run python -m ingest.job --task schedule --season 2026
    uv run python -m ingest.job --task stats --season 2026 --log-level DEBUG

Exit code is ``0`` only when every requested task succeeded; one failing task
does not prevent the others from running.

As a Cloud Run Job (tech spec §1, §7) — the container is built in wave 3, this
is the contract it must satisfy:

* Image: python 3.12 base, ``uv sync --all-extras`` (the ``ingest`` extra brings
  ``nflreadpy`` + ``polars``; the API image omits it), entrypoint
  ``python -m ingest.job``.
* Job args select the task: ``gcloud run jobs execute ingest-stats --args=--task,stats``.
  Deploy one Cloud Run Job per cadence, or one job and override args per trigger.
* Env: ``STORE_BACKEND=firestore``, ``GOOGLE_CLOUD_PROJECT``, ``SEASON`` (only as
  a fallback — the live season comes from ``nflreadpy.get_current_season()``).
  Service account needs Firestore read/write only.
* Cloud Scheduler cadence (tech spec §4.2, §6):
  ``nightly`` daily ~04:00 ET, ``stats`` Tue/Thu/Sat mornings ET,
  ``trending`` every 30 minutes.
* Timeouts: give ``stats`` at least 15 minutes (nflverse downloads) and 2 GiB;
  ``trending`` finishes in seconds.
* Alerting: a non-zero exit is the ingest-failure signal (tech spec §7) — stale
  data silently degrades paid answers, so alert on it.
"""

from __future__ import annotations

__all__ = ["job", "nflverse_ingest", "sleeper_players", "trending"]
