from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from typing import Any

from .maker_research import ZERO, VariantCampaign, _utc_now
from .models import MarketPair
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .selective_research_v183 import (
    GHOST_EDGE_THRESHOLDS,
    SelectiveHybridVariantV183,
    SelectiveMakerVariantV183,
    SelectivePairedMakerVariantV183,
    _threshold_key,
)
from .strategy import ArbitrageEngine


PREFILL_LOOKBACK_MS = (500, 250, 100, 50, 25, 10)


class FastAdverseSelectionMixin:
    """Phase 1.8.4 executable-in-shadow pre-fill protection.

    The Phase 1.8.3 ghost study proved that edge and quote age should be treated
    jointly. This mixin keeps the existing observational telemetry but adds an
    actual *shadow* cancellation race. A cancellation is only credited when the
    configured cancellation latency elapses before the first simulated maker
    fill. This prevents hindsight from turning toxic fills into impossible zero
    P&L outcomes.
    """

    def _telemetry_setup(self) -> None:
        super()._telemetry_setup()
        self._v184_prefill_history: dict[str, dict[str, deque[dict[str, Any]]]] = {}
        self._v184_prefill_cache_key: dict[str, tuple[float, float, float, float]] = {}
        self._v184_pending_cancel: dict[str, dict[str, Any]] = {}
        self._v184_prefill_timeline: dict[str, dict[str, Any]] = {}
        self.v184_cancel_intents = 0
        self.v184_cancel_effective = 0
        self.v184_cancel_race_lost = 0

    def _v184_history(self, market_id: str) -> dict[str, deque[dict[str, Any]]]:
        history = self._v184_prefill_history.get(market_id)
        if history is None:
            maxlen = max(32, int(self.settings.v184_prefill_history_max_samples))
            history = {
                "A": deque(maxlen=maxlen),
                "B": deque(maxlen=maxlen),
            }
            self._v184_prefill_history[market_id] = history
        return history

    def _v184_arm_cancel(
        self,
        campaign: VariantCampaign,
        *,
        now: float,
        reason: str,
        trigger_side: str | None,
        trigger_edge: Decimal | None,
        quote_age_ms: Decimal,
        book_age_a_ms: Decimal,
        book_age_b_ms: Decimal,
    ) -> None:
        if not self.settings.v184_fast_cancel_enabled:
            return
        if campaign.market_id in self._v184_pending_cancel:
            return

        latency_ms = max(0, int(self.settings.v184_cancel_latency_ms))
        pending = {
            "armed_at_monotonic": now,
            "armed_at": _utc_now(),
            "effective_at_monotonic": now + latency_ms / 1000,
            "configured_latency_ms": latency_ms,
            "reason": reason,
            "trigger_side": trigger_side,
            "trigger_edge_per_share": trigger_edge,
            "trigger_quote_age_ms": quote_age_ms,
            "book_age_a_ms": book_age_a_ms,
            "book_age_b_ms": book_age_b_ms,
        }
        self._v184_pending_cancel[campaign.market_id] = pending
        self.v184_cancel_intents += 1
        self.recorder.write(
            "maker_variant_fast_cancel_intent_v184",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "phase184_run_id": PHASE184_RUN_ID,
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                **{k: v for k, v in pending.items() if not k.endswith("_monotonic")},
            },
        )

    def _v184_maybe_execute_cancel(self, campaign: VariantCampaign) -> bool:
        pending = self._v184_pending_cancel.get(campaign.market_id)
        if pending is None or campaign.any_fill:
            return False
        now = time.monotonic()
        if now < float(pending["effective_at_monotonic"]):
            return False

        actual_latency_ms = Decimal(
            str(max(0.0, (now - float(pending["armed_at_monotonic"])) * 1000))
        )
        self.v184_cancel_effective += 1
        self.recorder.write(
            "maker_variant_fast_cancel_effective_v184",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "phase184_run_id": PHASE184_RUN_ID,
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "reason": pending.get("reason"),
                "trigger_side": pending.get("trigger_side"),
                "trigger_edge_per_share": pending.get("trigger_edge_per_share"),
                "trigger_quote_age_ms": pending.get("trigger_quote_age_ms"),
                "configured_latency_ms": pending.get("configured_latency_ms"),
                "actual_cancel_latency_ms": actual_latency_ms,
                "cancelled_at": _utc_now(),
            },
        )
        self._cancel(campaign, f"V184_{pending.get('reason') or 'FAST_CANCEL'}")
        return True

    def _v184_sample_prefill(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        if campaign.any_fill:
            return
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        if book_a is None or book_b is None or not book_a.ready or not book_b.ready:
            return

        cache_key = (
            float(book_a.updated_monotonic),
            float(book_b.updated_monotonic),
            float(book_a.last_trade_monotonic),
            float(book_b.last_trade_monotonic),
        )
        if self._v184_prefill_cache_key.get(campaign.market_id) == cache_key:
            return
        self._v184_prefill_cache_key[campaign.market_id] = cache_key

        now = time.monotonic()
        quote_age_ms = Decimal(str(max(0.0, (now - campaign.placed_at) * 1000)))
        book_age_a_ms = Decimal(str(max(0.0, (now - book_a.updated_monotonic) * 1000)))
        book_age_b_ms = Decimal(str(max(0.0, (now - book_b.updated_monotonic) * 1000)))
        side_context = {
            "A": self._maker_plus_taker_edge(campaign.leg_a.price, book_b, campaign.shares),
            "B": self._maker_plus_taker_edge(campaign.leg_b.price, book_a, campaign.shares),
        }
        history = self._v184_history(campaign.market_id)
        market_gates = self._ghost_prefill_gates.setdefault(
            campaign.market_id, {"A": {}, "B": {}}
        )

        valid_edges: list[tuple[str, Decimal]] = []
        for side, context in side_context.items():
            edge = context.get("edge_per_share")
            edge_value = Decimal(str(edge)) if edge is not None else None
            if edge_value is not None:
                valid_edges.append((side, edge_value))
            history[side].append(
                {
                    "sampled_at_monotonic": now,
                    "sampled_at": _utc_now(),
                    "quote_age_ms": quote_age_ms,
                    "edge_per_share": edge_value,
                    "net_profit": context.get("net_profit"),
                    "opposite_quote": context.get("opposite_quote"),
                    "taker_fee": context.get("taker_fee"),
                    "book_age_a_ms": book_age_a_ms,
                    "book_age_b_ms": book_age_b_ms,
                }
            )

            # Preserve the accepted Phase 1.8.3 ghost gate schema, but reuse
            # this cached edge calculation rather than quoting both books again.
            if edge_value is None:
                continue
            for threshold in GHOST_EDGE_THRESHOLDS:
                key = _threshold_key(threshold)
                if key in market_gates[side] or edge_value > threshold:
                    continue
                trigger = {
                    "triggered_at_monotonic": now,
                    "triggered_at": _utc_now(),
                    "quote_age_ms": quote_age_ms,
                    "side": side,
                    "threshold": threshold,
                    "edge_per_share": edge_value,
                    "net_profit": context.get("net_profit"),
                    "opposite_quote": context.get("opposite_quote"),
                    "taker_fee": context.get("taker_fee"),
                }
                market_gates[side][key] = trigger
                self.recorder.write(
                    "maker_variant_ghost_prefill_gate_trigger_v183",
                    {
                        "phase183_run_id": PHASE183_RUN_ID,
                        "phase184_run_id": PHASE184_RUN_ID,
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        **{k: v for k, v in trigger.items() if k != "triggered_at_monotonic"},
                    },
                )

        if not valid_edges or campaign.market_id in self._v184_pending_cancel:
            return

        trigger_side, worst_edge = min(valid_edges, key=lambda item: item[1])
        reason: str | None = None
        if worst_edge <= self.settings.v184_prefill_cancel_edge_per_share:
            reason = "TOXIC_EDGE"
        elif (
            quote_age_ms >= Decimal(self.settings.v184_stale_quote_age_ms)
            and worst_edge <= self.settings.v184_stale_max_edge_per_share
        ):
            reason = "STALE_NONPOSITIVE_EDGE"
        elif quote_age_ms >= Decimal(self.settings.v184_hard_quote_age_ms):
            reason = "HARD_QUOTE_AGE"
        elif (
            max(book_age_a_ms, book_age_b_ms)
            >= Decimal(self.settings.v184_max_opposite_book_age_ms)
            and worst_edge <= self.settings.v184_stale_max_edge_per_share
        ):
            reason = "STALE_BOOK_NONPOSITIVE_EDGE"

        if reason is not None:
            self._v184_arm_cancel(
                campaign,
                now=now,
                reason=reason,
                trigger_side=trigger_side,
                trigger_edge=worst_edge,
                quote_age_ms=quote_age_ms,
                book_age_a_ms=book_age_a_ms,
                book_age_b_ms=book_age_b_ms,
            )

    def _observe_ghost_prefill_gates(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        # Phase 1.8.3 calls this from both the event path and timer path. The
        # cache key above makes those calls cheap when the underlying books have
        # not changed.
        self._v184_sample_prefill(engine, pair, campaign)

    def process_due(self, engine: ArbitrageEngine) -> None:
        # Cancellation deadlines are checked before the inherited fill model.
        # Therefore a 5 ms cancel only wins if five real monotonic milliseconds
        # have elapsed before the queue-fill event is consumed.
        for market_id in list(self.campaigns):
            campaign = self.campaigns.get(market_id)
            if campaign is None or campaign.any_fill:
                continue
            if self._v184_maybe_execute_cancel(campaign):
                continue
            pair = engine.pairs.get(market_id)
            if pair is not None:
                self._v184_sample_prefill(engine, pair, campaign)
        super().process_due(engine)

    def _v184_emit_prefill_timeline(self, campaign: VariantCampaign) -> None:
        if campaign.market_id in self._v184_prefill_timeline:
            return
        snapshot = self._first_fill_snapshots.get(campaign.market_id, {})
        first_side = snapshot.get("first_fill_side") if isinstance(snapshot, dict) else None
        if first_side not in {"A", "B"} or campaign.first_fill_at is None:
            return

        history = list(self._v184_prefill_history.get(campaign.market_id, {}).get(first_side, ()))
        rows: dict[str, Any] = {}
        for lookback_ms in PREFILL_LOOKBACK_MS:
            target = campaign.first_fill_at - lookback_ms / 1000
            eligible = [row for row in history if float(row["sampled_at_monotonic"]) <= target]
            if not eligible:
                rows[str(lookback_ms)] = None
                continue
            chosen = eligible[-1]
            sample_gap_ms = Decimal(
                str(max(0.0, (target - float(chosen["sampled_at_monotonic"])) * 1000))
            )
            rows[str(lookback_ms)] = {
                "edge_per_share": chosen.get("edge_per_share"),
                "quote_age_ms": chosen.get("quote_age_ms"),
                "book_age_a_ms": chosen.get("book_age_a_ms"),
                "book_age_b_ms": chosen.get("book_age_b_ms"),
                "sample_gap_before_target_ms": sample_gap_ms,
            }

        payload = {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "strategy": self.strategy_name,
            "market_id": campaign.market_id,
            "slug": campaign.slug,
            "first_fill_side": first_side,
            "first_fill_ms": snapshot.get("first_fill_ms") if isinstance(snapshot, dict) else None,
            "edge_at_fill": (
                snapshot.get("complete_now_net_edge_per_share")
                if isinstance(snapshot, dict)
                else None
            ),
            "lookback_samples": rows,
            "captured_at": _utc_now(),
        }
        self._v184_prefill_timeline[campaign.market_id] = payload
        self.recorder.write("maker_variant_prefill_timeline_v184", payload)

    def _consume_new_trades(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        before_first = campaign.first_fill_at
        pending = self._v184_pending_cancel.get(campaign.market_id)
        super()._consume_new_trades(engine, pair, campaign)

        if before_first is None and campaign.first_fill_at is not None:
            self._v184_emit_prefill_timeline(campaign)
            if pending is not None:
                now = time.monotonic()
                lead_ms = Decimal(
                    str(
                        max(
                            0.0,
                            (float(pending["effective_at_monotonic"]) - campaign.first_fill_at)
                            * 1000,
                        )
                    )
                )
                self.v184_cancel_race_lost += 1
                self.recorder.write(
                    "maker_variant_fast_cancel_race_lost_v184",
                    {
                        "phase183_run_id": PHASE183_RUN_ID,
                        "phase184_run_id": PHASE184_RUN_ID,
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        "reason": pending.get("reason"),
                        "trigger_side": pending.get("trigger_side"),
                        "trigger_edge_per_share": pending.get("trigger_edge_per_share"),
                        "trigger_quote_age_ms": pending.get("trigger_quote_age_ms"),
                        "configured_latency_ms": pending.get("configured_latency_ms"),
                        "cancel_deadline_after_fill_ms": lead_ms,
                        "first_fill_at": _utc_now(),
                    },
                )
                self._v184_pending_cancel.pop(campaign.market_id, None)

    def _cancel(self, campaign: VariantCampaign, reason: str) -> None:
        self._v184_prefill_history.pop(campaign.market_id, None)
        self._v184_prefill_cache_key.pop(campaign.market_id, None)
        self._v184_pending_cancel.pop(campaign.market_id, None)
        self._v184_prefill_timeline.pop(campaign.market_id, None)
        super()._cancel(campaign, reason)

    def _finalize(
        self,
        campaign: VariantCampaign,
        pnl: Decimal,
        *,
        status: str,
        action: str,
        extra: dict[str, Any],
    ) -> None:
        timeline = self._v184_prefill_timeline.get(campaign.market_id)
        enriched = dict(extra)
        enriched["phase184_instrumented"] = True
        enriched["phase184_run_id"] = PHASE184_RUN_ID
        if timeline is not None:
            enriched["prefill_timeline_v184"] = timeline
            self.recorder.write(
                "maker_variant_prefill_timeline_outcome_v184",
                {
                    **timeline,
                    "actual_pnl": pnl,
                    "actual_status": status,
                    "actual_action": action,
                    "finalized_at": _utc_now(),
                },
            )

        self._v184_prefill_history.pop(campaign.market_id, None)
        self._v184_prefill_cache_key.pop(campaign.market_id, None)
        self._v184_pending_cancel.pop(campaign.market_id, None)
        self._v184_prefill_timeline.pop(campaign.market_id, None)
        super()._finalize(campaign, pnl, status=status, action=action, extra=enriched)

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "phase184_fast_cancel": True,
                "v184_cancel_intents": self.v184_cancel_intents,
                "v184_cancel_effective": self.v184_cancel_effective,
                "v184_cancel_race_lost": self.v184_cancel_race_lost,
                "v184_pending_cancels": len(self._v184_pending_cancel),
            }
        )
        return row


class SelectiveHybridVariantV184(FastAdverseSelectionMixin, SelectiveHybridVariantV183):
    pass


class SelectivePairedMakerVariantV184(
    FastAdverseSelectionMixin, SelectivePairedMakerVariantV183
):
    pass


class SelectiveMakerVariantV184(FastAdverseSelectionMixin, SelectiveMakerVariantV183):
    pass
