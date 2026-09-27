"""Internal pipeline models.

The product contracts live in :mod:`api.schemas` and are never duplicated here.
This module holds the one derived shape the ADK pipeline needs: the **synthesis
schema**, which is the endpoint's response model minus its ``meta`` block.

Why drop ``meta``
-----------------
:class:`~api.schemas.AnalysisMeta` is pure provenance — when the analysis ran,
how stale each dataset was, which model wrote it, whether the body came from
cache. Every one of those facts is known to the *engine*, not to the model.
Asking Gemini to emit a ``generated_at`` timestamp and a ``data_freshness`` map
would spend tokens on values we then have to overwrite anyway, and would give the
model an opportunity to invent a freshness marker — exactly the class of
fabrication the whole design is built to prevent.

So the synthesis agent is constrained to everything *except* ``meta``, and
:class:`~api.agents.pipeline.AdkAnalysisEngine` attaches the real
:class:`~api.schemas.AnalysisMeta` before validating against the true response
model. The schemas are *derived* from the response models with
``pydantic.create_model``, so a field added to a contract in ``api/schemas.py``
automatically appears in the corresponding synthesis schema.
"""

from __future__ import annotations

from functools import cache

from pydantic import BaseModel, ConfigDict, create_model

from api.agents.engine import RESPONSE_MODELS
from api.schemas import AnalysisResponse

#: Field excluded from every synthesis schema — the engine owns it.
ENGINE_OWNED_FIELDS: frozenset[str] = frozenset({"meta"})


@cache
def synthesis_schema_for(response_model: type[AnalysisResponse]) -> type[BaseModel]:
    """Return ``response_model`` minus the engine-owned provenance fields.

    Derived, never hand-written: the returned model carries the same field
    annotations, defaults and descriptions as the contract it came from, so the
    JSON schema the model is constrained to stays in lockstep with the OpenAPI
    surface agents read before paying.

    Args:
        response_model: One of the values of
            :data:`api.agents.engine.RESPONSE_MODELS`.

    Returns:
        A new ``BaseModel`` subclass named ``<Model>Synthesis``. Cached, so the
        same class object is reused across runs (ADK and google-genai both key
        schema conversion off the class).
    """
    fields = {
        name: (field.annotation, field)
        for name, field in response_model.model_fields.items()
        if name not in ENGINE_OWNED_FIELDS
    }
    model = create_model(  # type: ignore[call-overload]
        f"{response_model.__name__}Synthesis",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    model.__doc__ = (
        f"{response_model.__name__} as produced by the synthesis agent: every "
        f"field except {sorted(ENGINE_OWNED_FIELDS)}, which the engine attaches."
    )
    return model


def synthesis_schema(endpoint_key: str) -> type[BaseModel]:
    """Return the synthesis schema for ``endpoint_key``.

    Raises:
        KeyError: If ``endpoint_key`` is not a known paid endpoint.
    """
    return synthesis_schema_for(RESPONSE_MODELS[endpoint_key])
