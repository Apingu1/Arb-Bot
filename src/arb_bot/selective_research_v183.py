from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .maker_research import ZERO, VariantCampaign, _quote_payload, _utc_now
from .models import MarketPair
from .research_context_v183 import PHASE183_RUN_ID
from .selective_research_v182 import (
    SelectiveHybridVariantV182,
    SelectiveMakerVariantV182,
    SelectivePairedMakerVariantV182,
)
from .strategy import ArbitrageEngine


GHOST_EDGE_THRESHOLDS = (
    Decimal("0"),
    Decimal("-0.005"),
    Decimal("-0.010"),
    Decimal("-0.015"),
    Decimal("-0.020"),
)
GHOST_CANCEL_LATENCIES_MS = (5, 10, 25, 50)


def _threshold_key(value: Decimal) -> str:
    return format(value, "f")


class CounterfactualTelemetryMixin:
    """Phase 1.8.3 observational decision telemetry.

    No trading rule is changed. The live shadow strategy continues to make the
    same placement/completion/unwind decisions as Phase 1.8.2 while this mixin
    records what other actions *would* have done around the first maker fill.
    """

    def _telemetry_setup(self) -> None:
        super()._telemetry_setup()
        self._first_fill_counterfactual: dict[str, dict[str, Any]] = {}
        self._ghost_prefill_gates: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}

    def _observe_ghost_prefill_gates(
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

        now = time.monotonic()
        age_ms = Decimal(str(max(0.0, (now - campaign.placed_at) * 1000)))
        side_context = {
            "A": self._maker_plus_taker_edge(campaign.leg_a.price, book_b, campaign.shares),
            "B": self._maker_plus_taker_edge(campaign.leg_b.price, book_a, campaign.shares),
        }
        market_gates = self._ghost_prefill_gates.setdefault(
            campaign.market_id, {"A": {}, "B": {}}
        )

        for side, context in side_context.items():
            edge = context.get("edge_per_share")
            if edge is None:
                continue
            edge_value = Decimal(str(edge))
            for threshold in GHOST_EDGE_THRESHOLDS:
                key = _threshold_key(threshold)
                if key in market_gates[side] or edge_value > threshold:
                    continue
                trigger = {
                    "triggered_at_monotonic": now,
                    "triggered_at": _utc_now(),
                    "quote_age_ms": age_ms,
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
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        **{k: v for k, v in trigger.items() if k != "triggered_at_monotonic"},
                    },
                )

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge) -> None:
        pair = engine.pairs.get(market_id)
        campaign = self.campaigns.get(market_id)
        if pair is not None and campaign is not None and not campaign.any_fill:
            self._observe_ghost_prefill_gates(engine, pair, campaign)

        super().on_market_update(engine, market_id, surge)

        # A campaign may have been created by the call above. Capture the
        # initial ghost-gate state immediately rather than waiting for another
        # exchange message.
        campaign = self.campaigns.get(market_id)
        if pair is not None and campaign is not None and not campaign.any_fill:
            self._observe_ghost_prefill_gates(engine, pair, campaign)

    def process_due(self, engine: ArbitrageEngine) -> None:
        # main.py runs strategy timers immediately after applying each exchange
        # message, before on_market_update. Sampling here therefore preserves the
        # pre-fill book state even when that same message consumes our queue.
        for market_id, campaign in list(self.campaigns.items()):
            if campaign.any_fill:
                continue
            pair = engine.pairs.get(market_id)
            if pair is not None:
                self._observe_ghost_prefill_gates(engine, pair, campaign)
        super().process_due(engine)

    def _capture_first_fill_counterfactual(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        if campaign.market_id in self._first_fill_counterfactual:
            return

        snapshot = self._first_fill_snapshots.get(campaign.market_id, {})
        first_side = snapshot.get("first_fill_side") if isinstance(snapshot, dict) else None
        matched_pnl = self._matched_profit(campaign)
        exposure_leg, exposure_qty, exposure_avg = self._exposure_cost(campaign)

        completion = self._current_completion_sample(engine, pair, campaign)
        completion_net = completion.get("complete_now_net_profit") if completion else None
        if exposure_qty <= ZERO:
            complete_total = matched_pnl - campaign.taker_fees
        elif completion_net is not None:
            complete_total = matched_pnl + Decimal(str(completion_net)) - campaign.taker_fees
        else:
            complete_total = None

        unwind_quote, unwind_fee = self._unwind_quote(engine, pair, campaign)
        if exposure_qty <= ZERO:
            unwind_total = matched_pnl - campaign.taker_fees
        elif unwind_quote is not None:
            exposure_unwind = (
                unwind_quote.notional
                - exposure_qty * exposure_avg
                - unwind_fee
            )
            unwind_total = matched_pnl + exposure_unwind - campaign.taker_fees
        else:
            unwind_total = matched_pnl - exposure_qty * exposure_avg - campaign.taker_fees

        record = {
            "phase183_run_id": PHASE183_RUN_ID,
            "captured_at": _utc_now(),
            "first_fill_side": first_side,
            "first_fill_ms": snapshot.get("first_fill_ms") if isinstance(snapshot, dict) else None,
            "edge_at_fill": (
                snapshot.get("complete_now_net_edge_per_share")
                if isinstance(snapshot, dict)
                else None
            ),
            "matched_qty_at_first_fill": campaign.matched_qty,
            "matched_pnl_at_first_fill": matched_pnl,
            "exposure_leg": exposure_leg,
            "exposure_qty": exposure_qty,
            "exposure_average_cost": exposure_avg,
            "complete_now_total_pnl": complete_total,
            "complete_now_context": completion,
            "unwind_now_total_pnl": unwind_total,
            "unwind_now_quote": _quote_payload(unwind_quote),
            "unwind_now_taker_fee": unwind_fee,
            "second_maker_fill_seen": False,
            "second_maker_fill_qty": ZERO,
            "time_to_second_maker_fill_ms": None,
        }
        self._first_fill_counterfactual[campaign.market_id] = record
        if isinstance(snapshot, dict):
            snapshot["phase183_run_id"] = PHASE183_RUN_ID
            snapshot["counterfactual_complete_now_total_pnl"] = complete_total
            snapshot["counterfactual_unwind_now_total_pnl"] = unwind_total

        self.recorder.write(
            "maker_variant_first_fill_choices_v183",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                **record,
            },
        )

    def _observe_second_maker_fill(
        self,
        campaign: VariantCampaign,
        *,
        before_a: Decimal,
        before_b: Decimal,
    ) -> None:
        record = self._first_fill_counterfactual.get(campaign.market_id)
        if record is None or record.get("second_maker_fill_seen"):
            return
        first_side = record.get("first_fill_side")
        second_qty = ZERO
        if first_side == "A" and campaign.leg_b.filled_qty > before_b:
            second_qty = campaign.leg_b.filled_qty - before_b
        elif first_side == "B" and campaign.leg_a.filled_qty > before_a:
            second_qty = campaign.leg_a.filled_qty - before_a
        elif first_side == "BOTH_SAME_UPDATE" and (
            campaign.leg_a.filled_qty > before_a and campaign.leg_b.filled_qty > before_b
        ):
            second_qty = min(
                campaign.leg_a.filled_qty - before_a,
                campaign.leg_b.filled_qty - before_b,
            )

        if second_qty <= ZERO:
            return
        elapsed = Decimal("0")
        if campaign.first_fill_at is not None:
            elapsed = Decimal(str(max(0.0, (time.monotonic() - campaign.first_fill_at) * 1000)))
        record["second_maker_fill_seen"] = True
        record["second_maker_fill_qty"] = second_qty
        record["time_to_second_maker_fill_ms"] = elapsed
        self.recorder.write(
            "maker_variant_second_maker_fill_v183",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "first_fill_side": first_side,
                "second_maker_fill_qty": second_qty,
                "time_to_second_maker_fill_ms": elapsed,
                "observed_at": _utc_now(),
            },
        )

    def _consume_new_trades(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        before_first = campaign.first_fill_at
        before_a = campaign.leg_a.filled_qty
        before_b = campaign.leg_b.filled_qty
        super()._consume_new_trades(engine, pair, campaign)

        if before_first is None and campaign.first_fill_at is not None:
            self._capture_first_fill_counterfactual(engine, pair, campaign)
        if campaign.first_fill_at is not None:
            self._observe_second_maker_fill(
                campaign,
                before_a=before_a,
                before_b=before_b,
            )

    def _ghost_gate_result(
        self,
        campaign: VariantCampaign,
        actual_pnl: Decimal,
        first_side: str | None,
    ) -> dict[str, Any]:
        market_gates = self._ghost_prefill_gates.get(campaign.market_id, {"A": {}, "B": {}})
        output: dict[str, Any] = {}
        first_fill_at = campaign.first_fill_at

        for threshold in GHOST_EDGE_THRESHOLDS:
            key = _threshold_key(threshold)
            side_trigger = (
                market_gates.get(first_side, {}).get(key)
                if first_side in {"A", "B"}
                else None
            )
            candidates = [
                trigger
                for side in ("A", "B")
                for trigger in [market_gates.get(side, {}).get(key)]
                if trigger is not None
            ]
            any_trigger = min(
                candidates,
                key=lambda item: item["triggered_at_monotonic"],
                default=None,
            )

            threshold_result: dict[str, Any] = {}
            for scope, trigger in (
                ("ACTUAL_FIRST_SIDE", side_trigger),
                ("ANY_SIDE", any_trigger),
            ):
                trigger_at = trigger.get("triggered_at_monotonic") if trigger else None
                lead_ms = None
                if first_fill_at is not None and trigger_at is not None:
                    lead_ms = Decimal(str((first_fill_at - trigger_at) * 1000))
                latency_rows: dict[str, Any] = {}
                for latency in GHOST_CANCEL_LATENCIES_MS:
                    avoided = lead_ms is not None and lead_ms >= Decimal(latency)
                    counterfactual = ZERO if avoided else actual_pnl
                    latency_rows[str(latency)] = {
                        "would_avoid_first_fill": avoided,
                        "counterfactual_pnl": counterfactual,
                        "delta_vs_actual": counterfactual - actual_pnl,
                    }
                threshold_result[scope] = {
                    "triggered": trigger is not None,
                    "trigger_side": trigger.get("side") if trigger else None,
                    "trigger_quote_age_ms": trigger.get("quote_age_ms") if trigger else None,
                    "trigger_edge_per_share": trigger.get("edge_per_share") if trigger else None,
                    "lead_to_first_fill_ms": lead_ms,
                    "latencies": latency_rows,
                }
            output[key] = threshold_result
        return output

    def _cancel(self, campaign: VariantCampaign, reason: str) -> None:
        # No first fill means there is no realised decision counterfactual to
        # compare. The trigger events remain in JSONL for placement diagnostics.
        if not campaign.any_fill:
            self._first_fill_counterfactual.pop(campaign.market_id, None)
            self._ghost_prefill_gates.pop(campaign.market_id, None)
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
        enriched = dict(extra)
        snapshot = self._first_fill_snapshots.get(campaign.market_id, {})
        first_side = snapshot.get("first_fill_side") if isinstance(snapshot, dict) else None
        counterfactual = self._first_fill_counterfactual.pop(campaign.market_id, None)
        gates = self._ghost_gate_result(campaign, pnl, first_side)
        self._ghost_prefill_gates.pop(campaign.market_id, None)

        enriched["phase183_instrumented"] = True
        enriched["phase183_run_id"] = PHASE183_RUN_ID
        enriched["ghost_prefill_gate_results_v183"] = gates

        if counterfactual is not None:
            counterfactual["actual_eventual_pnl"] = pnl
            counterfactual["actual_eventual_status"] = status
            counterfactual["actual_eventual_action"] = action
            candidates = {
                "COMPLETE_NOW": counterfactual.get("complete_now_total_pnl"),
                "UNWIND_NOW": counterfactual.get("unwind_now_total_pnl"),
                "ACTUAL_POLICY": pnl,
            }
            valid = {
                name: Decimal(str(value))
                for name, value in candidates.items()
                if value is not None
            }
            best_action = max(valid, key=valid.get) if valid else "ACTUAL_POLICY"
            best_pnl = valid.get(best_action, pnl)
            counterfactual["best_action"] = best_action
            counterfactual["best_pnl"] = best_pnl
            counterfactual["actual_policy_regret"] = best_pnl - pnl
            enriched["first_fill_counterfactual_v183"] = counterfactual

            self.recorder.write(
                "maker_variant_first_fill_counterfactual_v183",
                {
                    "phase183_run_id": PHASE183_RUN_ID,
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    **counterfactual,
                },
            )

        self.recorder.write(
            "maker_variant_ghost_prefill_gate_result_v183",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "first_fill_side": first_side,
                "actual_pnl": pnl,
                "status": status,
                "results": gates,
            },
        )
        super()._finalize(campaign, pnl, status=status, action=action, extra=enriched)


class SelectiveHybridVariantV183(CounterfactualTelemetryMixin, SelectiveHybridVariantV182):
    pass


class SelectivePairedMakerVariantV183(
    CounterfactualTelemetryMixin, SelectivePairedMakerVariantV182
):
    pass


class SelectiveMakerVariantV183(CounterfactualTelemetryMixin, SelectiveMakerVariantV182):
    pass
