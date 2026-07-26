# Merge Work Log — Algo V3
## Task: Merge updated Strike Selector + new Exit Engine into existing project

### What we are merging
1. `strike_selector.py` — updated with `ShortStrangleHedgedSelector` (15-delta default, closest-higher rule, exchange-listed strikes only, alias kept)
2. `exit_engine/` — brand new SL/TP engine (models, interfaces, policies, triggers, actions, decision_manager, engine, audit, config)
3. All existing files — carry forward unchanged unless they need wiring updates

### Integration points (where exit engine connects to existing code)
- `strategy.py` (IronFlyState.on_tick) → replace hardcoded SL/TP logic with exit_engine call
- `strangle_strategy.py` (StrangleState.on_tick) → replace hardcoded SL/TP logic with exit_engine call
- `backtest_engine.py` → pass TradeContext to exit engine each tick instead of calling on_tick directly
- `trading_engine.py` → wire exit engine construction with correct YAML policy per strategy/timeframe
- `config.py` → add STRANGLE_SHORT_DELTA default = 15 (updated)

### Files to produce
| # | File | Status | Notes |
|---|------|--------|-------|
| 1 | exit_engine/__init__.py | ✅ DONE | |
| 2 | exit_engine/models.py | ✅ DONE | |
| 3 | exit_engine/interfaces.py | ✅ DONE | |
| 4 | exit_engine/triggers.py | ✅ DONE | |
| 5 | exit_engine/actions.py | ✅ DONE | |
| 6 | exit_engine/audit.py | ✅ DONE | |
| 7 | exit_engine/policies.py | ✅ DONE | |
| 8 | exit_engine/decision_manager.py | ✅ DONE | |
| 9 | exit_engine/engine.py | ✅ DONE | |
| 10 | exit_engine/config.py | ✅ DONE | |
| 11 | strike_selector.py | ✅ DONE | 15-delta default, closest-higher, exchange-listed only |
| 12 | strategy.py | ✅ DONE | IronFlyState wired to exit_engine |
| 13 | strangle_strategy.py | ✅ DONE | StrangleState wired to exit_engine |
| 14 | backtest_engine.py | ✅ DONE | TradeContext wiring |
| 15 | trading_engine.py | ✅ DONE | Exit engine construction per strategy/timeframe |
| 16 | config.py | ✅ DONE | STRANGLE_SHORT_DELTA default=15, exit policy paths |
| 17 | config/iron_fly_exit_policy.yaml | ✅ DONE | |
| 18 | config/strangle_intraday_exit_policy.yaml | ✅ DONE | |
| 19 | config/strangle_weekly_exit_policy.yaml | ✅ DONE | |
| 20 | config/strangle_monthly_exit_policy.yaml | ✅ DONE | |
| 21 | greeks_engine.py | ✅ DONE | unchanged copy |
| 22 | adjustment_engine.py | ✅ DONE | unchanged copy |
| 23 | strangle_data_fetcher.py | ✅ DONE | unchanged copy |
| 24 | data_fetcher.py | ✅ DONE | unchanged copy |
| 25 | data_cache.py | ✅ DONE | unchanged copy |
| 26 | upstox_client.py | ✅ DONE | unchanged copy |
| 27 | live_data_source.py | ✅ DONE | unchanged copy |
| 28 | event_calendar.py | ✅ DONE | unchanged copy |
| 29 | pnl_format.py | ✅ DONE | unchanged copy |
| 30 | menu.py | ✅ DONE | unchanged copy |
| 31 | run_backtest.py | ✅ DONE | unchanged copy |
| 32 | run_swing_backtest.py | ✅ DONE | unchanged copy |
| 33 | check_setup.py | ✅ DONE | unchanged copy |
| 34 | .env.example | ✅ DONE | add exit policy paths |
| 35 | requirements.txt | ✅ DONE | add pyyaml |
| 36 | config/event_calendar/india_fixed_events.yaml | ✅ DONE | |

### Key design decisions made
- Exit engine is the SOLE authority for SL/TP decisions in both strategies
- IronFlyState and StrangleState retain their leg-tracking/P&L accounting role but delegate exit decisions to the engine
- Trailing SL lives in exit_engine/policies.py (TradeLevelExitPolicy), not in strategy.py anymore
- Per-leg SL for the Iron Fly is a LegLevelExitPolicy in the YAML config (excluded_leg_names: [ce_otm7, pe_otm7] for hedge legs)
- Each strategy×timeframe combo has its own YAML policy file — fully config-driven, no hardcoded thresholds
- ShortStrangleHedgedSelector default delta = 15, HedgedDeltaStrangleSelector alias kept for backward compat
