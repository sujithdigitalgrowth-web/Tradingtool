import time
import json
import os
from datetime import datetime
from logzero import logger, logfile

from login import login
from broker import (
    get_nifty_atm_option, get_candle_data,
    get_ltp, place_buy_order, place_sell_order, square_off_all
)
from strategy import compute_levels, check_entry, check_exit
from risk_manager import RiskManager, QTY
from gann import BREAKOUT_BUFFER, is_entry_valid

os.makedirs("logs", exist_ok=True)
logfile("logs/bot.log", maxBytes=5e6, backupCount=3)

STATE_FILE          = "logs/state.json"
TRADING_FLAG_FILE   = "logs/trading_enabled.json"
NIFTY_TOKEN  = "26000"   # NSE Nifty 50 index token
GAP_WAIT_TIME = "09:30"  # Skip first 15 min on gap-open days
GAP_STOP_PCT  = 0.004    # 0.4% spot stop for gap trades
MIN_RR_RATIO  = 0.2      # Minimum reward:risk before taking entry


# ── Helpers ──────────────────────────────────────────────────────

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def is_trading_enabled() -> bool:
    try:
        if os.path.exists(TRADING_FLAG_FILE):
            with open(TRADING_FLAG_FILE) as f:
                return json.load(f).get("enabled", False)
    except Exception:
        pass
    return False


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "09:15" <= t <= "15:30"


def get_prev_close(obj) -> float:
    """Fetch previous day Nifty 50 close from daily candles."""
    from broker import get_candle_data
    df = get_candle_data(obj, NIFTY_TOKEN, exchange="NSE", interval="ONE_DAY", days=5)
    if df.empty or len(df) < 2:
        raise Exception("Could not fetch Nifty 50 daily candles for prev close")
    prev_close = float(df.iloc[-2]["close"])
    logger.info(f"Nifty 50 previous close: {prev_close:.2f}")
    return prev_close


# ── Main Bot ─────────────────────────────────────────────────────

def run():
    logger.info("=" * 55)
    logger.info("  Nifty 50 Gann Square of 9 Options Bot")
    logger.info("=" * 55)

    obj, auth_token, feed_token, refresh_token = login()
    risk = RiskManager()

    # ── Morning setup: compute Gann levels ──
    prev_close = get_prev_close(obj)
    gann_levels = compute_levels(prev_close)

    position = {
        "active": False,
        "type": None,           # "CE" or "PE"
        "symbol": None,
        "token": None,
        "entry_price": 0.0,     # option entry price
        "entry_spot": 0.0,      # BankNifty spot at entry
        "qty": QTY,
        "gap_trade": False,
    }

    gap_type = None
    last_candle_minute = None
    sl_sides = set()            # directions that already hit SL today

    while True:
        now = datetime.now()

        if not is_market_open():
            logger.info("Market closed. Waiting...")
            time.sleep(60)
            continue

        # ── Force square-off at 15:15 ───────────────────────
        if risk.should_square_off_now():
            if position["active"]:
                logger.info("Force square-off time reached.")
                ltp = get_ltp(obj, "NFO", position["symbol"], position["token"])
                risk.record_trade(
                    position["symbol"], position["type"],
                    position["entry_price"], ltp, position["qty"], "EOD_SQUAREOFF"
                )
                square_off_all(obj)
                position["active"] = False
            save_state({"date": now.strftime("%d %b %Y"), "risk": risk.summary(), "position": position, "gann": gann_levels})
            logger.info("All positions squared off. Done for today.")
            time.sleep(3600)
            continue

        # ── Wait for new 5-min candle ────────────────────────
        current_minute = now.replace(second=0, microsecond=0)
        if last_candle_minute == current_minute:
            time.sleep(10)
            continue

        try:
            spot = get_ltp(obj, "NSE", "Nifty 50", NIFTY_TOKEN)
        except Exception as e:
            logger.error(f"Could not fetch spot: {e}")
            time.sleep(15)
            continue

        time_str = now.strftime("%H:%M")

        # Detect gap type from first 9:15 candle open
        if gap_type is None and time_str >= "09:15":
            r1_buf = gann_levels["R1"] * (1 + BREAKOUT_BUFFER)
            s1_buf = gann_levels["S1"] * (1 - BREAKOUT_BUFFER)
            if spot > r1_buf:
                gap_type = "UP"
                logger.info(f"GAP-UP detected: spot={spot:.0f} > R1={gann_levels['R1']:.0f}")
            elif spot < s1_buf:
                gap_type = "DOWN"
                logger.info(f"GAP-DOWN detected: spot={spot:.0f} < S1={gann_levels['S1']:.0f}")
            else:
                gap_type = "NONE"

        # ── Manage existing position ─────────────────────────
        if position["active"]:
            try:
                option_ltp = get_ltp(obj, "NFO", position["symbol"], position["token"])

                # 1. Check hard SL/Target on option price
                hard_action = risk.check_sl_target(position["entry_price"], option_ltp)

                # 2. Check exit on spot (gap vs normal rules)
                if position["gap_trade"]:
                    # Fix 2: tighter 0.4% spot stop
                    gap_stop_hit = (
                        (position["type"] == "CE" and spot <= position["entry_spot"] * (1 - GAP_STOP_PCT)) or
                        (position["type"] == "PE" and spot >= position["entry_spot"] * (1 + GAP_STOP_PCT))
                    )
                    # Fix 3: R3/S3 target for gap trades
                    gap_target_hit = (
                        (position["type"] == "CE" and spot >= gann_levels["R3"] and spot > position["entry_spot"]) or
                        (position["type"] == "PE" and spot <= gann_levels["S3"] and spot < position["entry_spot"])
                    )
                    gann_action = "TARGET" if gap_target_hit else ("SL" if gap_stop_hit else "HOLD")
                else:
                    gann_action = check_exit(gann_levels, position["type"], spot, position["entry_spot"])

                exit_reason = None
                if hard_action in ("SL", "TARGET"):
                    exit_reason = hard_action
                elif gann_action in ("SL", "TARGET"):
                    exit_reason = f"GANN_{gann_action}"

                if exit_reason:
                    place_sell_order(obj, position["symbol"], position["token"], position["qty"])
                    risk.record_trade(
                        position["symbol"], position["type"],
                        position["entry_price"], option_ltp, position["qty"], exit_reason
                    )
                    if "SL" in exit_reason:
                        sl_sides.add(position["type"])
                    position["active"] = False
                    logger.info(f"Position closed [{exit_reason}] | Option=₹{option_ltp:.2f} Spot=₹{spot:.2f}")

            except Exception as e:
                logger.error(f"Error managing position: {e}")

        # ── Look for new entry ───────────────────────────────
        if not position["active"] and not is_trading_enabled():
            logger.info("Trading disabled via dashboard — skipping entry")

        if not position["active"] and is_trading_enabled():
            can_trade, reason = risk.can_trade()
            if not can_trade:
                logger.info(f"Skipping entry: {reason}")
            # Fix 1: on gap days, skip until GAP_WAIT_TIME
            elif gap_type not in (None, "NONE") and time_str < GAP_WAIT_TIME:
                logger.info(f"GAP-{gap_type} day — waiting until {GAP_WAIT_TIME} before entry")
            else:
                try:
                    df = get_candle_data(obj, NIFTY_TOKEN, exchange="NSE", interval="FIVE_MINUTE")

                    if df.empty:
                        logger.warning("No candle data, skipping.")
                    else:
                        signal = check_entry(gann_levels, df)

                        # Gap direction filter
                        if gap_type == "UP" and signal != "BUY_CE":
                            signal = "HOLD"
                        elif gap_type == "DOWN" and signal != "BUY_PE":
                            signal = "HOLD"

                        # No re-entry in same direction after SL
                        side_check = "CE" if signal == "BUY_CE" else ("PE" if signal == "BUY_PE" else None)
                        if side_check in sl_sides:
                            logger.info(f"Skipping {signal}: already hit SL on {side_check} today")
                            signal = "HOLD"

                        # Entry quality: room to target + minimum R:R
                        if signal in ("BUY_CE", "BUY_PE"):
                            valid, reason = is_entry_valid(gann_levels, signal, spot, MIN_RR_RATIO)
                            if not valid:
                                logger.info(f"Entry filtered ({signal}): {reason}")
                                signal = "HOLD"

                        is_gap = gap_type not in (None, "NONE")

                        if signal == "BUY_CE":
                            sym, tok, strike, _ = get_nifty_atm_option(obj, "CE")
                            entry_price = get_ltp(obj, "NFO", sym, tok)
                            place_buy_order(obj, sym, tok, QTY)
                            position.update({
                                "active": True, "type": "CE",
                                "symbol": sym, "token": tok,
                                "entry_price": entry_price, "entry_spot": spot,
                                "qty": QTY, "gap_trade": is_gap,
                            })
                            logger.info(f"NIFTY ENTERED CE {'[GAP]' if is_gap else ''} | {sym} Opt=₹{entry_price:.2f} Spot={spot:.0f} R1={gann_levels['R1']:.0f}")

                        elif signal == "BUY_PE":
                            sym, tok, strike, _ = get_nifty_atm_option(obj, "PE")
                            entry_price = get_ltp(obj, "NFO", sym, tok)
                            place_buy_order(obj, sym, tok, QTY)
                            position.update({
                                "active": True, "type": "PE",
                                "symbol": sym, "token": tok,
                                "entry_price": entry_price, "entry_spot": spot,
                                "qty": QTY, "gap_trade": is_gap,
                            })
                            logger.info(f"NIFTY ENTERED PE {'[GAP]' if is_gap else ''} | {sym} Opt=₹{entry_price:.2f} Spot={spot:.0f} S1={gann_levels['S1']:.0f}")

                except Exception as e:
                    logger.error(f"Error in entry logic: {e}")

        last_candle_minute = current_minute
        save_state({"date": now.strftime("%d %b %Y"), "risk": risk.summary(), "position": position, "gann": gann_levels})
        logger.info(
            f"Spot=₹{spot:.0f}  R1={gann_levels['R1']:.0f}  S1={gann_levels['S1']:.0f}  "
            f"PnL=₹{risk.daily_pnl:.0f}  Trades={risk.trade_count}  "
            f"Pos={'[' + position['type'] + ']' if position['active'] else 'None'}"
        )
        time.sleep(10)


if __name__ == "__main__":
    run()
