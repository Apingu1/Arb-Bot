from __future__ import annotations

from decimal import Decimal

from .models import FillSegment


CRYPTO_TAKER_FEE_RATE = Decimal("0.07")


def taker_fee_for_segment(segment: FillSegment, fee_rate: Decimal = CRYPTO_TAKER_FEE_RATE) -> Decimal:
    """Polymarket fee = shares × feeRate × p × (1-p)."""
    p = segment.price
    return segment.shares * fee_rate * p * (Decimal("1") - p)


def taker_fee(segments: tuple[FillSegment, ...], fee_rate: Decimal = CRYPTO_TAKER_FEE_RATE) -> Decimal:
    return sum((taker_fee_for_segment(s, fee_rate) for s in segments), Decimal("0"))
