"""
Strategy: Gann Square of 9 intraday breakout on BankNifty spot.

Morning setup  : Calculate S/R levels from previous day's BankNifty close.
Entry          : 5-min candle closes above R1 → BUY CE
                 5-min candle closes below S1 → BUY PE
Target         : R2 (CE) / S2 (PE) on spot
Stop           : Spot falls back below base (CE) / rises back above base (PE)
Hard SL/Target : risk_manager.py 30% option SL / 50% option target (safety net)
No new entries : After 14:45
Square-off     : 15:15 hard cutoff
"""

import pandas as pd
from logzero import logger
from gann import square_of_9, get_signal, get_exit_signal, format_levels

MIN_CANDLES = 5   # Minimum candles needed before taking a signal


def compute_levels(prev_close: float) -> dict:
    """Compute today's Gann levels from yesterday's BankNifty close."""
    levels = square_of_9(prev_close)
    logger.info(f"Gann levels | {format_levels(levels)}")
    return levels


def check_entry(levels: dict, df: pd.DataFrame) -> str:
    """
    Returns 'BUY_CE', 'BUY_PE', or 'HOLD'.
    Uses the last two 5-min candle closes for breakout detection.
    """
    if len(df) < MIN_CANDLES:
        logger.warning(f"Not enough candles ({len(df)}), skipping entry check.")
        return "HOLD"

    prev_close = float(df.iloc[-2]["close"])
    curr_close = float(df.iloc[-1]["close"])

    return get_signal(levels, prev_close, curr_close)


def check_exit(levels: dict, position_type: str, current_spot: float, entry_spot: float) -> str:
    """
    Returns 'SL', 'TARGET', or 'HOLD'.
    Checks Gann level exits on the spot price.
    """
    return get_exit_signal(levels, position_type, current_spot, entry_spot)
