"""
Source collector protocol and registry.

Each platform (YouTube, Reddit, …) registers a collector here.
The pipeline calls get_collector(source_type).collect(…) without
knowing which platform it is talking to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


@dataclass
class CollectResult:
    new_comments: int = 0
    quota_info: str = ""


@runtime_checkable
class SourceCollector(Protocol):
    """Protocol every platform collector must implement."""

    source_type: str

    def collect(
        self,
        conn,
        source_id: str,
        settings: dict[str, str],
        progress_callback: Callable[[str], None] | None = None,
    ) -> CollectResult:
        ...


# ── Registry ──────────────────────────────────────────────────────────────────

_collectors: dict[str, SourceCollector] = {}


def register_collector(collector: SourceCollector) -> None:
    _collectors[collector.source_type] = collector


def get_collector(source_type: str) -> SourceCollector:
    if source_type not in _collectors:
        raise ValueError(
            f"No collector registered for source_type={source_type!r}. "
            f"Registered: {list(_collectors)}"
        )
    return _collectors[source_type]


def registered_source_types() -> list[str]:
    return list(_collectors)
