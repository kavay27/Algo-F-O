"""
breeze_client.py
================
Thin, defensive wrapper around `breeze_connect.BreezeConnect`.

Features
--------
* Lazy session creation (cached backtests can run fully offline).
* Sliding-window rate limiter (calls/minute + min gap between calls).
* Retries with exponential backoff for transient failures.
  Order placement is NEVER blindly retried (to avoid duplicate orders).
* Local CSV cache for 1-minute historical candles in `data/`.
* Helpers: ATM strike rounding, Breeze date formatting, weekly expiry logic.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections import deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pytz

import config

IST = pytz.timezone(config.TIMEZONE)
log = logging.getLogger(__name__)

OHLC_COLS = ["open", "high", "low", "close", "volume", "open_interest"]


# =========================================================================== #
# Exceptions
# =========================================================================== #
class BreezeAPIError(Exception):
    """Any non-successful Breeze response."""


class NoDataError(BreezeAPIError):
    """Breeze answered successfully but there is no data (holiday / bad contract)."""


# =========================================================================== #
# SDK import (isolated)
# =========================================================================== #
def _import_breeze_connect():
    """
    `breeze_connect` does `sys.path.insert(1, <its dir>); import config`.
    Our project also has a `config.py`, which would shadow the SDK's module.
    Import the SDK with our module/dir temporarily hidden, then restore.
    Done lazily because the SDK downloads its security master at import time.
    """
    import sys
    if "breeze_connect" in sys.modules:
        return sys.modules["breeze_connect"].BreezeConnect

    project_dir = Path(__file__).resolve().parent
    ours = sys.modules.pop("config", None)
    saved_path = list(sys.path)
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != project_dir]
    try:
        from breeze_connect import BreezeConnect
    finally:
        sys.path[:] = saved_path
        sys.modules.pop("config", None)       # SDK's config
        if ours is not None:
            sys.modules["config"] = ours      # restore project config
    return BreezeConnect


# =========================================================================== #
# Generic helpers
# =========================================================================== #
def now_ist() -> datetime:
    return datetime.now(IST)


def resolve_breeze_stock_code(stock_code: str) -> str:
    """Map common index names to the official Breeze stock code."""
    s = (stock_code or "").upper().strip()
    if s in ("SENSEX", "BSESEN"):
        return "BSESEN"
    if s in ("NIFTY", "NIFTY 50"):
        return "NIFTY"
    return s


def round_to_strike(price: float, step: int = config.STRIKE_STEP) -> int:
    """ATM strike: nearest multiple of `step`."""
    return int(round(price / float(step)) * step)


def to_breeze_iso(dt: datetime) -> str:
    """Breeze expects 'YYYY-MM-DDTHH:MM:SS.000Z' strings."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def breeze_expiry(d: date) -> str:
    """Expiry format used by Breeze for quotes / orders / history."""
    return f"{d:%Y-%m-%d}T06:00:00.000Z"


def is_holiday(d: date) -> bool:
    return d.weekday() >= 5 or d.isoformat() in config.NSE_HOLIDAYS


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while is_holiday(d):
        d -= timedelta(days=1)
    return d


def expiry_weekday_for(d: date, underlying: str = "NIFTY") -> int:
    rules = config.EXPIRY_RULES.get(underlying.upper(), {"old": 3, "new": 1})
    return rules["new"] if d >= config.EXPIRY_WEEKDAY_CHANGE_DATE else rules["old"]


def nominal_weekly_expiry(trade_date: date, underlying: str = "NIFTY") -> date:
    """
    Nearest weekly expiry on/after `trade_date`, before holiday adjustment
    by API probing. Applies EXPIRY_OVERRIDES and NSE_HOLIDAYS shifts.
    """
    d = trade_date
    target_weekday = expiry_weekday_for(d, underlying=underlying)
    while d.weekday() != target_weekday:
        d += timedelta(days=1)

    override = config.EXPIRY_OVERRIDES.get(d.isoformat())
    if override:
        return date.fromisoformat(override)

    while is_holiday(d) and d > trade_date:
        d = previous_trading_day(d)
    return d


def expiry_candidates(trade_date: date, underlying: str = "NIFTY", max_candidates: int = 3) -> list[date]:
    """Nominal expiry followed by earlier trading days (holiday shift probing)."""
    first = nominal_weekly_expiry(trade_date, underlying=underlying)
    out = [first]
    d = first
    while len(out) < max_candidates:
        d = previous_trading_day(d)
        if d < trade_date:
            break
        out.append(d)
    return out


# =========================================================================== #
# Rate limiter
# =========================================================================== #
class RateLimiter:
    """Thread-safe sliding-window limiter."""

    def __init__(self, max_calls: int, period: float = 60.0, min_interval: float = 0.0):
        self.max_calls = max_calls
        self.period = period
        self.min_interval = min_interval
        self._calls: deque[float] = deque()
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = _time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()

                wait_gap = self.min_interval - (now - self._last)
                if len(self._calls) >= self.max_calls:
                    wait_window = self.period - (now - self._calls[0]) + 0.01
                else:
                    wait_window = 0.0

                wait = max(wait_gap, wait_window)
                if wait <= 0:
                    self._calls.append(now)
                    self._last = now
                    return
                if wait_window > 0:
                    log.debug("Rate limit reached, sleeping %.2fs", wait)
                _time.sleep(wait)


# =========================================================================== #
# Breeze client
# =========================================================================== #
class BreezeClient:
    def __init__(
        self,
        api_key: str = config.BREEZE_API_KEY,
        api_secret: str = config.BREEZE_API_SECRET,
        session_token: str = config.BREEZE_SESSION_TOKEN,
        connect: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token
        self.breeze = None  # BreezeConnect instance (created lazily)
        self.limiter = RateLimiter(
            config.API_CALLS_PER_MINUTE, 60.0, config.API_MIN_INTERVAL_SEC
        )
        if connect:
            self.connect()

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        if self.breeze is not None:
            return
        if not (self.api_key and self.api_secret and self.session_token):
            raise BreezeAPIError(
                "Missing Breeze credentials. Fill BREEZE_API_KEY, BREEZE_API_SECRET "
                "and BREEZE_SESSION_TOKEN in .env"
            )
        log.info("Creating Breeze session ...")
        BreezeConnect = _import_breeze_connect()
        breeze = BreezeConnect(api_key=self.api_key)
        breeze.generate_session(api_secret=self.api_secret,
                                session_token=self.session_token)
        self.breeze = breeze
        log.info("Breeze session established.")

    # ------------------------------------------------------------------ #
    # Core call with throttling / retries / response validation
    # ------------------------------------------------------------------ #
    def _call(self, method: str, retries: Optional[int] = None, **kwargs) -> Any:
        self.connect()
        retries = config.API_MAX_RETRIES if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            try:
                resp = getattr(self.breeze, method)(**kwargs)
                return self._parse(resp, method)
            except NoDataError:
                raise
            except Exception as exc:  # network errors, 5xx, rate-limit, etc.
                if attempt > retries:
                    log.error("%s failed after %d attempt(s): %s", method, attempt, exc)
                    raise BreezeAPIError(f"{method} failed: {exc}") from exc
                backoff = config.API_RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
                log.warning("%s attempt %d failed (%s). Retrying in %.1fs",
                            method, attempt, exc, backoff)
                _time.sleep(backoff)

    @staticmethod
    def _parse(resp: Any, method: str) -> Any:
        if not isinstance(resp, dict):
            raise BreezeAPIError(f"{method}: unexpected response {resp!r}")
        status = resp.get("Status")
        error = resp.get("Error")
        success = resp.get("Success")
        if error:
            if "no data" in str(error).lower():
                raise NoDataError(f"{method}: {error}")
            raise BreezeAPIError(f"{method}: Status={status} Error={error}")
        if status not in (200, "200", None):
            raise BreezeAPIError(f"{method}: Status={status} Response={resp}")
        if success is None or (isinstance(success, list) and len(success) == 0):
            raise NoDataError(f"{method}: empty Success payload")
        return success

    # ------------------------------------------------------------------ #
    # Historical data (1-minute) with local cache
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cache_path(stock_code: str, exchange_code: str, product_type: str,
                    trade_date: date, expiry: Optional[date],
                    right: Optional[str], strike: Optional[int]) -> Path:
        if product_type == "cash":
            name = f"{stock_code}_{exchange_code}_spot_{trade_date:%Y%m%d}.csv"
        else:
            name = (f"{stock_code}_{exchange_code}_{expiry:%Y%m%d}_{strike}"
                    f"{'CE' if right == 'call' else 'PE'}_{trade_date:%Y%m%d}.csv")
        sub = config.DATA_DIR / f"{trade_date:%Y}" / f"{trade_date:%m}"
        sub.mkdir(parents=True, exist_ok=True)
        return sub / name

    def get_historical_1min(
        self,
        stock_code: str,
        exchange_code: str,
        product_type: str,
        trade_date: date,
        expiry: Optional[date] = None,
        right: Optional[str] = None,
        strike: Optional[int] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """
        Returns a DataFrame indexed by IST-naive datetime with OHLCV columns,
        restricted to market hours of `trade_date`. Empty DataFrame == no data.
        """
        path = self._cache_path(stock_code, exchange_code, product_type,
                                trade_date, expiry, right, strike)
        if use_cache and path.exists():
            df = pd.read_csv(path, parse_dates=["datetime"], index_col="datetime")
            return df

        from_dt = datetime.combine(trade_date, time(9, 0))
        to_dt = datetime.combine(trade_date, time(15, 59))
        breeze_code = resolve_breeze_stock_code(stock_code)
        kwargs = dict(
            interval="1minute",
            from_date=to_breeze_iso(from_dt),
            to_date=to_breeze_iso(to_dt),
            stock_code=breeze_code,
            exchange_code=exchange_code,
            product_type=product_type,
            expiry_date=breeze_expiry(expiry) if expiry else "",
            right=right or "",
            strike_price=str(strike) if strike is not None else "",
        )
        try:
            rows = self._call("get_historical_data_v2", **kwargs)
            df = self._to_frame(rows, trade_date)
        except NoDataError:
            if breeze_code != stock_code:
                try:
                    kwargs["stock_code"] = stock_code
                    rows = self._call("get_historical_data_v2", **kwargs)
                    df = self._to_frame(rows, trade_date)
                except NoDataError:
                    df = pd.DataFrame(columns=OHLC_COLS)
                    df.index.name = "datetime"
            else:
                df = pd.DataFrame(columns=OHLC_COLS)
                df.index.name = "datetime"

        # Never cache today's (incomplete) data.
        if use_cache and trade_date < now_ist().date():
            df.to_csv(path)
        return df

    @staticmethod
    def _to_frame(rows: list[dict], trade_date: date) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if df.empty or "datetime" not in df:
            out = pd.DataFrame(columns=OHLC_COLS)
            out.index.name = "datetime"
            return out
        df["datetime"] = pd.to_datetime(df["datetime"])
        for c in OHLC_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else 0.0
        df = df[OHLC_COLS + ["datetime"]].drop_duplicates("datetime")
        df = df.set_index("datetime").sort_index()
        start = datetime.combine(trade_date, config.MARKET_OPEN)
        end = datetime.combine(trade_date, config.MARKET_CLOSE)
        return df.loc[(df.index >= start) & (df.index <= end)]

    def get_spot_history(self, trade_date: date, stock_code: str = config.UNDERLYING,
                         exchange_code: str = config.SPOT_EXCHANGE,
                         use_cache: bool = True) -> pd.DataFrame:
        return self.get_historical_1min(stock_code, exchange_code,
                                        config.SPOT_PRODUCT_TYPE, trade_date,
                                        use_cache=use_cache)

    def get_option_history(self, trade_date: date, expiry: date, right: str,
                           strike: int, stock_code: str = config.UNDERLYING,
                           exchange_code: str = config.OPTION_EXCHANGE,
                           use_cache: bool = True) -> pd.DataFrame:
        return self.get_historical_1min(stock_code, exchange_code,
                                        config.OPTION_PRODUCT_TYPE, trade_date,
                                        expiry=expiry, right=right, strike=strike,
                                        use_cache=use_cache)

    # ------------------------------------------------------------------ #
    # Live quotes
    # ------------------------------------------------------------------ #
    def get_ltp(self, stock_code: str, exchange_code: str, product_type: str,
                expiry: Optional[date] = None, right: str = "",
                strike: Optional[int] = None) -> float:
        breeze_code = resolve_breeze_stock_code(stock_code)
        try:
            rows = self._call(
                "get_quotes",
                stock_code=breeze_code,
                exchange_code=exchange_code,
                expiry_date=breeze_expiry(expiry) if expiry else "",
                product_type=product_type,
                right=right,
                strike_price=str(strike) if strike is not None else "",
            )
        except NoDataError:
            if breeze_code != stock_code:
                rows = self._call(
                    "get_quotes",
                    stock_code=stock_code,
                    exchange_code=exchange_code,
                    expiry_date=breeze_expiry(expiry) if expiry else "",
                    product_type=product_type,
                    right=right,
                    strike_price=str(strike) if strike is not None else "",
                )
            else:
                raise
        row = next((r for r in rows if str(r.get("exchange_code", "")).upper()
                    == exchange_code.upper()), rows[0])
        ltp = float(row.get("ltp") or 0)
        if ltp <= 0:
            raise NoDataError(f"Zero/invalid LTP for {stock_code} {right} {strike}")
        return ltp

    def get_spot_ltp(self, stock_code: str = config.UNDERLYING,
                     exchange_code: str = config.SPOT_EXCHANGE) -> float:
        return self.get_ltp(stock_code, exchange_code, config.SPOT_PRODUCT_TYPE)

    def get_option_ltp(self, expiry: date, right: str, strike: int,
                       stock_code: str = config.UNDERLYING,
                       exchange_code: str = config.OPTION_EXCHANGE) -> float:
        return self.get_ltp(stock_code, exchange_code,
                            config.OPTION_PRODUCT_TYPE, expiry, right, strike)

    # ------------------------------------------------------------------ #
    # Orders (LIVE only)
    # ------------------------------------------------------------------ #
    def place_option_order(self, action: str, right: str, strike: int,
                           expiry: date, quantity: int,
                           stock_code: str = config.UNDERLYING,
                           exchange_code: str = config.OPTION_EXCHANGE) -> str:
        """Market order on options. Returns order_id. NOT retried."""
        today = now_ist().date()
        breeze_code = resolve_breeze_stock_code(stock_code)
        resp = self._call(
            "place_order",
            retries=0,
            stock_code=breeze_code,
            exchange_code=exchange_code,
            product="options",
            action=action,                 # "buy" / "sell"
            order_type="market",
            stoploss="",
            quantity=str(quantity),
            price="",
            validity="day",
            validity_date=to_breeze_iso(datetime.combine(today, time(6, 0))),
            disclosed_quantity="0",
            expiry_date=breeze_expiry(expiry),
            right=right,                   # "call" / "put"
            strike_price=str(strike),
            user_remark="AlgoOptionsTrade",
        )
        order_id = resp.get("order_id") if isinstance(resp, dict) else None
        if not order_id:
            raise BreezeAPIError(f"place_order returned no order_id: {resp}")
        log.info("ORDER PLACED %s %s %s %s %s qty=%s -> %s",
                 action.upper(), stock_code, strike, right.upper(), expiry, quantity, order_id)
        return str(order_id)

    def get_order_detail(self, order_id: str, exchange_code: str = config.OPTION_EXCHANGE) -> dict:
        rows = self._call("get_order_detail",
                          exchange_code=exchange_code, order_id=order_id)
        return rows[0] if isinstance(rows, list) else rows

    def wait_for_fill(self, order_id: str, exchange_code: str = config.OPTION_EXCHANGE,
                      timeout: float = config.ORDER_FILL_TIMEOUT_SEC) -> float:
        """Poll order book until executed; returns average fill price."""
        deadline = _time.monotonic() + timeout
        last_status = None
        while _time.monotonic() < deadline:
            try:
                d = self.get_order_detail(order_id, exchange_code=exchange_code)
                last_status = str(d.get("status", "")).lower()
                if last_status in ("executed", "complete", "completed"):
                    px = float(d.get("average_price") or d.get("price") or 0)
                    if px > 0:
                        return px
                if last_status in ("rejected", "cancelled", "canceled"):
                    raise BreezeAPIError(f"Order {order_id} {last_status}: "
                                         f"{d.get('cancellation_reason') or d}")
            except NoDataError:
                pass
            _time.sleep(1.0)
        raise BreezeAPIError(f"Order {order_id} not filled within {timeout}s "
                             f"(last status={last_status})")
