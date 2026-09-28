"""`AppError`: raised for application-layer problems the CLI reports as a usage error."""

from __future__ import annotations


class AppError(Exception):
    """A problem wiring the engine to a concrete project: bad profile, missing pack,
    missing adapter, no profile for a command that needs one, and so on.

    The CLI catches this (and `chipgraph.core.config.errors.ConfigError`) and exits 2.
    """
