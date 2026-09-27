"""Regression checks for headers on nginx locations with local add_header rules."""

from pathlib import Path

CONFIG = (Path(__file__).parents[1] / "infra" / "nginx.conf.template").read_text()
SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Strict-Transport-Security",
)


def test_security_headers_repeated_in_cache_override_locations() -> None:
    """nginx drops inherited add_header values when a location defines one."""
    for header in SECURITY_HEADERS:
        assert CONFIG.count(f"add_header {header} ") == 3, header


def test_security_headers_apply_to_error_responses_too() -> None:
    for header in SECURITY_HEADERS:
        lines = [line for line in CONFIG.splitlines() if f"add_header {header} " in line]
        assert lines
        assert all(line.rstrip().endswith("always;") for line in lines)
