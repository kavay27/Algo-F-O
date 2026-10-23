"""
live_trader.py
==============
Multi-strategy live / paper execution bot supporting NSE (NIFTY) and BSE (SENSEX).

* PAPER_TRADE = True  -> real Breeze quotes, simulated fills (place_order is NEVER called).
* PAPER_TRADE = False -> real market orders via Breeze.

Features:
    1. Multi-strategy registry (e.g. 1, 2, 3, 4, 5...) with custom entry/exit times, SL, etc.
    2. Runs NSE (NIFTY) and BSE (SENSEX) strategies together in a single trading session.
    3. State is persisted to `data/live_state_<date>_<strat>_<mode>.json` after every change.
    4. Safe interrupt / on-demand square-off utility:
       - Stop all running strategies: python live_trader.py --square-off all
       - Stop a specific strategy:   python live_trader.py --square-off 1

Usage
-----
    python live_trader.py --strategy 1,2,3 --paper
    python live_trader.py --strategy all --live
    python live_trader.py --square-off all
    python live_trader.py --square-off 2
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import config
from breeze_client import (IST, BreezeAPIError, BreezeClient, NoDataError,
                           expiry_candidates, is_holiday, now_ist, round_to_strike)

log = logging.getLogger("live_trader")


# =========================================================================== #
# State
# --------------------------------------------------------------------------- #
@dataclass
class LegState:
    right: str
    action: str                       # "buy" or "sell"
    strike: int
    expiry: str
    qty: int
    sl_percent: float = 0.25
    underlying: str = "NIFTY"
    option_exchange: str = "NFO"
    status: str = "PENDING"           # PENDING / OPEN / CLOSED
    entry_price: float = 0.0
    entry_time: str = ""
    sl_price: float = 0.0
    sl_moved_to_cost: bool = False
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    entry_order_id: str = ""
    exit_order_id: str = ""
    last_ltp: float = 0.0

    @property
    def tag(self) -> str:
        return "CE" if self.right == "call" else "PE"

    @property
    def pnl(self) -> float:
        if self.status == "PENDING":
            return 0.0
        ref = self.exit_price if self.status == "CLOSED" else self.last_ltp
        if self.action == "sell":
            return (self.entry_price - ref) * self.qty
        else:
            return (ref - self.entry_price) * self.qty

    def is_sl_hit(self, ltp: float) -> bool:
        if self.action == "sell":
            return ltp >= self.sl_price
        else:
            return ltp <= self.sl_price


@dataclass
class DayState:
    trade_date: str
    strategy_key: str
    mode: str
    underlying: str = "NIFTY"
    spot_exchange: str = "NSE"
    option_exchange: str = "NFO"
    spot: float = 0.0
    atm_strike: int = 0
    expiry: str = ""
    entered: bool = False
    completed: bool = False
    legs: dict = field(default_factory=dict)  # right -> LegState

    @property
    def path(self) -> Path:
        return config.DATA_DIR / f"live_state_{self.trade_date}_{self.strategy_key}_{self.mode}.json"

    def save(self) -> None:
        data = asdict(self)
        data["legs"] = {k: asdict(v) for k, v in self.legs.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    @classmethod
    def load_or_new(cls, trade_date: date, strategy_key: str, mode: str) -> "DayState":
        s = cls(trade_date.isoformat(), strategy_key, mode)
        if s.path.exists():
            data = json.loads(s.path.read_text())
            legs = {k: LegState(**v) for k, v in data.pop("legs", {}).items()}
            s = cls(**data)
            s.legs = legs
            log.info("Resumed state from %s", s.path.name)
        return s

    def open_legs(self) -> list[LegState]:
        return [l for l in self.legs.values() if l.status == "OPEN"]

    def total_pnl(self) -> float:
        return sum(l.pnl for l in self.legs.values())


# =========================================================================== #
# Executors
# =========================================================================== #
class PaperExecutor:
    """Simulates fills at the current LTP (+/- slippage). Never places orders."""

    name = "PAPER"

    def __init__(self, client: BreezeClient):
        self.client = client

    def execute(self, action: str, leg: LegState) -> tuple[float, str]:
        try:
            ltp = self.client.get_option_ltp(
                date.fromisoformat(leg.expiry), leg.right, leg.strike,
                stock_code=leg.underlying, exchange_code=leg.option_exchange
            )
        except Exception as exc:
            ltp = leg.last_ltp or leg.entry_price or 100.0
            log.warning("Could not fetch real-time LTP for paper order (%s). Using fallback LTP %.2f", exc, ltp)
        slip = config.PAPER_SLIPPAGE_POINTS
        px = (ltp + slip) if action == "buy" else (ltp - slip)
        oid = f"PAPER-{leg.underlying}-{leg.tag}-{action.upper()}-{now_ist():%H%M%S}"
        log.info("[PAPER] %s %s %s %s %s (%s) @ %.2f (LTP %.2f)",
                 action.upper(), leg.qty, leg.underlying, leg.strike, leg.tag, leg.option_exchange, px, ltp)
        return round(px, 2), oid


class LiveExecutor:
    """Real market orders through Breeze."""

    name = "LIVE"

    def __init__(self, client: BreezeClient):
        self.client = client

    def execute(self, action: str, leg: LegState) -> tuple[float, str]:
        expiry = date.fromisoformat(leg.expiry)
        oid = self.client.place_option_order(
            action, leg.right, leg.strike, expiry, leg.qty,
            stock_code=leg.underlying, exchange_code=leg.option_exchange
        )
        try:
            px = self.client.wait_for_fill(oid, exchange_code=leg.option_exchange)
        except BreezeAPIError as exc:
            log.error("Could not confirm fill for %s (%s). Using LTP as estimate - "
                      "VERIFY IN BROKER TERMINAL.", oid, exc)
            px = self.client.get_option_ltp(
                expiry, leg.right, leg.strike,
                stock_code=leg.underlying, exchange_code=leg.option_exchange
            )
        log.info("[LIVE] %s %s %s %s %s (%s) filled @ %.2f (order %s)",
                 action.upper(), leg.qty, leg.underlying, leg.strike, leg.tag, leg.option_exchange, px, oid)
        return round(px, 2), oid


# =========================================================================== #
# Trader
# =========================================================================== #
class StraddleTrader:
    def __init__(self, client: BreezeClient, strategy_key: str, paper: bool):
        self.client = client
        self.strategy_key = strategy_key
        self.strat = config.STRATEGIES[strategy_key]
        self.executor = PaperExecutor(client) if paper else LiveExecutor(client)
        self.mode = self.executor.name
        self.today = now_ist().date()
        self.state = DayState.load_or_new(self.today, self.strategy_key, self.mode)

    # ------------------------------------------------------------------ #
    def _at(self, t) -> datetime:
        return IST.localize(datetime.combine(self.today, t))

    @staticmethod
    def _sleep_until(target: datetime) -> None:
        while True:
            remaining = (target - now_ist()).total_seconds()
            if remaining <= 0:
                return
            if remaining > 60:
                log.info("Waiting %.1f min until %s", remaining / 60, target.strftime("%H:%M:%S"))
            _time.sleep(min(remaining, 30))

    def _resolve_expiry(self, strike: int, right: str) -> date:
        """Nearest weekly expiry that actually has a live quote."""
        for exp in expiry_candidates(self.today, underlying=self.strat.underlying):
            try:
                self.client.get_option_ltp(
                    exp, right, strike,
                    stock_code=self.strat.underlying,
                    exchange_code=self.strat.option_exchange
                )
                return exp
            except (NoDataError, BreezeAPIError) as exc:
                log.warning("Expiry %s not tradable for %s %s %s on %s (%s); trying earlier",
                            exp, self.strat.underlying, strike, right, self.strat.option_exchange, exc)
        raise BreezeAPIError(f"Could not resolve tradable expiry for {self.strat.underlying} {strike} {right}")

    # ------------------------------------------------------------------ #
    def enter(self) -> None:
        s = self.state
        s.underlying = self.strat.underlying
        s.spot_exchange = self.strat.spot_exchange
        s.option_exchange = self.strat.option_exchange

        spot = self.client.get_spot_ltp(
            stock_code=self.strat.underlying,
            exchange_code=self.strat.spot_exchange
        )
        atm_strike = round_to_strike(spot, step=self.strat.strike_step)
        
        ce_strike = atm_strike + (self.strat.ce_strike_offset * self.strat.strike_step)
        pe_strike = atm_strike + (self.strat.pe_strike_offset * self.strat.strike_step)

        exp_ce = self._resolve_expiry(ce_strike, "call")
        exp_pe = self._resolve_expiry(pe_strike, "put")
        expiry = exp_ce

        s.spot, s.atm_strike, s.expiry = spot, atm_strike, expiry.isoformat()
        log.info("ENTRY | Strategy: %s (%s) | %s on %s | Spot=%.2f ATM=%d | CE %s (%s) | PE %s (%s) | Qty=%d",
                 self.strategy_key, self.strat.name, self.strat.underlying, self.strat.spot_exchange,
                 spot, atm_strike, ce_strike, self.strat.ce_action.upper(), pe_strike, self.strat.pe_action.upper(),
                 self.strat.quantity)

        s.legs["call"] = LegState("call", self.strat.ce_action, ce_strike, expiry.isoformat(),
                                  self.strat.quantity, self.strat.ce_sl_percent,
                                  underlying=self.strat.underlying, option_exchange=self.strat.option_exchange)
        s.legs["put"] = LegState("put", self.strat.pe_action, pe_strike, expiry.isoformat(),
                                 self.strat.quantity, self.strat.pe_sl_percent,
                                 underlying=self.strat.underlying, option_exchange=self.strat.option_exchange)
        s.save()

        filled: list[LegState] = []
        for leg in s.legs.values():
            try:
                px, oid = self.executor.execute(leg.action, leg)
            except Exception as exc:
                log.critical("Entry failed for %s: %s", leg.tag, exc)
                for f in filled:
                    self._exit_leg(f, "ENTRY_ABORT")
                s.completed = True
                s.save()
                raise
            leg.status = "OPEN"
            leg.entry_price = px
            leg.last_ltp = px
            leg.entry_time = now_ist().strftime("%H:%M:%S")
            leg.entry_order_id = oid
            
            # SL price calculation
            if leg.action == "sell":
                leg.sl_price = round(px * (1.0 + leg.sl_percent), 2)
            else:
                leg.sl_price = round(px * (1.0 - leg.sl_percent), 2)

            filled.append(leg)
            log.info("%s %s %s @ %.2f | SL %.2f", leg.underlying, leg.tag, leg.action.upper(), px, leg.sl_price)
            s.save()

        s.entered = True
        s.save()

    def _exit_leg(self, leg: LegState, reason: str) -> None:
        exit_action = "buy" if leg.action == "sell" else "sell"
        try:
            px, oid = self.executor.execute(exit_action, leg)
        except Exception as exc:
            log.critical("EXIT FAILED for %s (%s): %s - SQUARE OFF MANUALLY!", leg.tag, reason, exc)
            raise
        leg.status = "CLOSED"
        leg.exit_price = px
        leg.exit_order_id = oid
        leg.exit_time = now_ist().strftime("%H:%M:%S")
        leg.exit_reason = reason
        log.info("%s EXIT (%s) @ %.2f | leg P&L %.2f", leg.tag, reason, px, leg.pnl)
        self.state.save()

    # ------------------------------------------------------------------ #
    def monitor(self) -> None:
        s = self.state
        exit_at = self._at(self.strat.exit_time)
        last_status_log = 0.0

        while s.open_legs() and now_ist() < exit_at:
            stopped_now: list[LegState] = []
            for leg in s.open_legs():
                try:
                    ltp = self.client.get_option_ltp(
                        date.fromisoformat(leg.expiry),
                        leg.right, leg.strike,
                        stock_code=leg.underlying,
                        exchange_code=leg.option_exchange
                    )
                except BreezeAPIError as exc:
                    log.warning("LTP fetch failed for %s %s: %s", leg.underlying, leg.tag, exc)
                    continue
                leg.last_ltp = ltp
                if leg.is_sl_hit(ltp):
                    reason = "SL_COST" if leg.sl_moved_to_cost else "SL"
                    log.warning("%s %s SL HIT: LTP %.2f vs SL %.2f", leg.underlying, leg.tag, ltp, leg.sl_price)
                    try:
                        self._exit_leg(leg, reason)
                    except Exception:
                        continue  # leg stays OPEN; retried on next poll
                    stopped_now.append(leg)

            if self.strat.move_sl_to_cost and len(stopped_now) == 1:
                for other in s.open_legs():
                    if not other.sl_moved_to_cost:
                        other.sl_price = other.entry_price
                        other.sl_moved_to_cost = True
                        log.info("Cost-to-cost: %s %s SL moved to entry %.2f",
                                 other.underlying, other.tag, other.sl_price)
            s.save()

            if _time.monotonic() - last_status_log >= 60:
                last_status_log = _time.monotonic()
                log.info("STATUS | %s | MTM %.2f",
                         " | ".join(f"{l.underlying}_{l.tag} {l.status} ltp={l.last_ltp:.2f} sl={l.sl_price:.2f}"
                                    for l in s.legs.values()), s.total_pnl())

            _time.sleep(config.POLL_INTERVAL_SEC)

        for attempt in range(5):
            for leg in s.open_legs():
                try:
                    self._exit_leg(leg, "TIME")
                except Exception:
                    pass
            if not s.open_legs():
                break
            _time.sleep(2)

    # ------------------------------------------------------------------ #
    def finish(self) -> None:
        s = self.state
        s.completed = True
        s.save()
        pnl = s.total_pnl()
        log.info("DAY COMPLETE | %s | %s (%s) | net P&L (pre-charges) %.2f",
                 self.mode, self.strategy_key, self.strat.name, pnl)

        out = config.RESULTS_DIR / f"{self.mode.lower()}_trades.csv"
        new = not out.exists()
        with out.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "strategy_key", "strategy_name", "mode", "underlying", "exchange",
                            "spot", "atm_strike", "expiry", "leg", "action", "strike",
                            "entry_time", "entry", "sl_final", "exit_time", "exit",
                            "reason", "qty", "pnl"])
            for l in s.legs.values():
                w.writerow([s.trade_date, self.strategy_key, self.strat.name, self.mode,
                            self.strat.underlying, f"{self.strat.spot_exchange}/{self.strat.option_exchange}",
                            round(s.spot, 2), s.atm_strike, s.expiry,
                            l.tag, l.action.upper(), l.strike, l.entry_time, l.entry_price, l.sl_price, l.exit_time,
                            l.exit_price, l.exit_reason, l.qty, round(l.pnl, 2)])

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        s = self.state
        if s.completed:
            log.info("Today's trade for strategy %s already completed (%s). Nothing to do.",
                     self.strategy_key, s.path.name)
            return

        entry_at = self._at(self.strat.entry_time)
        exit_at = self._at(self.strat.exit_time)

        if not s.entered:
            now = now_ist()
            if now >= exit_at:
                log.info("Past exit time (%s); no trade today.", self.strat.exit_time)
                return
            if now > entry_at + timedelta(minutes=config.LATE_ENTRY_GRACE_MINUTES):
                log.warning("Started %.1f min after entry time %s (grace %d min). Skipping today.",
                            (now - entry_at).total_seconds() / 60, self.strat.entry_time, config.LATE_ENTRY_GRACE_MINUTES)
                return
            self._sleep_until(entry_at)
            self.enter()

        self.monitor()
        self.finish()

    def emergency_square_off(self) -> None:
        for leg in self.state.open_legs():
            try:
                self._exit_leg(leg, "INTERRUPT")
            except Exception:
                pass


# =========================================================================== #
# Entrypoint
# =========================================================================== #
def setup_logging() -> None:
    logfile = config.LOGS_DIR / f"live_{now_ist():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(logfile, encoding="utf-8")],
    )


class MultiStrategyRunner:
    """Runs and monitors multiple strategies concurrently in a single event loop."""

    def __init__(self, client: BreezeClient, strategy_keys: list[str], paper: bool, force_day: bool):
        self.client = client
        self.paper = paper
        self.force_day = force_day
        self.today = now_ist().date()
        self.traders: dict[str, StraddleTrader] = {}

        for k in strategy_keys:
            strat = config.STRATEGIES.get(k)
            if not strat:
                log.error("Unknown strategy '%s'. Skipping.", k)
                continue
            if not self.force_day:
                if self.today.weekday() not in strat.allowed_weekdays:
                    log.info("Strategy %s (%s) does not run on %s (runs %s). Skipping today.",
                             k, strat.name, self.today.strftime("%A"), strat.allowed_weekdays)
                    continue
                if is_holiday(self.today):
                    log.info("%s is an NSE holiday. Skipping strategy %s.", self.today, k)
                    continue
            self.traders[k] = StraddleTrader(client, strategy_key=k, paper=paper)

    def run(self) -> None:
        if not self.traders:
            log.info("No active strategies eligible to trade today.")
            return

        log.info("MultiStrategyRunner active with %d strategies: %s",
                 len(self.traders), [f"{k} ({t.strat.name})" for k, t in self.traders.items()])

        last_status_log = 0.0

        while True:
            now = now_ist()
            active_count = 0

            for k, trader in self.traders.items():
                s = trader.state
                # Check if state was updated/completed on disk externally (e.g. via --square-off)
                if s.path.exists():
                    try:
                        disk_data = json.loads(s.path.read_text())
                        if disk_data.get("completed") and not s.completed:
                            log.info("Strategy %s marked completed on disk externally. Stopping monitoring.", k)
                            s.completed = True
                            continue
                    except Exception:
                        pass

                if s.completed:
                    continue

                entry_at = trader._at(trader.strat.entry_time)
                exit_at = trader._at(trader.strat.exit_time)

                # Entry check
                if not s.entered:
                    if now >= exit_at:
                        log.info("Strategy %s past exit time (%s); skipping entry.", k, trader.strat.exit_time)
                        s.completed = True
                        s.save()
                        continue
                    if now >= entry_at:
                        if now <= entry_at + timedelta(minutes=config.LATE_ENTRY_GRACE_MINUTES):
                            log.info("Triggering ENTRY for strategy %s (%s)...", k, trader.strat.name)
                            try:
                                trader.enter()
                            except Exception as exc:
                                log.error("Entry failed for strategy %s: %s", k, exc)
                        else:
                            log.warning("Strategy %s started too late after %s. Skipping.", k, trader.strat.entry_time)
                            s.completed = True
                            s.save()
                            continue

                # Monitoring check if in open position
                if s.entered and not s.completed:
                    active_count += 1
                    open_legs = s.open_legs()

                    if now >= exit_at:
                        log.info("Strategy %s hit EXIT_TIME (%s). Squaring off...", k, trader.strat.exit_time)
                        for attempt in range(5):
                            for leg in s.open_legs():
                                try:
                                    trader._exit_leg(leg, "TIME")
                                except Exception:
                                    pass
                            if not s.open_legs():
                                break
                            _time.sleep(1)
                        trader.finish()
                        continue

                    # SL Check per open leg
                    stopped_now: list[LegState] = []
                    for leg in open_legs:
                        try:
                            ltp = self.client.get_option_ltp(
                                date.fromisoformat(leg.expiry), leg.right, leg.strike,
                                stock_code=leg.underlying, exchange_code=leg.option_exchange
                            )
                        except BreezeAPIError as exc:
                            log.warning("Strategy %s: LTP fetch failed for %s %s: %s", k, leg.underlying, leg.tag, exc)
                            continue
                        leg.last_ltp = ltp
                        if leg.is_sl_hit(ltp):
                            reason = "SL_COST" if leg.sl_moved_to_cost else "SL"
                            log.warning("Strategy %s [%s] %s %s SL HIT: LTP %.2f vs SL %.2f",
                                        k, trader.strat.name, leg.underlying, leg.tag, ltp, leg.sl_price)
                            try:
                                trader._exit_leg(leg, reason)
                            except Exception:
                                continue
                            stopped_now.append(leg)

                    if trader.strat.move_sl_to_cost and len(stopped_now) == 1:
                        for other in s.open_legs():
                            if not other.sl_moved_to_cost:
                                other.sl_price = other.entry_price
                                other.sl_moved_to_cost = True
                                log.info("Strategy %s cost-to-cost: %s %s SL moved to entry %.2f",
                                         k, other.underlying, other.tag, other.sl_price)
                    s.save()

                    if not s.open_legs():
                        trader.finish()

            # Status heartbeat across all running strategies
            if _time.monotonic() - last_status_log >= 60:
                last_status_log = _time.monotonic()
                total_mtm = sum(t.state.total_pnl() for t in self.traders.values())
                summary_parts = []
                for k, t in self.traders.items():
                    s = t.state
                    open_tags = [f"{l.tag}:{l.last_ltp:.1f}" for l in s.open_legs()]
                    status_str = f"{k}({t.strat.name}:{t.strat.underlying}:{t.strat.spot_exchange}) PnL:{s.total_pnl():.0f}"
                    if open_tags:
                        status_str += f" [{' '.join(open_tags)}]"
                    summary_parts.append(status_str)
                log.info("HEARTBEAT | Total MTM: %.2f | %s", total_mtm, " | ".join(summary_parts))

            # Check if all completed and all entry times passed
            all_done = all(
                t.state.completed or (not t.state.entered and now >= t._at(t.strat.exit_time))
                for t in self.traders.values()
            )
            if all_done:
                log.info("All configured strategies completed for the day.")
                break

            _time.sleep(config.POLL_INTERVAL_SEC)

    def emergency_square_off(self) -> None:
        for k, trader in self.traders.items():
            trader.emergency_square_off()


# =========================================================================== #
# Entrypoint
# =========================================================================== #
def setup_logging() -> None:
    logfile = config.LOGS_DIR / f"live_{now_ist():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(logfile, encoding="utf-8")],
    )


def execute_square_off(client: BreezeClient, target: str, forced_paper: Optional[bool] = None) -> None:
    """
    Squares off open positions immediately for a specific strategy or all strategies.
    Updates the on-disk state to completed so any running background monitor stops cleanly.
    """
    today = now_ist().date()
    if target.lower() == "all":
        target_keys = set(config.STRATEGIES.keys())
        for f in config.DATA_DIR.glob(f"live_state_{today}_*.json"):
            parts = f.stem.split("_")
            if len(parts) >= 4:
                target_keys.add(parts[3])
        strat_keys = sorted(list(target_keys))
    else:
        strat_keys = [k.strip() for k in target.split(",") if k.strip()]

    log.info("Executing square-off command for target(s): %s", strat_keys)

    squared_off_count = 0
    for k in strat_keys:
        if k not in config.STRATEGIES:
            log.warning("Strategy key '%s' not recognized in config.STRATEGIES. Skipping.", k)
            continue

        # Determine whether this strategy was running in paper or live mode
        if forced_paper is not None:
            paper_mode = forced_paper
        else:
            live_path = config.DATA_DIR / f"live_state_{today}_{k}_LIVE.json"
            paper_path = config.DATA_DIR / f"live_state_{today}_{k}_PAPER.json"
            if live_path.exists():
                paper_mode = False
            elif paper_path.exists():
                paper_mode = True
            else:
                paper_mode = config.PAPER_TRADE

        trader = StraddleTrader(client, strategy_key=k, paper=paper_mode)
        s = trader.state

        if s.completed:
            log.info("Strategy %s is already completed for today (%s).", k, s.path.name)
            continue

        if not s.entered or not s.open_legs():
            log.info("Strategy %s has no open legs. Marking as completed to prevent future entry.", k)
            s.completed = True
            s.save()
            continue

        log.warning("Squaring off %d open leg(s) for Strategy %s (%s, mode=%s)...",
                    len(s.open_legs()), k, trader.strat.name, trader.mode)
        for leg in list(s.open_legs()):
            try:
                trader._exit_leg(leg, "MANUAL_SQUARE_OFF")
            except Exception as exc:
                log.critical("Failed to square off Strategy %s leg %s: %s", k, leg.tag, exc)

        trader.finish()
        squared_off_count += 1
        log.info("Strategy %s squared off successfully. Net P&L: %.2f", k, s.total_pnl())

    if squared_off_count == 0:
        log.info("No active open positions required square-off.")
    else:
        log.info("Square-off completed for %d strategy/strategies.", squared_off_count)


def main() -> None:
    p = argparse.ArgumentParser(description="Multi-Strategy Options Live/Paper Bot (NSE NIFTY & BSE SENSEX)")
    p.add_argument("--strategy", default="all", help="Strategy key or comma list (e.g. '1,2,3' or 'all')")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="Force paper trading")
    g.add_argument("--live", action="store_true", help="Force LIVE trading (real orders)")
    p.add_argument("--force-day", action="store_true", help="Ignore weekday/holiday filter")
    p.add_argument("--square-off", metavar="TARGET", help="Square off immediately (e.g. '--square-off all' or '--square-off 1' or '--square-off 1,4')")
    args = p.parse_args()

    setup_logging()

    forced_paper = True if args.paper else False if args.live else None

    # Handle square-off utility command
    if args.square_off:
        if forced_paper is False:
            log.warning("*** LIVE SQUARE-OFF: REAL MARKET EXIT ORDERS WILL BE PLACED ***")
            client = BreezeClient(connect=True)
        else:
            try:
                client = BreezeClient(connect=True)
            except Exception as exc:
                log.warning("Breeze connection not established (%s). Using offline fallback client for square-off.", exc)
                client = BreezeClient(connect=False)

        execute_square_off(client, target=args.square_off, forced_paper=forced_paper)
        return

    # Normal execution flow
    if args.strategy.lower() == "all":
        strat_keys = list(config.STRATEGIES.keys())
    else:
        strat_keys = [k.strip() for k in args.strategy.split(",")]

    paper = True if args.paper else False if args.live else config.PAPER_TRADE

    if not paper:
        log.warning("*** LIVE MODE: REAL ORDERS WILL BE PLACED ***")

    log.info("Initializing Breeze API Session...")
    client = BreezeClient(connect=True)

    runner = MultiStrategyRunner(client, strategy_keys=strat_keys, paper=paper, force_day=args.force_day)
    try:
        runner.run()
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        if config.SQUARE_OFF_ON_INTERRUPT:
            log.warning("Squaring off open legs across all strategies (SQUARE_OFF_ON_INTERRUPT=True)")
            runner.emergency_square_off()
        else:
            log.warning("Open positions remain saved. Rerun the script to resume monitoring.")


if __name__ == "__main__":
    main()

