#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  cp .env.example "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example"
fi

set_key() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

# Fast small-size taker research.
set_key MIN_NET_EDGE_PER_SHARE 0.001
set_key MIN_EXPECTED_PROFIT_USDC 0.001
set_key MIN_TRADE_SHARES 1
set_key MAX_TRADE_SHARES 5
set_key SHADOW_LATENCY_MS 5
set_key SHADOW_RECOVERY_LATENCY_MS 10
set_key MARKET_COOLDOWN_MS 100
set_key MAX_BOOK_AGE_MS 250
set_key STRATEGY_TIMER_INTERVAL_MS 1

# Accelerated Dual-FOK research. Keeps SURGE and the 2x baseline depth gate.
set_key DUAL_FOK_ENABLED true
set_key DUAL_FOK_BASE_LATENCY_MS 2
set_key DUAL_FOK_RECOVERY_LATENCY_MS 10
set_key DUAL_FOK_COOLDOWN_MS 100
set_key DUAL_FOK_MAX_BOOK_AGE_MS 100
set_key DUAL_FOK_USE_SURGE_GATE true
set_key DUAL_FOK_PRIMARY_SIZE 1
set_key DUAL_FOK_PRIMARY_EDGE_TARGET 0.001
set_key DUAL_FOK_PRIMARY_SKEW_MS 2
set_key DUAL_FOK_PRIMARY_COVERAGE_MULTIPLE 2
set_key DUAL_FOK_PRIMARY_STABILITY_MS 0
set_key DUAL_FOK_SKEWS_MS 0,1,2,5,10,25
set_key DUAL_FOK_SIZE_CANDIDATES 1,2,5,10
set_key DUAL_FOK_EDGE_TARGETS 0.001,0.002,0.003,0.005,0.010
set_key DUAL_FOK_COVERAGE_MULTIPLES 1,2,5
set_key DUAL_FOK_STABILITY_PERIODS_MS 0,2,5,10,25,50
set_key REVERSE_DUAL_FOK_ENABLED true
set_key REVERSE_DUAL_FOK_SKEWS_MS 0,1,2,5,10,25

# Ideal atomic ceiling: never contributes to shadow equity.
set_key ATOMIC_BENCHMARK_ENABLED true
set_key ATOMIC_REVERSE_ENABLED true
set_key ATOMIC_SIZES 1,5,10,20
set_key ATOMIC_MIN_NET_EDGE_PER_SHARE 0.0001
set_key ATOMIC_EDGE_BANDS 0.001,0.002,0.003,0.005,0.010
set_key ATOMIC_MAX_BOOK_AGE_MS 100

# Selective paired-maker queue controls.
set_key PAIRED_MAKER_ENABLED true
set_key PAIRED_MAKER_TRADE_SHARES 1
set_key PAIRED_MAKER_TARGET_PAIR 0.99
set_key PAIRED_MAKER_MIN_GROSS_EDGE_PER_SHARE 0.005
set_key PAIRED_MAKER_MAX_QUEUES 25,50,100,250
set_key PAIRED_MAKER_MAX_QUEUE_IMBALANCE 4

echo "Applied Phase 1.7 fast-arbitrage + atomic/paired-maker research profile to $ENV_FILE"
echo
grep -E '^(MIN_NET_EDGE_PER_SHARE|MIN_TRADE_SHARES|SHADOW_LATENCY_MS|STRATEGY_TIMER_INTERVAL_MS|DUAL_FOK_BASE_LATENCY_MS|DUAL_FOK_PRIMARY_SIZE|DUAL_FOK_PRIMARY_EDGE_TARGET|DUAL_FOK_SKEWS_MS|ATOMIC_BENCHMARK_ENABLED|ATOMIC_SIZES|PAIRED_MAKER_ENABLED|PAIRED_MAKER_MAX_QUEUES)=' "$ENV_FILE"
