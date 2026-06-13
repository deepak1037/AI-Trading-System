"""Earnings analyzer (Phase 3, Module A).

Identifies earnings plays from Moomoo IV data:
  * IV Crush  — sell premium before earnings, profit from IV collapse.
  * IV Spike  — buy premium when IV is low, exit BEFORE earnings on IV expansion.

Public surface is intentionally small; import the pieces you need from their
submodules (``models``, ``iv_analyzer``, ``strategy_builder``, ``llm_assessor``,
``earnings_scanner``).
"""

from __future__ import annotations

__all__: list[str] = []
