import math
from logzero import logger


# ── Gann Config ──────────────────────────────────────────────────
INCREMENT = 0.125        # 1/8 step — standard Gann Square of 9 unit
NUM_LEVELS = 4           # How many S/R levels to calculate each side
BREAKOUT_BUFFER = 0.001  # 0.1% buffer above/below level to confirm breakout
# ────────────────────────────────────────────────────────────────


def square_of_9(base_price: float, increment: float = INCREMENT, levels: int = NUM_LEVELS) -> dict:
    """
    Calculate Gann Square of 9 support and resistance levels.

    Formula: level = (sqrt(base) ± n * increment) ** 2

    Returns dict with keys: base, R1..R4, S1..S4
    """
    sqrt_base = math.sqrt(base_price)
    result = {"base": round(base_price, 2)}

    for n in range(1, levels + 1):
        result[f"R{n}"] = round((sqrt_base + n * increment) ** 2, 2)
        result[f"S{n}"] = round((sqrt_base - n * increment) ** 2, 2)

    return result


def get_signal(levels: dict, prev_close: float, current_close: float) -> str:
    """
    Regular Gann crossover signal on a 5-min candle close.
    Gap-open entries are handled separately in the bot/backtest engine
    with a 15-minute wait and different risk rules.

    BUY_CE : price crossed above R1 breakout level
    BUY_PE : price crossed below S1 breakdown level
    HOLD   : no signal
    """
    r1 = levels["R1"]
    s1 = levels["S1"]

    above_r1 = r1 * (1 + BREAKOUT_BUFFER)
    below_s1 = s1 * (1 - BREAKOUT_BUFFER)

    if prev_close < above_r1 and current_close >= above_r1:
        logger.info(f"BUY_CE signal | {current_close:.2f} crossed above R1={r1:.2f}")
        return "BUY_CE"

    if prev_close > below_s1 and current_close <= below_s1:
        logger.info(f"BUY_PE signal | {current_close:.2f} crossed below S1={s1:.2f}")
        return "BUY_PE"

    return "HOLD"


def get_exit_signal(levels: dict, position_type: str, current_price: float,
                    entry_spot: float) -> str:
    """
    Exit logic based on Gann levels.

    CE exits:
      - Target : spot reaches R2 and is above entry
      - Stop   : spot falls back below R1 (breakout failed)

    PE exits:
      - Target : spot reaches S2 and is below entry
      - Stop   : spot rises back above S1 (breakdown failed)
    """
    r1 = levels["R1"]
    s1 = levels["S1"]
    r2 = levels["R2"]
    s2 = levels["S2"]

    if position_type == "CE":
        if current_price >= r2 and current_price > entry_spot:
            logger.info(f"CE TARGET hit | spot={current_price:.2f} R2={r2:.2f}")
            return "TARGET"
        # Stop: falls back below R1 — breakout failed
        if current_price <= r1 * (1 - BREAKOUT_BUFFER):
            logger.info(f"CE STOP hit | spot={current_price:.2f} fell below R1={r1:.2f}")
            return "SL"

    elif position_type == "PE":
        if current_price <= s2 and current_price < entry_spot:
            logger.info(f"PE TARGET hit | spot={current_price:.2f} S2={s2:.2f}")
            return "TARGET"
        # Stop: rises back above S1 — breakdown failed
        if current_price >= s1 * (1 + BREAKOUT_BUFFER):
            logger.info(f"PE STOP hit | spot={current_price:.2f} rose above S1={s1:.2f}")
            return "SL"

    return "HOLD"


def is_entry_valid(levels: dict, signal: str, entry_spot: float,
                   min_rr: float = 0.2) -> tuple:
    """
    Validate entry quality before placing a trade.

    Checks:
      1. Room to target  — entry must be below R2 (CE) / above S2 (PE)
      2. Minimum R:R     — reward >= min_rr * risk  (using R1/S1 as stop anchor)

    Returns (valid: bool, reason: str)
    """
    if signal == "BUY_CE":
        if entry_spot >= levels["R2"]:
            return False, f"entry {entry_spot:.0f} >= R2 {levels['R2']:.0f} — no room to target"
        reward = levels["R2"] - entry_spot
        risk   = entry_spot - levels["R1"] * (1 - BREAKOUT_BUFFER)
        if risk > 0 and reward < min_rr * risk:
            return False, f"R:R {reward:.0f}/{risk:.0f} = {reward/risk:.2f} below min {min_rr}"
        return True, ""

    elif signal == "BUY_PE":
        if entry_spot <= levels["S2"]:
            return False, f"entry {entry_spot:.0f} <= S2 {levels['S2']:.0f} — no room to target"
        reward = entry_spot - levels["S2"]
        risk   = levels["S1"] * (1 + BREAKOUT_BUFFER) - entry_spot
        if risk > 0 and reward < min_rr * risk:
            return False, f"R:R {reward:.0f}/{risk:.0f} = {reward/risk:.2f} below min {min_rr}"
        return True, ""

    return True, ""


def format_levels(levels: dict) -> str:
    """Human-readable level summary for logging."""
    return (
        f"R4={levels['R4']:.0f}  R3={levels['R3']:.0f}  R2={levels['R2']:.0f}  R1={levels['R1']:.0f}  "
        f"[BASE={levels['base']:.0f}]  "
        f"S1={levels['S1']:.0f}  S2={levels['S2']:.0f}  S3={levels['S3']:.0f}  S4={levels['S4']:.0f}"
    )
