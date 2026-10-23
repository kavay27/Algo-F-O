# 📈 Algorithmic Options Trading System (ICICI Direct Breeze)

A complete, automated algorithmic trading and backtesting system built for **NSE NIFTY** and **BSE SENSEX** weekly index options using the **ICICI Direct Breeze API**.

This guide is written in plain language for easy reference, so anyone can operate, backtest, paper trade, live trade, and manage positions without needing a software engineering background.

---

## 📑 Table of Contents
1. [What This System Does](#-what-this-system-does)
2. [Folder & File Guide](#-folder--file-guide)
3. [One-Time Setup & Daily Login](#-one-time-setup--daily-login)
4. [Command Cheat Sheet (Copy & Paste)](#-command-cheat-sheet-copy--paste)
   - [Backtesting (Historical Testing)](#1-backtesting-historical-testing)
   - [Paper Trading (Practice Mode with Real Prices)](#2-paper-trading-practice-mode)
   - [Live Trading (Real Money Orders)](#3-live-trading-real-money-orders)
   - [Emergency & Square-Off Commands](#4-emergency--square-off-commands)
5. [How to Edit Strategies](#-how-to-edit-strategies)
6. [Daily Trading Routine (Checklist)](#-daily-trading-routine-checklist)
7. [Troubleshooting & FAQs](#-troubleshooting--faqs)

---

## 🎯 What This System Does

- **Multi-Strategy Engine**: Run different strategies simultaneously (e.g., NIFTY Straddle + SENSEX Strangle) in the same trading session.
- **Both NSE & BSE**: Fully supports NSE NIFTY (`NFO`) and BSE SENSEX (`BFO`) with automatic strike intervals (50 for NIFTY, 100 for SENSEX) and proper lot sizes (75 for NIFTY, 10 for SENSEX).
- **SEBI Expiry Aware**: Automatically uses the correct weekly expiry rules (NIFTY on Tuesdays, SENSEX on Thursdays).
- **Automated Risk Management**: Automatically monitors Stop Loss (SL), trails SL to cost when one leg is stopped out, and exits all positions at market close.
- **Safe State Recovery**: If your internet drops or you close the window, the bot saves position state to disk and resumes without double-entering.
- **Instant Square-Off Utility**: Stop all or any individual strategy immediately from a second terminal window with one command.

---

## 📂 Folder & File Guide

| File / Folder | What It Is & What It Does |
| :--- | :--- |
| **`config.py`** | **The Control Center.** All strategy parameters (entry/exit times, stop losses, strikes, lot sizes) live here. You only need to edit this file to adjust strategies. |
| **`live_trader.py`** | **The Live / Paper Trading Bot.** Executes your strategies during market hours and monitors prices second-by-second. |
| **`backtester.py`** | **The Time Machine.** Tests your strategies against historical market data to see how much profit/loss they would have made in the past. |
| **`breeze_client.py`** | Connects to ICICI Direct Breeze API, downloads market data, and manages orders safely. |
| **`.env`** | Private file holding your ICICI Direct API Key, Secret, and Daily Session Token. (Never share this file). |
| **`data/`** | Caches downloaded 1-minute historical data and keeps track of active live trade states. |
| **`results/`** | Stores CSV reports of all backtests, paper trades, and live trades. |
| **`logs/`** | Stores detailed logs of every price tick and order for troubleshooting. |

---

## 🔑 One-Time Setup & Daily Login

### Step 1: Open PowerShell in Your Algo Folder
1. Press `Windows Key + R`, type `powershell`, and press Enter.
2. Navigate to your project folder:
   ```powershell
   cd c:\Users\KIIT\algo
   ```

### Step 2: Configure Your `.env` File
Create a file named `.env` in `c:\Users\KIIT\algo\` (you can copy `.env.example`). It should look like this:
```ini
BREEZE_API_KEY=your_api_key_here
BREEZE_API_SECRET=your_api_secret_here
BREEZE_SESSION_TOKEN=your_daily_session_token_here
```

### Step 3: Every Morning Before Market Opens (08:45 AM)
ICICI Direct requires a **new Session Token every trading day**:
1. Open this link in your browser (replace `YOUR_API_KEY` with your actual API key):
   ```text
   https://api.icicidirect.com/apiuser/login?api_key=YOUR_API_KEY
   ```
2. Log into your ICICI Direct account with your User ID, Password, and OTP/PIN.
3. After logging in, look at the web browser address bar. Copy the number after `apisession=`:
   ```text
   https://127.0.0.1/?apisession=12345678
   ```
4. Paste that code into your `.env` file next to `BREEZE_SESSION_TOKEN`:
   ```ini
   BREEZE_SESSION_TOKEN=12345678
   ```
5. Save the `.env` file. You are now ready for the trading day!

---

## ⚡ Command Cheat Sheet (Copy & Paste)

Always run commands from `c:\Users\KIIT\algo`. 

> **Tip**: All commands below use the project's Python environment: `.\.venv\Scripts\python.exe`.

---

### 1. Backtesting (Historical Testing)

Test your strategies on past market data before risking real money.

#### Backtest All Strategies for a Date Range:
```powershell
.\.venv\Scripts\python.exe backtester.py --strategy all --start 2025-06-01 --end 2025-06-30
```

#### Backtest Only Strategy 1 (NIFTY):
```powershell
.\.venv\Scripts\python.exe backtester.py --strategy 1 --start 2025-06-01 --end 2025-06-30
```

#### Backtest Strategy 1 and Strategy 4 (NIFTY + SENSEX):
```powershell
.\.venv\Scripts\python.exe backtester.py --strategy 1,4 --start 2025-06-01 --end 2025-06-30
```

*Results are printed in the terminal and detailed trade-by-trade CSV files are saved in the `results/` folder.*

---

### 2. Paper Trading (Practice Mode)
*Paper trading uses real live market quotes from Breeze, but **simulates** fills. Real money is never used.*

#### Paper Trade All Configured Strategies (NIFTY + SENSEX together):
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy all --paper
```

#### Paper Trade Strategy 1 Only:
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy 1 --paper
```

#### Paper Trade Strategies 1 and 4 (NIFTY & SENSEX):
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy 1,4 --paper
```

#### Testing on Weekends / Outside Market Hours:
Add `--force-day` to bypass the weekday/holiday check:
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy 1 --paper --force-day
```

---

### 3. Live Trading (Real Money Orders)
> ⚠️ **CAUTION**: Live trading sends actual Buy/Sell market orders directly to your ICICI Direct demat account.

#### Live Trade All Configured Strategies:
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy all --live
```

#### Live Trade Specific Strategies (e.g. 1 & 4):
```powershell
.\.venv\Scripts\python.exe live_trader.py --strategy 1,4 --live
```

---

### 4. Emergency & Square-Off Commands

If you need to stop trading, exit your positions, or book profits early, open a **new PowerShell window** and run:

#### Emergency Exit ALL Active Strategies:
Immediately closes every open leg across NIFTY and SENSEX and stops the bot:
```powershell
.\.venv\Scripts\python.exe live_trader.py --square-off all
```

#### Square Off a Specific Strategy Only (e.g., Strategy 1):
Closes Strategy 1's open positions while letting Strategy 4 continue running:
```powershell
.\.venv\Scripts\python.exe live_trader.py --square-off 1
```

#### Square Off Selected Strategies (e.g., 1 and 2):
```powershell
.\.venv\Scripts\python.exe live_trader.py --square-off 1,2
```

#### Keyboard Shortcut to Stop:
In the window where the bot is running, press **`Ctrl + C`**. The bot will catch the interrupt, cleanly square off any open positions at market, and exit safely.

---

## 🛠️ How to Edit Strategies

Open `config.py` in any text editor (like VS Code, Notepad, or Antigravity IDE). Scroll to the `STRATEGIES` dictionary around line 135:

```python
STRATEGIES = {
    "1": StrategyConfig(
        name="NIFTY_ATM_Short_Straddle",
        underlying="NIFTY",               # "NIFTY" or "SENSEX"
        spot_exchange="NSE",              # "NSE" for NIFTY, "BSE" for SENSEX
        option_exchange="NFO",            # "NFO" for NIFTY, "BFO" for SENSEX
        strike_step=50,                   # 50 for NIFTY, 100 for SENSEX
        allowed_weekdays=(0, 1),          # 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
        entry_time=time(9, 20, 0),        # 09:20 AM entry
        exit_time=time(15, 15, 0),        # 03:15 PM exit
        lot_size=75,                      # 75 for NIFTY, 10 for SENSEX
        num_lots=1,                       # Number of lots to trade
        ce_action="sell",                 # "buy" or "sell"
        ce_strike_offset=0,               # 0 = ATM, +2 = OTM2 (100 pts up), -2 = ITM2 (100 pts down)
        ce_sl_percent=0.25,               # 0.25 = 25% Stop Loss
        pe_action="sell",                 # "buy" or "sell"
        pe_strike_offset=0,               # 0 = ATM
        pe_sl_percent=0.25,               # 0.25 = 25% Stop Loss
        move_sl_to_cost=True,             # If CE hits SL, move PE SL to entry price
    ),
    ...
}
```

### Common Adjustments:
- **Change Entry Time**: Change `time(9, 20, 0)` to `time(9, 30, 0)` for 09:30 AM.
- **Change Exit Time**: Change `time(15, 15, 0)` to `time(15, 25, 0)` for 03:25 PM.
- **Change Stop Loss**: Set `ce_sl_percent=0.30` for 30% SL, or `0.50` for 50% SL.
- **Trade OTM (Out of The Money)**:
  - For Call: `ce_strike_offset=2` (selects strike 2 steps above ATM: +100 for NIFTY, +200 for SENSEX).
  - For Put: `pe_strike_offset=-2` (selects strike 2 steps below ATM: -100 for NIFTY, -200 for SENSEX).
- **Trade ITM (In The Money)**:
  - For Call: `ce_strike_offset=-2` (selects strike 2 steps below ATM).
  - For Put: `pe_strike_offset=2` (selects strike 2 steps above ATM).
- **Increase Lots**: Set `num_lots=2` or `num_lots=3`.

---

## 📅 Daily Trading Routine (Checklist)

| Time (IST) | Action |
| :--- | :--- |
| **08:45 AM** | 1. Open ICICI Direct login link.<br>2. Copy the new daily `apisession` token.<br>3. Paste it into `.env` as `BREEZE_SESSION_TOKEN`. |
| **09:10 AM** | 1. Open PowerShell and run:<br>`.\.venv\Scripts\python.exe live_trader.py --strategy all --paper` (or `--live`)<br>2. Bot will show "Waiting until 09:20:00" and connect to the market. |
| **09:20 AM** | Bot takes entry, logs the exact ATM strikes, orders, and SL levels. |
| **09:20 - 15:15** | Bot logs status every minute with total Mark-to-Market (MTM) P&L and checks for stop-losses every second. |
| **15:15 PM** | Bot automatically squares off remaining open legs and saves final results in `results/`. |
| **Emergency** | If you want to exit early at any time, open another terminal and run:<br>`.\.venv\Scripts\python.exe live_trader.py --square-off all` |

---

## ❓ Troubleshooting & FAQs

### 1. `Session key is expired` or `Could not authenticate`
- **Cause**: ICICI Direct requires a new session token every morning.
- **Fix**: Repeat the 08:45 AM login step in [One-Time Setup & Daily Login](#-one-time-setup--daily-login) to get today's session token and paste it into `.env`.

### 2. `Started X minutes after entry time. Skipping today.`
- **Cause**: You started the bot after the allowed entry window (default grace period is 5 minutes).
- **Fix**: Make sure to start the script at or before the configured `entry_time`. To change the grace window, edit `LATE_ENTRY_GRACE_MINUTES` in `config.py`.

### 3. `Today is weekday X (not allowed)`
- **Cause**: The strategy is set to only trade on certain days (e.g., Monday and Tuesday).
- **Fix**: In `config.py`, check `allowed_weekdays=(0, 1, 2, 3, 4)`. You can add more days or use `--force-day` to test right now.

### 4. How do I know if my trade was paper or live?
- Check the log output:
  - `[PAPER]` indicates simulated fills (no real money).
  - `[LIVE]` indicates actual order IDs with ICICI Direct.
- Check the results files:
  - `results/paper_trades.csv` for paper trades.
  - `results/live_trades.csv` for real trades.

---

*Keep this file handy for all daily operations and strategy backtests!*
