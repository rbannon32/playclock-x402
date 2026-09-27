"""One-off operational entry points that ship inside the API image.

These live under ``api/`` for the same reason :mod:`api.data.sources` does:
the runtime image copies ``api/`` and nothing else, so a module outside it
cannot be run from the deployed container at all.
"""
