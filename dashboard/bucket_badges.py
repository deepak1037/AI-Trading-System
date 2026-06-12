"""Pure presentation helpers for the three-bucket dashboard (Phase 2 Step 10).

Kept free of Streamlit so the badge / progress / exit-recommendation logic is
unit-testable in isolation; the page modules import these and render the result.
"""

from __future__ import annotations

from typing import Any

from broker_client.buckets.exit_rules import ExitRulesEngine
from broker_client.buckets.models import BucketPosition

_BUCKET_BADGES = {
    1: "🟢 B1-MSP",
    2: "🟡 B2-Earnings",
    3: "🔴 B3-LEAP",
}


def bucket_badge(bucket: int | None, sub_type: str | None = None) -> str:
    """Coloured bucket badge, e.g. ``🟢 B1-MSP``. Unknown → grey badge."""
    badge = _BUCKET_BADGES.get(int(bucket) if bucket is not None else 0, "⚪ B?")
    if sub_type:
        badge = f"{badge} ({sub_type})"
    return badge


def recoverable_badge(recoverable: Any) -> str:
    """♻️ Recoverable vs ⚠️ Defined Risk."""
    is_recoverable = bool(recoverable) if recoverable is not None else True
    return "♻️ Recoverable" if is_recoverable else "⚠️ Defined Risk"


def dte_progress_bar(dte_remaining: int | None, original_dte: int | None, width: int = 10) -> str:
    """ASCII countdown bar showing elapsed time, e.g. ``███░░░░░░░ 21d``.

    Fills with elapsed proportion (original − remaining)/original.
    """
    rem = int(dte_remaining or 0)
    orig = int(original_dte or 0)
    if orig <= 0:
        return f"{'░' * width} {rem}d"
    used = max(0.0, min(1.0, (orig - rem) / orig))
    filled = int(round(used * width))
    return f"{'█' * filled}{'░' * (width - filled)} {rem}d"


def exit_recommendation(row: Any, engine: ExitRulesEngine | None = None) -> str:
    """Run the exit-rules engine over a positions row and return the action."""
    engine = engine or ExitRulesEngine()
    bp = row_to_bucket_position(row)
    rec = engine.check_exit(bp)
    return rec.action if rec else "HOLD"


def row_to_bucket_position(row: Any) -> BucketPosition:
    """Build a BucketPosition from a positions-table row (dict or object)."""
    get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
    return BucketPosition(
        ticker=str(get("ticker", "?") or "?"),
        strategy=get("strategy", "") or "",
        bucket=int(get("bucket", 1) or 1),
        sub_type=get("sub_type", "msp") or "msp",
        recoverable=bool(get("recoverable", True)),
        position_type=get("position_type", "equity_long") or "equity_long",
        option_type=get("option_type"),
        strike=get("strike"),
        expiry=get("expiry"),
        qty=int(get("qty", 1) or 1),
        entry_price=float(get("entry_price", 0.0) or 0.0),
        current_price=float(get("current_price", 0.0) or 0.0),
        underlying_price=get("underlying_price"),
        profit_pct=float(get("profit_pct", 0.0) or 0.0),
        dte_remaining=int(get("dte_remaining", 0) or 0),
        original_dte=int(get("original_dte", 0) or 0),
        thesis_status=get("thesis_status", "active") or "active",
        thesis_completion_pct=float(get("thesis_completion_pct", 0.0) or 0.0),
    )


def thesis_label(status: str | None, completion_pct: Any) -> str:
    """e.g. ``active (40% complete)``."""
    status = status or "active"
    try:
        pct = float(completion_pct or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    return f"{status} ({pct:.0f}% complete)"


__all__ = [
    "bucket_badge",
    "recoverable_badge",
    "dte_progress_bar",
    "exit_recommendation",
    "row_to_bucket_position",
    "thesis_label",
]
