"""discord_alerts/capital_manager.py

Capital-rotation decision logic, shared by the backtest and the live bridge.

When a new Ravish signal arrives and buying power is short, it can be worth
closing an aged winner — one with little premium left to squeeze — to fund the
new opportunity. This module decides WHICH open positions (if any) to close,
and only does so when the swap actually improves expected profit.

Key property: rotation REPLACES a position rather than adding one, so it lifts
capital *velocity* without raising peak concurrent exposure (the crash risk).

The function is pure (no I/O) so it can be unit-tested and reused unchanged by
both the offline backtest and the live paper bridge.
"""

from __future__ import annotations

from typing import Optional


def pick_rotations(
    need_cash: float,
    available_cash: float,
    new_expected_pnl: float,
    candidates: list[dict],
    threshold_pct: float,
) -> list:
    """Choose open positions to close so a new trade can be funded.

    Args:
        need_cash:        capital the new trade requires.
        available_cash:   cash on hand right now.
        new_expected_pnl: $ profit the new trade is expected to make if it
                          reaches its own profit target.
        candidates:       open positions, each a dict with:
                            - "id":            any handle the caller maps back
                            - "mark_pct":      current profit % (None → ineligible)
                            - "capital":       $ freed if this position is closed
                            - "remaining_pnl": $ still expected if held to its PT
        threshold_pct:    only positions at or above this profit % may be rotated
                          (don't disturb trades that haven't matured).

    Returns:
        A list of candidate "id"s to close, chosen most-profitable-first (least
        remaining upside), each only if the new trade's expected profit exceeds
        that position's remaining profit. Returns [] if rotation cannot free
        enough — in which case the caller should skip the new trade rather than
        close positions for nothing.
    """
    if need_cash <= available_cash:
        return []                      # no rotation needed
    pool = [c for c in candidates
            if c.get("mark_pct") is not None and c["mark_pct"] >= threshold_pct]
    # Most-profitable first: highest mark = least premium left = cheapest to give up.
    pool.sort(key=lambda c: -c["mark_pct"])

    cash = available_cash
    chosen = []
    for c in pool:
        if cash >= need_cash:
            break
        # Only rotate when the NEW trade is expected to out-earn what we'd be
        # giving up by closing this one early. Never trade down.
        if new_expected_pnl <= c.get("remaining_pnl", 0.0):
            continue
        chosen.append(c["id"])
        cash += c["capital"]
    return chosen if cash >= need_cash else []


__all__ = ["pick_rotations"]
