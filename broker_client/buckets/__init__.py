"""Three-bucket trading framework (Phase 2).

Bucket 1 — MSP / Wheel (income engine, recoverable via rolls)
Bucket 2 — Earnings plays (2A puts recoverable, 2B defined-risk spreads)
Bucket 3 — Event / LEAP (binary lotto plays, not recoverable)

Public surface:
    BucketPosition       — enriched position consumed by exit logic
    BucketManager        — classification, capacity, P&L per bucket
    ExitRulesEngine      — deterministic exit recommendations (source of truth)
    LLMExitEngine        — Claude-assisted exit review (supplements the rules)
    WheelTracker         — wheel-cycle state machine
"""

from __future__ import annotations

from broker_client.buckets.bucket_manager import (
    BucketClassification,
    BucketManager,
    BucketPnL,
)
from broker_client.buckets.models import BucketPosition

__all__ = [
    "BucketPosition",
    "BucketClassification",
    "BucketManager",
    "BucketPnL",
]
