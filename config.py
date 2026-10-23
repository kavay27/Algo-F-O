"""
config.py
=========
Single source of truth for credentials and every strategy parameter.

Credentials are loaded from a local `.env` file (see `.env.example`).
Everything else is plain Python so it can be tweaked without touching logic.
"""

import os
from datetime import date, time
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"          # cached 1-minute CSVs + live state files
LOGS_DIR = BASE_DIR / "logs"          # log files
RESULTS_DIR = BASE_DIR / "results"    # backtest / paper / live trade logs

for _d in (DATA_DIR, LOGS_DIR, RESULTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Credentials (.env)
# --------------------------------------------------------------------------- #
load_dotenv(BASE_DIR / ".env")

BREEZE_API_KEY = os.getenv("BREEZE_API_KEY", "").strip()
BREEZE_API_SECRET = os.getenv("BREEZE_API_SECRET", "").strip()
BREEZE_SESSION_TOKEN = os.getenv("BREEZE_SESSION_TOKEN", "").strip()

# --------------------------------------------------------------------------- #
# General
# --------------------------------------------------------------------------- #
TIMEZONE = "Asia/Kolkata"
MARKET_OPEN = time(9, 15, 0)
MARKET_CLOSE = time(15, 30, 0)

# --------------------------------------------------------------------------- #
# Instrument & Exchange Defaults
# --------------------------------------------------------------------------- #
UNDERLYING = "NIFTY"          # Default stock_code
SPOT_EXCHANGE = "NSE"         # Default spot exchange
SPOT_PRODUCT_TYPE = "cash"
OPTION_EXCHANGE = "NFO"       # Default options exchange
OPTION_PRODUCT_TYPE = "options"
STRIKE_STEP = 50              # Default ATM rounding step

INSTRUMENT_DEFAULTS = {
    "NIFTY": {
        "spot_code": "NIFTY",
        "fno_code": "NIFTY",
        "spot_exchange": "NSE",
        "option_exchange": "NFO",
        "strike_step": 50,
        "lot_size": 75,
    },
    "SENSEX": {
        "spot_code": "BSESEN",     # Breeze stock_code for SENSEX
        "fno_code": "BSESEN",      # Breeze stock_code for SENSEX options
        "spot_exchange": "BSE",
        "option_exchange": "BFO",
        "strike_step": 100,
        "lot_size": 10,
    },
    "BSESEN": {
        "spot_code": "BSESEN",
        "fno_code": "BSESEN",
        "spot_exchange": "BSE",
        "option_exchange": "BFO",
        "strike_step": 100,
        "lot_size": 10,
    },
}

# Expiry rules per underlying
# Effective 1-Sep-2025: NIFTY weekly expiry is Tuesday, SENSEX weekly expiry is Thursday.
EXPIRY_WEEKDAY_CHANGE_DATE = date(2025, 9, 1)
EXPIRY_RULES = {
    "NIFTY": {
        "old": 3,  # Thursday before 1-Sep-2025
        "new": 1,  # Tuesday on/after 1-Sep-2025
    },
    "SENSEX": {
        "old": 4,  # Friday before 1-Sep-2025 (or earlier 1=Tue)
        "new": 3,  # Thursday on/after 1-Sep-2025
    },
    "BSESEN": {
        "old": 4,
        "new": 3,
    },
}

# --------------------------------------------------------------------------- #
# Strategy Definitions & Configuration
# --------------------------------------------------------------------------- #
from dataclasses import dataclass, field
from typing import Dict, Tuple

@dataclass
class StrategyConfig:
    name: str
    underlying: str = "NIFTY"          # "NIFTY" or "SENSEX" / "BSESEN"
    spot_exchange: str = "NSE"         # "NSE" or "BSE"
    option_exchange: str = "NFO"       # "NFO" or "BFO"
    strike_step: int = 50              # 50 for NIFTY, 100 for SENSEX
    allowed_weekdays: Tuple[int, ...] = (0, 1)  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    entry_time: time = time(9, 20, 0)
    exit_time: time = time(15, 15, 0)
    lot_size: int = 75                 # 75 for NIFTY, 10 for SENSEX
    num_lots: int = 1
    
    # CE Leg Config
    ce_action: str = "sell"            # "buy" or "sell"
    ce_strike_offset: int = 0          # 0=ATM, +1=OTM1, -1=ITM1 (each step is strike_step pts)
    ce_sl_percent: float = 0.25        # 0.25 = 25% SL
    
    # PE Leg Config
    pe_action: str = "sell"            # "buy" or "sell"
    pe_strike_offset: int = 0          # 0=ATM, +1=ITM1, -1=OTM1 (each step is strike_step pts)
    pe_sl_percent: float = 0.25        # 0.25 = 25% SL
    
    move_sl_to_cost: bool = True       # If one leg hits SL, move survivor SL to cost
    
    @property
    def quantity(self) -> int:
        return self.lot_size * self.num_lots


# Registry of all strategies. You can define strategy 1, 2, 3, 4, 5 etc.
# Both NSE (NIFTY) and BSE (SENSEX) strategies can be defined and run together!
STRATEGIES: Dict[str, StrategyConfig] = {
    # Strategy 1: NIFTY ATM Short Straddle (NSE / NFO)
    "1": StrategyConfig(
        name="NIFTY_ATM_Short_Straddle",
        underlying="NIFTY",
        spot_exchange="NSE",
        option_exchange="NFO",
        strike_step=50,
        allowed_weekdays=(0, 1),
        entry_time=time(9, 16, 0),
        exit_time=time(15, 30, 0),
        lot_size=75,
        num_lots=1,
        ce_action="buy",
        ce_strike_offset=20,       # ATM + 1000
        ce_sl_percent=0.0,
        pe_action="buy",
        pe_strike_offset=-20,      # ATM - 1000
        pe_sl_percent=0.0,
        move_sl_to_cost=False,
    ),
    
    # Strategy 2: NIFTY Sell ATM (NSE / NFO)
    "2": StrategyConfig(
        name="NIFTY_Sell_ATM_1030",
        underlying="NIFTY",
        spot_exchange="NSE",
        option_exchange="NFO",
        strike_step=50,
        allowed_weekdays=(0, 1),
        entry_time=time(9, 20, 0),
        exit_time=time(10, 30, 0),
        lot_size=75,
        num_lots=1,
        ce_action="sell",
        ce_strike_offset=0,       # ATM
        ce_sl_percent=0.50,       # 50% SL
        pe_action="sell",
        pe_strike_offset=0,       # ATM
        pe_sl_percent=0.50,       # 50% SL
        move_sl_to_cost=False,
    ),

    # Strategy 3: NIFTY Afternoon Sell ATM (NSE / NFO)
    "3": StrategyConfig(
        name="NIFTY_Sell_ATM_1430",
        underlying="NIFTY",
        spot_exchange="NSE",
        option_exchange="NFO",
        strike_step=50,
        allowed_weekdays=(0, 1),
        entry_time=time(14, 30, 0),
        exit_time=time(15, 30, 0),
        lot_size=75,
        num_lots=1,
        ce_action="sell",
        ce_strike_offset=0,       # ATM
        ce_sl_percent=0.50,
        pe_action="sell",
        pe_strike_offset=0,       # ATM
        pe_sl_percent=0.50,
        move_sl_to_cost=False,
    ),

    # Strategy 4: BSE SENSEX ATM Short Straddle (BSE / BFO)
    "4": StrategyConfig(
        name="SENSEX_ATM_Short_Straddle",
        underlying="SENSEX",
        spot_exchange="BSE",
        option_exchange="BFO",
        strike_step=100,           # SENSEX strike interval is 100 pts
        allowed_weekdays=(0, 1, 2, 3, 4),  # All weekdays
        entry_time=time(9, 20, 0),
        exit_time=time(15, 15, 0),
        lot_size=10,               # SENSEX lot size
        num_lots=1,
        ce_action="sell",
        ce_strike_offset=0,        # ATM
        ce_sl_percent=0.25,        # 25% SL
        pe_action="sell",
        pe_strike_offset=0,        # ATM
        pe_sl_percent=0.25,        # 25% SL
        move_sl_to_cost=True,
    ),

    # Strategy 5: BSE SENSEX OTM Strangle (BSE / BFO)
    "5": StrategyConfig(
        name="SENSEX_OTM_Strangle",
        underlying="SENSEX",
        spot_exchange="BSE",
        option_exchange="BFO",
        strike_step=100,           # SENSEX strike interval is 100 pts
        allowed_weekdays=(0, 1, 2, 3, 4),
        entry_time=time(9, 25, 0),
        exit_time=time(15, 10, 0),
        lot_size=10,
        num_lots=1,
        ce_action="sell",
        ce_strike_offset=2,        # OTM 2 (ATM + 200)
        ce_sl_percent=0.30,
        pe_action="sell",
        pe_strike_offset=-2,       # OTM 2 (ATM - 200)
        pe_sl_percent=0.30,
        move_sl_to_cost=True,
    ),
}

# Active default strategy key (e.g. "1", "2", "3", "4", "5", or "all")
ACTIVE_STRATEGY = "all"

# --------------------------------------------------------------------------- #
# Expiry handling
# --------------------------------------------------------------------------- #
EXPIRY_WEEKDAY_OLD = 3        # Thursday (fallback for legacy imports)
EXPIRY_WEEKDAY_NEW = 1        # Tuesday  (fallback for legacy imports)

# If an expiry falls on a holiday NSE shifts it to the previous trading day.
# Both the backtester and live trader auto-detect this by probing for data,
# but you can hard-code known exceptions here: {"nominal": "actual"} (YYYY-MM-DD)
EXPIRY_OVERRIDES: dict[str, str] = {
    # "2025-10-21": "2025-10-20",
}

# Optional list of NSE trading holidays (YYYY-MM-DD). Used to skip days and
# to shift expiries without an extra API probe. Fill from the NSE circular.
NSE_HOLIDAYS: set[str] = set()

# --------------------------------------------------------------------------- #
# Paper / live trading
# --------------------------------------------------------------------------- #
PAPER_TRADE = True            # True -> real quotes, simulated orders (no place_order)
POLL_INTERVAL_SEC = 2.0       # LTP polling interval while monitoring
LATE_ENTRY_GRACE_MINUTES = 5  # allow entry if bot starts up to N min after ENTRY_TIME
ORDER_FILL_TIMEOUT_SEC = 30   # wait for live order to report "Executed"
PAPER_SLIPPAGE_POINTS = 0.0   # simulated slippage per fill in paper mode
SQUARE_OFF_ON_INTERRUPT = False  # Ctrl+C: square off open legs before exiting?

# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
# Which 1-min candle field represents the price "at" ENTRY_TIME / EXIT_TIME.
# "open" of the 09:20 candle == price at 09:20:00 (recommended).
BACKTEST_ENTRY_PRICE_FIELD = "open"
BACKTEST_EXIT_PRICE_FIELD = "open"
BACKTEST_SLIPPAGE_POINTS = 0.5     # adverse slippage per fill (points)
BACKTEST_BROKERAGE_PER_ORDER = 20.0  # flat brokerage per executed order (INR)
BACKTEST_OTHER_CHARGES_PCT = 0.0005  # STT/exchange/GST approx, on sell turnover

# --------------------------------------------------------------------------- #
# Breeze API throttling / retries
# --------------------------------------------------------------------------- #
API_CALLS_PER_MINUTE = 90     # Breeze hard limit is 100/min; keep headroom
API_MIN_INTERVAL_SEC = 0.15   # minimum gap between two consecutive calls
API_MAX_RETRIES = 3           # retries for transient failures (NOT for orders)
API_RETRY_BACKOFF_SEC = 2.0   # exponential backoff base
