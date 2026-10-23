"""
backtester.py
=============
Historical backtest of the Monday & Tuesday NIFTY short straddle using
1-minute Breeze data (cached in `data/`).

Simulation rules (per trading day, Mon/Tue only)
------------------------------------------------
1. Spot price at ENTRY_TIME (09:20) -> ATM strike (nearest 50).
2. Sell ATM CE + ATM PE of the nearest weekly expiry at ENTRY_TIME.
3. SL per leg = entry * (1 + SL_PERCENT). A leg is stopped out on the first
   candle whose HIGH >= SL. Fill = max(SL, candle open) + slippage
   (gap-through-SL is filled at the open, which is worse).
4. MOVE_SL_TO_COST: once one leg is stopped, the surviving leg's SL becomes its
   entry price, effective from the NEXT candle (no intra-candle look-ahead).
   If both legs hit SL in the same candle, both are stopped.
5. Any open leg is squared off at EXIT_TIME (15:15).

Usage
-----
    python backtester.py --start 2025-01-01 --end 2025-12-31
    python backtester.py --start 2025-06-01 --end 2025-06-30 --sl 0.3 --no-c2c
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

import config
from breeze_client import (BreezeAPIError, BreezeClient, expiry_candidates,
                           is_holiday, round_to_strike)

log = logging.getLogger("backtester")
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Leg:
    right: str
    action: str        # "buy" or "sell"
    strike: int
    entry: float
    sl: float
    open: bool = True
    exit: Optional[float] = None
    exit_time: Optional[datetime] = None
    reason: str = ""
    sl_moved: bool = False

    @property
    def tag(self) -> str:
        return "ce" if self.right == "call" else "pe"

    def is_sl_hit(self, candle: pd.Series) -> tuple[bool, float]:
        """Check if SL is hit based on action. Buy -> Low drops below SL. Sell -> High rises above SL."""
        if self.action == "sell":
            if float(candle["high"]) >= self.sl:
                return True, max(self.sl, float(candle["open"]))
        else: # "buy"
            if float(candle["low"]) <= self.sl:
                return True, min(self.sl, float(candle["open"]))
        return False, 0.0

    def calc_pnl(self, qty: int) -> float:
        if self.exit is None:
            return 0.0
        if self.action == "sell":
            return (self.entry - self.exit) * qty
        else:
            return (self.exit - self.entry) * qty


class Backtester:
    def __init__(self, client: BreezeClient, strategy: Optional[config.StrategyConfig] = None,
                 use_cache: bool = True):
        self.client = client
        self.strat = strategy or config.STRATEGIES[config.ACTIVE_STRATEGY]
        self.use_cache = use_cache
        self.slip = config.BACKTEST_SLIPPAGE_POINTS

    # ------------------------------------------------------------------ #
    @staticmethod
    def _price_at(df: pd.DataFrame, ts: datetime, field: str) -> tuple[Optional[datetime], Optional[float]]:
        """Price of the first candle at/after `ts` (within 5 minutes)."""
        sub = df.loc[(df.index >= ts) & (df.index <= ts + timedelta(minutes=5))]
        if sub.empty:
            return None, None
        return sub.index[0], float(sub.iloc[0][field])

    def _resolve_expiry_and_data(self, d: date, right: str, strike: int):
        """Find the actual expiry (handles holiday shifts) and its data for a specific strike."""
        for exp in expiry_candidates(d, underlying=self.strat.underlying):
            df = self.client.get_option_history(
                d, exp, right, strike,
                stock_code=self.strat.underlying,
                exchange_code=self.strat.option_exchange,
                use_cache=self.use_cache
            )
            if not df.empty:
                return exp, df
        return None, None

    # ------------------------------------------------------------------ #
    def run_day(self, d: date) -> Optional[dict]:
        spot_df = self.client.get_spot_history(
            d,
            stock_code=self.strat.underlying,
            exchange_code=self.strat.spot_exchange,
            use_cache=self.use_cache
        )
        if spot_df.empty:
            log.info("%s: no %s spot data on %s (holiday?) - skipped", d, self.strat.underlying, self.strat.spot_exchange)
            return None

        entry_ts = datetime.combine(d, self.strat.entry_time)
        exit_ts = datetime.combine(d, self.strat.exit_time)
        ef = config.BACKTEST_ENTRY_PRICE_FIELD
        xf = config.BACKTEST_EXIT_PRICE_FIELD

        _, spot = self._price_at(spot_df, entry_ts, ef)
        if spot is None:
            log.warning("%s: no %s spot candle at entry time - skipped", d, self.strat.underlying)
            return None
            
        atm_strike = round_to_strike(spot, step=self.strat.strike_step)
        ce_strike = atm_strike + (self.strat.ce_strike_offset * self.strat.strike_step)
        pe_strike = atm_strike + (self.strat.pe_strike_offset * self.strat.strike_step)

        exp_ce, ce_df = self._resolve_expiry_and_data(d, "call", ce_strike)
        exp_pe, pe_df = self._resolve_expiry_and_data(d, "put", pe_strike)
        if exp_ce is None or exp_pe is None:
            log.warning("%s: option data missing for %s %s CE %s / PE %s - skipped",
                        d, self.strat.underlying, self.strat.option_exchange, ce_strike, pe_strike)
            return None
        expiry = exp_ce

        legs: dict[str, Leg] = {}
        entry_candle: dict[str, datetime] = {}
        leg_configs = [
            ("call", ce_df, self.strat.ce_action, ce_strike, self.strat.ce_sl_percent),
            ("put", pe_df, self.strat.pe_action, pe_strike, self.strat.pe_sl_percent),
        ]
        
        for right, df, action, strike, sl_pct in leg_configs:
            ts, px = self._price_at(df, entry_ts, ef)
            if px is None:
                log.warning("%s: no %s candle at entry - skipped", d, right)
                return None
                
            # Slippage: buying costs more, selling yields less
            fill = (px + self.slip) if action == "buy" else (px - self.slip)
            
            # SL calculation:
            # For Sell: SL is higher (entry * (1 + sl_pct))
            # For Buy: SL is lower (entry * (1 - sl_pct))
            if action == "sell":
                sl = round(fill * (1.0 + sl_pct), 2)
            else:
                sl = round(fill * (1.0 - sl_pct), 2)
                
            legs[right] = Leg(right, action, strike, fill, sl)
            entry_candle[right] = ts

        # ---------------- minute-by-minute SL simulation ---------------- #
        frames = {"call": ce_df, "put": pe_df}
        timeline = sorted(set(ce_df.index) | set(pe_df.index))
        timeline = [t for t in timeline if entry_ts <= t < exit_ts]

        for ts in timeline:
            hit_now = []
            for right, leg in legs.items():
                if not leg.open or ts not in frames[right].index:
                    continue
                if ts < entry_candle[right] or (ef == "close" and ts == entry_candle[right]):
                    continue
                c = frames[right].loc[ts]
                hit, base = leg.is_sl_hit(c)
                if hit:
                    leg.exit = (base - self.slip) if leg.action == "buy" else (base + self.slip)
                    leg.exit_time = ts
                    leg.reason = "SL_COST" if leg.sl_moved else "SL"
                    leg.open = False
                    hit_now.append(right)

            if self.strat.move_sl_to_cost and len(hit_now) == 1:
                other = legs["put" if hit_now[0] == "call" else "call"]
                if other.open and not other.sl_moved:
                    other.sl = other.entry
                    other.sl_moved = True

            if not any(l.open for l in legs.values()):
                break

        # ---------------- time exit ---------------- #
        for right, leg in legs.items():
            if not leg.open:
                continue
            df = frames[right]
            ts, px = self._price_at(df, exit_ts, xf)
            if px is None:  # fallback: last close before exit time
                prior = df.loc[df.index < exit_ts]
                ts, px = prior.index[-1], float(prior.iloc[-1]["close"])
            leg.exit = (px - self.slip) if leg.action == "buy" else (px + self.slip)
            leg.exit_time = ts
            leg.reason = "TIME"
            leg.open = False

        # ---------------- P&L ---------------- #
        q = self.strat.quantity
        gross = sum(l.calc_pnl(q) for l in legs.values())
        turnover = sum(l.entry * q for l in legs.values())
        charges = 4 * config.BACKTEST_BROKERAGE_PER_ORDER + turnover * config.BACKTEST_OTHER_CHARGES_PCT

        row = {
            "date": d, "weekday": WEEKDAY_NAMES[d.weekday()],
            "strategy": self.strat.name,
            "underlying": self.strat.underlying,
            "exchange": f"{self.strat.spot_exchange}/{self.strat.option_exchange}",
            "spot": round(spot, 2), "atm_strike": atm_strike, "expiry": expiry,
        }
        for l in legs.values():
            row.update({
                f"{l.tag}_action": l.action.upper(),
                f"{l.tag}_strike": l.strike,
                f"{l.tag}_entry": round(l.entry, 2),
                f"{l.tag}_sl_initial": round(l.sl, 2),
                f"{l.tag}_exit": round(l.exit, 2),
                f"{l.tag}_exit_time": l.exit_time.strftime("%H:%M") if l.exit_time else "",
                f"{l.tag}_exit_reason": l.reason,
                f"{l.tag}_pnl": round(l.calc_pnl(q), 2),
            })
        row.update({"gross_pnl": round(gross, 2), "charges": round(charges, 2),
                    "net_pnl": round(gross - charges, 2)})
        log.info("%s %s CE %s %s->%s (%s) | PE %s %s->%s (%s) | net=%.0f",
                 d, row["weekday"], row["ce_strike"],
                 row["ce_entry"], row["ce_exit"], row["ce_exit_reason"],
                 row["pe_strike"], row["pe_entry"], row["pe_exit"], row["pe_exit_reason"], row["net_pnl"])
        return row

    # ------------------------------------------------------------------ #
    def run(self, start: date, end: date) -> pd.DataFrame:
        rows = []
        d = start
        while d <= end:
            if d.weekday() in self.strat.allowed_weekdays and not is_holiday(d):
                try:
                    r = self.run_day(d)
                    if r:
                        rows.append(r)
                except BreezeAPIError as exc:
                    log.error("%s: API error, day skipped: %s", d, exc)
            d += timedelta(days=1)
        return pd.DataFrame(rows)


# =========================================================================== #
# Reporting
# =========================================================================== #
def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    pnl = df["net_pnl"]
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    sl_hits = int((df["ce_exit_reason"].str.startswith("SL")).sum()
                  + (df["pe_exit_reason"].str.startswith("SL")).sum())
    return {
        "trades": len(df),
        "net_pnl": round(pnl.sum(), 2),
        "gross_pnl": round(df["gross_pnl"].sum(), 2),
        "charges": round(df["charges"].sum(), 2),
        "win_rate_%": round(100 * len(wins) / len(df), 2),
        "avg_pnl": round(pnl.mean(), 2),
        "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.mean(), 2) if len(losses) else 0.0,
        "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if losses.sum() else np.inf,
        "max_win": round(pnl.max(), 2),
        "max_loss": round(pnl.min(), 2),
        "max_drawdown": round(drawdown.min(), 2),
        "legs_stopped": sl_hits,
        "both_legs_stopped_days": int(((df["ce_exit_reason"].str.startswith("SL"))
                                       & (df["pe_exit_reason"].str.startswith("SL"))).sum()),
        "pnl_by_weekday": df.groupby("weekday")["net_pnl"].sum().round(2).to_dict(),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Multi-Strategy Options Backtester (NSE NIFTY & BSE SENSEX)")
    p.add_argument("--strategy", default="all", help=f"Strategy key or comma-separated list (e.g. '1,2,3' or 'all'). Available: {list(config.STRATEGIES.keys())}")
    p.add_argument("--start", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    p.add_argument("--no-cache", action="store_true", help="Force re-download")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOGS_DIR / "backtest.log", encoding="utf-8")],
    )

    if args.strategy.lower() == "all":
        strat_keys = list(config.STRATEGIES.keys())
    else:
        strat_keys = [k.strip() for k in args.strategy.split(",")]

    client = BreezeClient()
    all_summaries = {}
    all_trade_frames = []

    for key in strat_keys:
        strat = config.STRATEGIES.get(key)
        if not strat:
            log.error("Unknown strategy '%s'. Available: %s", key, list(config.STRATEGIES.keys()))
            continue

        bt = Backtester(client, strategy=strat, use_cache=not args.no_cache)
        log.info("\n" + "=" * 60)
        log.info("RUNNING STRATEGY %s: %s | Days=%s | Entry=%s Exit=%s",
                 key, strat.name, strat.allowed_weekdays, strat.entry_time, strat.exit_time)
        log.info("=" * 60)
        trades = bt.run(args.start, args.end)

        if trades.empty:
            log.warning("No trades generated for strategy %s.", key)
            continue

        out = config.RESULTS_DIR / f"backtest_strat_{key}_{strat.name}_{args.start:%Y%m%d}_{args.end:%Y%m%d}.csv"
        trades.to_csv(out, index=False)
        all_trade_frames.append(trades)

        stats = summarize(trades)
        all_summaries[f"{key} ({strat.name})"] = stats

        print("\n" + "=" * 60 + f"\nSUMMARY: Strategy {key} - {strat.name}\n" + "=" * 60)
        for k, v in stats.items():
            print(f"{k:>24}: {v}")
        print("=" * 60 + f"\nTrade log: {out}\n")

    # Combined portfolio view across all executed strategies
    if len(all_trade_frames) > 1:
        combined_df = pd.concat(all_trade_frames, ignore_index=True)
        comb_out = config.RESULTS_DIR / f"backtest_COMBINED_{args.start:%Y%m%d}_{args.end:%Y%m%d}.csv"
        combined_df.to_csv(comb_out, index=False)

        print("\n" + "#" * 65 + "\nCOMBINED PORTFOLIO SUMMARY (ALL STRATEGIES)\n" + "#" * 65)
        comb_stats = summarize(combined_df)
        for k, v in comb_stats.items():
            print(f"{k:>24}: {v}")
        print("#" * 65 + f"\nCombined log saved to: {comb_out}\n")


if __name__ == "__main__":
    main()

