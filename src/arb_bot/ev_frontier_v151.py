from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Any

from .config import Settings
from .ev_frontier import EVFrontierSuite, FrontierHedgeVariant, ZERO, ONE
from .fees import taker_fee
from .hedgeable_research import HedgeCampaign, HedgeIntent, _edge_label, _floor_to_tick, _quote_payload
from .maker_research import MarketRegimeTracker
from .models import MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


class CorrectedFrontierHedgeVariant(FrontierHedgeVariant):
    """Phase 1.5.1 EV variant with a research-specific absolute-profit gate.

    Phase 1.5 inherited HEDGE_MIN_EXPECTED_PROFIT_USDC=0.10 from the
    production-style HEDGE controls. That unintentionally prevented the 5-share
    0.5c/share experiments from placing at all. Phase 1.5.1 keeps the per-share
    edge target authoritative and applies EV_MIN_EXPECTED_PROFIT_USDC instead.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ghost_best_recovery_pnls: list[Decimal] = []
        self.ghost_best_recovery_pnl_per_share: list[Decimal] = []
        self.ghost_fill_elapsed_ms: list[Decimal] = []
        self.ghost_reason_pnls: dict[str, list[Decimal]] = defaultdict(list)
        self.ghost_reason_elapsed_ms: dict[str, list[Decimal]] = defaultdict(list)

    def _side_intent(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        *,
        maker_side: str,
        shares: Decimal,
    ) -> HedgeIntent | None:
        maker_book = engine.books.get(pair.token_a if maker_side == "A" else pair.token_b)
        hedge_book = engine.books.get(pair.token_b if maker_side == "A" else pair.token_a)
        if not maker_book or not hedge_book or not maker_book.ready or not hedge_book.ready:
            return None
        best_bid = maker_book.best_bid()
        best_ask = maker_book.best_ask()
        if best_bid is None or best_ask is None:
            return None

        quote = hedge_book.quote_buy(shares)
        if quote is None:
            return None
        fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate)
        reserve = shares * self.settings.hedge_latency_reserve_per_share
        required_profit = shares * self.edge_target
        max_maker_notional = shares - quote.notional - fee - reserve - required_profit
        if max_maker_notional <= ZERO:
            return None
        max_maker_price = max_maker_notional / shares

        tick = self.settings.maker_tick_size
        passive_cap = best_ask - tick
        improve_cap = best_bid + tick * Decimal(self.settings.hedge_max_improve_ticks)
        maker_price = min(_floor_to_tick(max_maker_price, tick), passive_cap, improve_cap)
        if maker_price < tick:
            return None

        expected = shares - shares * maker_price - quote.notional - fee - reserve
        edge = expected / shares
        if edge < self.edge_target or expected < self.settings.ev_min_expected_profit_usdc:
            return None

        queue = maker_book.bids.get(maker_price, ZERO)
        return HedgeIntent(
            maker_side=maker_side,
            hedge_side="B" if maker_side == "A" else "A",
            shares=shares,
            maker_price=maker_price,
            opposite_quote=quote,
            opposite_fee=fee,
            expected_profit_after_reserve=expected,
            expected_edge_after_reserve=edge,
            queue_ahead=queue,
            regime=self._market_regime(engine, pair),
        )

    def _current_hedge_state(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: HedgeCampaign,
    ) -> tuple[bool, Decimal | None, Decimal | None, str | None]:
        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        if not hedge_book or not hedge_book.ready:
            return False, None, None, "NO_HEDGE_BOOK"
        quote = hedge_book.quote_buy(campaign.target_shares)
        if quote is None:
            return False, None, None, "NO_HEDGE_DEPTH"
        net, edge, _ = self._decision_economics(quote, campaign.maker_price, campaign.target_shares)
        if edge >= self.edge_target and net >= self.settings.ev_min_expected_profit_usdc:
            return True, net, edge, None
        if edge <= -self.settings.ev_hard_loss_per_share:
            return False, net, edge, "HARD_LOSS"
        return False, net, edge, None

    def _record_ghost_fill(self, engine: ArbitrageEngine, pair: MarketPair, ghost, fill: Decimal) -> None:
        hedge_book = engine.books.get(pair.token_b if ghost.hedge_side == "B" else pair.token_a)
        maker_book = engine.books.get(pair.token_a if ghost.maker_side == "A" else pair.token_b)
        completion = hedge_book.quote_buy(fill) if hedge_book else None
        completion_fee = taker_fee(completion.segments, self.settings.crypto_taker_fee_rate) if completion else ZERO
        completion_pnl = (
            fill - fill * ghost.maker_price - completion.notional - completion_fee
            if completion
            else None
        )
        unwind = maker_book.quote_sell(fill) if maker_book else None
        unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate) if unwind else ZERO
        unwind_pnl = unwind.notional - fill * ghost.maker_price - unwind_fee if unwind else -(fill * ghost.maker_price)
        best_recovery = max(completion_pnl, unwind_pnl) if completion_pnl is not None else unwind_pnl
        elapsed_ms = Decimal(str((__import__("time").monotonic() - ghost.cancelled_at) * 1000))
        pnl_per_share = best_recovery / fill if fill > ZERO else ZERO

        self.ghost_filled += 1
        if best_recovery > ZERO:
            self.ghost_profitable += 1
        target_profit = fill * ghost.edge_target
        if completion_pnl is not None and completion_pnl >= target_profit:
            self.ghost_target_profitable += 1

        self.ghost_best_recovery_pnls.append(best_recovery)
        self.ghost_best_recovery_pnl_per_share.append(pnl_per_share)
        self.ghost_fill_elapsed_ms.append(elapsed_ms)
        self.ghost_reason_pnls[ghost.cancel_reason].append(best_recovery)
        self.ghost_reason_elapsed_ms[ghost.cancel_reason].append(elapsed_ms)

        self.recorder.write(
            "hedge_ghost_outcome",
            {
                "strategy": self.strategy_name,
                "market_id": ghost.market_id,
                "slug": ghost.slug,
                "cancel_reason": ghost.cancel_reason,
                "outcome": "WOULD_FILL",
                "maker_side": ghost.maker_side,
                "hedge_side": ghost.hedge_side,
                "maker_price": ghost.maker_price,
                "fill_qty": fill,
                "elapsed_ms": elapsed_ms,
                "completion_quote": _quote_payload(completion),
                "completion_pnl": completion_pnl,
                "unwind_quote": _quote_payload(unwind),
                "unwind_pnl": unwind_pnl,
                "best_recovery_pnl": best_recovery,
                "best_recovery_pnl_per_share": pnl_per_share,
                "would_be_profitable": best_recovery > ZERO,
                "would_clear_original_target": completion_pnl is not None and completion_pnl >= target_profit,
            },
        )

    @staticmethod
    def _avg(values: list[Decimal]) -> Decimal:
        return sum(values, ZERO) / Decimal(len(values)) if values else ZERO

    def _ghost_reason_stats(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for reason, pnls in sorted(self.ghost_reason_pnls.items()):
            elapsed = self.ghost_reason_elapsed_ms.get(reason, [])
            result[reason] = {
                "count": len(pnls),
                "avg_pnl": self._avg(pnls),
                "median_pnl": median(pnls) if pnls else ZERO,
                "best_pnl": max(pnls) if pnls else ZERO,
                "worst_pnl": min(pnls) if pnls else ZERO,
                "profitable": sum(1 for value in pnls if value > ZERO),
                "avg_elapsed_ms": self._avg(elapsed),
            }
        return result

    @property
    def has_ev_evidence(self) -> bool:
        return self.maker_fills > 0 or self.ghost_filled > 0

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "sample_status": "OK" if self.has_ev_evidence else "INSUFFICIENT_DATA",
                "ghost_avg_best_recovery_pnl": self._avg(self.ghost_best_recovery_pnls),
                "ghost_median_best_recovery_pnl": median(self.ghost_best_recovery_pnls) if self.ghost_best_recovery_pnls else ZERO,
                "ghost_best_recovery_pnl": max(self.ghost_best_recovery_pnls) if self.ghost_best_recovery_pnls else ZERO,
                "ghost_worst_recovery_pnl": min(self.ghost_best_recovery_pnls) if self.ghost_best_recovery_pnls else ZERO,
                "ghost_avg_best_recovery_pnl_per_share": self._avg(self.ghost_best_recovery_pnl_per_share),
                "ghost_avg_elapsed_ms": self._avg(self.ghost_fill_elapsed_ms),
                "ghost_reason_stats": self._ghost_reason_stats(),
                "ev_min_expected_profit_usdc": self.settings.ev_min_expected_profit_usdc,
            }
        )
        if not self.has_ev_evidence:
            row["strategy"] = f"{self.strategy_name} [INSUFFICIENT_DATA]"
        return row


class CorrectedEVFrontierSuite(EVFrontierSuite):
    """Phase 1.5.1 suite that makes the 5-share grace experiment valid."""

    def __init__(self, settings: Settings, recorder: JsonlRecorder, regime_tracker: MarketRegimeTracker) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime_tracker = regime_tracker
        self.variants: list[CorrectedFrontierHedgeVariant] = []

        grace_size = settings.ev_grace_trade_shares
        for grace in settings.ev_grace_periods_ms:
            name = f"EV-G{grace}-S{format(grace_size.normalize(), 'f')}-E{_edge_label(settings.ev_grace_edge_target)}-L{settings.ev_grace_latency_ms}"
            self.variants.append(
                CorrectedFrontierHedgeVariant(
                    settings,
                    recorder,
                    regime_tracker,
                    edge_target=settings.ev_grace_edge_target,
                    completion_latency_ms=settings.ev_grace_latency_ms,
                    grace_ms=grace,
                    fixed_size=grace_size,
                    strategy_name=name,
                )
            )

        for size in settings.ev_size_candidates:
            name = f"EV-S{format(size.normalize(), 'f')}-G{settings.ev_size_grace_ms}-E{_edge_label(settings.ev_size_edge_target)}-L{settings.ev_size_latency_ms}"
            self.variants.append(
                CorrectedFrontierHedgeVariant(
                    settings,
                    recorder,
                    regime_tracker,
                    edge_target=settings.ev_size_edge_target,
                    completion_latency_ms=settings.ev_size_latency_ms,
                    grace_ms=settings.ev_size_grace_ms,
                    fixed_size=size,
                    strategy_name=name,
                )
            )

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row.get("sample_status") == "OK"]
        return sorted(
            rows,
            key=lambda row: (row["modeled_ev_per_placement"], row["realized_ev_per_placement"]),
            reverse=True,
        )
