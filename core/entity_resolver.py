"""
Entity alias resolution — maps variant names to canonical entity names.
"""

from __future__ import annotations


class EntityResolver:
    """Maps variant names/handles to canonical entity names."""

    def __init__(self, alias_map: dict[str, list[str]] | None):
        alias_map = alias_map or {}
        self._lookup: dict[str, str] = {}
        for canonical, variants in alias_map.items():
            self._lookup[canonical.lower()] = canonical
            for v in variants:
                self._lookup[v.lower()] = canonical

    def resolve(self, name: str) -> str:
        return self._lookup.get(name.lower().strip(), name.strip())

    def resolve_list(self, names: list[str]) -> list[str]:
        return [self.resolve(n) for n in names]
