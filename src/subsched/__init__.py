"""Subscription-aware coding agent scheduler."""

from importlib import metadata as _metadata


def __getattr__(name: str) -> str:
    if name == "__version__":
        return _metadata.version("agent-scheduler")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
