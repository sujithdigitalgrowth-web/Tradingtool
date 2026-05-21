import requests
import pandas as pd
from logzero import logger
from datetime import datetime, timedelta
import json
import os


INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_instruments = None


def load_instruments():
    global _instruments
    if _instruments is None:
        logger.info("Loading instrument master...")
        resp = requests.get(INSTRUMENT_URL, timeout=30)
        _instruments = resp.json()
        logger.info(f"Loaded {len(_instruments)} instruments")
    return _instruments


def get_banknifty_atm_option(obj, option_type="CE"):
    """Get current week BankNifty ATM option symbol and token."""
    instruments = load_instruments()

    # Get BankNifty spot price
    ltp_data = obj.ltpData("NSE", "Nifty Bank", "26009")
    spot = float(ltp_data["data"]["ltp"])
    logger.info(f"BankNifty spot: {spot}")

    # Round to nearest 100 for ATM strike
    atm_strike = round(spot / 100) * 100

    # Find nearest Thursday expiry
    today = datetime.today()
    days_until_thursday = (3 - today.weekday()) % 7
    if days_until_thursday == 0 and today.hour >= 15:
        days_until_thursday = 7
    expiry = today + timedelta(days=days_until_thursday)
    expiry_str = expiry.strftime("%d%b%Y").upper()

    symbol = f"BANKNIFTY{expiry_str}{atm_strike}{option_type}"
    logger.info(f"Looking for symbol: {symbol}")

    for inst in instruments:
        if (
            inst.get("name") == "BANKNIFTY"
            and inst.get("instrumenttype") == "OPTIDX"
            and str(inst.get("strike")) == str(float(atm_strike) * 100)
            and inst.get("symbol", "").endswith(option_type)
            and inst.get("exch_seg") == "NFO"
        ):
            exp_date = datetime.strptime(inst["expiry"], "%d%b%Y")
            if exp_date.date() == expiry.date():
                logger.info(f"Found: {inst['symbol']} token={inst['token']}")
                return inst["symbol"], inst["token"], atm_strike, spot

    # If exact expiry not found, find closest
    candidates = []
    for inst in instruments:
        if (
            inst.get("name") == "BANKNIFTY"
            and inst.get("instrumenttype") == "OPTIDX"
            and str(inst.get("strike")) == str(float(atm_strike) * 100)
            and inst.get("symbol", "").endswith(option_type)
            and inst.get("exch_seg") == "NFO"
        ):
            try:
                exp_date = datetime.strptime(inst["expiry"], "%d%b%Y")
                if exp_date >= today:
                    candidates.append((exp_date, inst))
            except Exception:
                pass

    if candidates:
        candidates.sort(key=lambda x: x[0])
        inst = candidates[0][1]
        logger.info(f"Using nearest expiry: {inst['symbol']} token={inst['token']}")
        return inst["symbol"], inst["token"], atm_strike, spot

    raise Exception(f"Could not find BankNifty {option_type} option near strike {atm_strike}")


def get_nifty_atm_option(obj, option_type="CE"):
    """Get current week Nifty 50 ATM option symbol and token."""
    instruments = load_instruments()

    # Get Nifty 50 spot price
    ltp_data = obj.ltpData("NSE", "Nifty 50", "26000")
    spot = float(ltp_data["data"]["ltp"])
    logger.info(f"Nifty 50 spot: {spot}")

    # Round to nearest 50 for ATM strike (Nifty options use 50-point intervals)
    atm_strike = round(spot / 50) * 50

    # Find expiry: weekly (nearest Thursday), but skip expiry-day — too volatile
    today = datetime.today()
    days_until_thursday = (3 - today.weekday()) % 7
    is_expiry_day = (days_until_thursday == 0)
    if is_expiry_day:
        # On Thursday: always use next week's expiry to avoid expiry-day risk
        days_until_thursday = 7
    expiry = today + timedelta(days=days_until_thursday)

    for inst in instruments:
        if (
            inst.get("name") == "NIFTY"
            and inst.get("instrumenttype") == "OPTIDX"
            and str(inst.get("strike")) == str(float(atm_strike) * 100)
            and inst.get("symbol", "").endswith(option_type)
            and inst.get("exch_seg") == "NFO"
        ):
            try:
                exp_date = datetime.strptime(inst["expiry"], "%d%b%Y")
                if exp_date.date() == expiry.date():
                    logger.info(f"Found: {inst['symbol']} token={inst['token']}")
                    return inst["symbol"], inst["token"], atm_strike, spot
            except Exception:
                pass

    # Fallback: nearest upcoming expiry at ATM strike
    candidates = []
    for inst in instruments:
        if (
            inst.get("name") == "NIFTY"
            and inst.get("instrumenttype") == "OPTIDX"
            and str(inst.get("strike")) == str(float(atm_strike) * 100)
            and inst.get("symbol", "").endswith(option_type)
            and inst.get("exch_seg") == "NFO"
        ):
            try:
                exp_date = datetime.strptime(inst["expiry"], "%d%b%Y")
                if exp_date >= today:
                    candidates.append((exp_date, inst))
            except Exception:
                pass

    if candidates:
        candidates.sort(key=lambda x: x[0])
        inst = candidates[0][1]
        logger.info(f"Using nearest expiry: {inst['symbol']} token={inst['token']}")
        return inst["symbol"], inst["token"], atm_strike, spot

    raise Exception(f"Could not find Nifty 50 {option_type} option near strike {atm_strike}")


def get_candle_data(obj, symbol_token, exchange="NFO", interval="FIVE_MINUTE", days=2):
    """Fetch historical candle data as DataFrame."""
    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

    params = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    data = obj.getCandleData(params)
    if data["status"] is False or not data.get("data"):
        logger.warning(f"No candle data: {data.get('message')}")
        return pd.DataFrame()

    df = pd.DataFrame(data["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    return df


def get_ltp(obj, exchange, symbol, token):
    """Get last traded price."""
    data = obj.ltpData(exchange, symbol, token)
    if data["status"] is False:
        raise Exception(f"LTP fetch failed: {data['message']}")
    return float(data["data"]["ltp"])


def place_buy_order(obj, symbol, token, qty, order_type="MARKET", price=0):
    """Place a buy order."""
    params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": order_type,
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(price),
        "quantity": str(qty),
    }
    response = obj.placeOrder(params)
    logger.info(f"BUY order placed: {symbol} qty={qty} | Response: {response}")
    return response


def place_sell_order(obj, symbol, token, qty, order_type="MARKET", price=0):
    """Place a sell order."""
    params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "SELL",
        "exchange": "NFO",
        "ordertype": order_type,
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(price),
        "quantity": str(qty),
    }
    response = obj.placeOrder(params)
    logger.info(f"SELL order placed: {symbol} qty={qty} | Response: {response}")
    return response


def get_positions(obj):
    """Get current open positions."""
    data = obj.position()
    if data["status"] is False:
        return []
    return data.get("data") or []


def square_off_all(obj):
    """Square off all open intraday positions."""
    positions = get_positions(obj)
    for pos in positions:
        net_qty = int(pos.get("netqty", 0))
        if net_qty == 0:
            continue
        symbol = pos["tradingsymbol"]
        token = pos["symboltoken"]
        if net_qty > 0:
            place_sell_order(obj, symbol, token, abs(net_qty))
        else:
            place_buy_order(obj, symbol, token, abs(net_qty))
        logger.info(f"Squared off {symbol} qty={net_qty}")
