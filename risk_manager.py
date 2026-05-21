from logzero import logger
from datetime import datetime


# ── Risk Config ──────────────────────────────────────────────────
MAX_DAILY_LOSS     = 3000   # Stop if total loss exceeds this per day
MAX_TRADES_PER_DAY = 2      # Max trades per day
STOP_LOSS_PCT      = 0.30   # Exit if option loses 30% from entry price
TARGET_PCT         = 0.50   # Book profit if option gains 50% from entry price
LOT_SIZE           = 75     # Nifty 50 lot size (1 lot = 75 units)
NUM_LOTS           = 1      # Number of lots to trade
SQUARE_OFF_TIME     = "15:15"
NO_NEW_TRADE_TIME   = "14:50"
DAILY_PROFIT_TARGET = 40       # Lock in profit: stop new entries once P&L >= ₹40
# ────────────────────────────────────────────────────────────────

QTY = 1   # Strict: buy exactly 1 option unit per trade


class RiskManager:
    def __init__(self):
        self.daily_pnl  = 0.0
        self.trade_count = 0
        self.win_count   = 0
        self.trades_log  = []

    def can_trade(self) -> tuple:
        now = datetime.now().strftime("%H:%M")

        if now >= NO_NEW_TRADE_TIME:
            return False, f"No new trades after {NO_NEW_TRADE_TIME}"

        if self.daily_pnl <= -MAX_DAILY_LOSS:
            return False, f"Daily loss limit hit (₹{self.daily_pnl:.0f})"

        if self.trade_count >= MAX_TRADES_PER_DAY:
            return False, f"Max trades per day reached ({self.trade_count})"

        # Profit lock: preserve today's gains
        if self.daily_pnl >= DAILY_PROFIT_TARGET:
            return False, f"Daily profit target reached (+₹{self.daily_pnl:.0f}) — locked in"

        return True, "OK"

    def should_square_off_now(self) -> bool:
        now = datetime.now().strftime("%H:%M")
        return now >= SQUARE_OFF_TIME

    def check_sl_target(self, entry_price: float, current_price: float) -> str:
        """Returns 'SL', 'TARGET', or 'HOLD'."""
        if entry_price <= 0:
            return "HOLD"

        change_pct = (current_price - entry_price) / entry_price

        if change_pct <= -STOP_LOSS_PCT:
            logger.warning(f"Stop-loss triggered | Entry={entry_price} Current={current_price} Change={change_pct:.1%}")
            return "SL"

        if change_pct >= TARGET_PCT:
            logger.info(f"Target hit | Entry={entry_price} Current={current_price} Change={change_pct:.1%}")
            return "TARGET"

        return "HOLD"

    def record_trade(self, symbol: str, side: str, entry: float, exit_price: float, qty: int, reason: str):
        pnl = (exit_price - entry) * qty
        self.daily_pnl  += pnl
        self.trade_count += 1
        if pnl > 0:
            self.win_count += 1
        self.trades_log.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "qty": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
        })
        logger.info(f"Trade recorded | {symbol} {side} Entry={entry} Exit={exit_price} PnL=₹{pnl:.2f} Reason={reason}")

    def summary(self) -> dict:
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "trade_count": self.trade_count,
            "trades": self.trades_log,
        }
