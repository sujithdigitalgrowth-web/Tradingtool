import pyotp
import os
import time
from dotenv import load_dotenv
from SmartApi import SmartConnect
from logzero import logger

load_dotenv()


def _get_creds():
    """Return credentials, raising a clear error if any env var is missing."""
    missing = [k for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID",
                            "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")
               if not os.getenv(k)]
    if missing:
        raise EnvironmentError(
            f"Missing Railway env vars: {', '.join(missing)}. "
            "Set them in Railway → Variables."
        )
    return (os.getenv("ANGEL_API_KEY"),
            os.getenv("ANGEL_CLIENT_ID"),
            os.getenv("ANGEL_PASSWORD"),
            os.getenv("ANGEL_TOTP_SECRET"))


def login(retries: int = 3):
    """
    Login to Angel One Smart API.
    Retries up to `retries` times with a 5-second delay between attempts.
    Tries current TOTP window first, then previous window (handles clock drift).
    """
    api_key, client_id, password, totp_secret = _get_creds()

    last_err = None
    for attempt in range(1, retries + 1):
        # Try current TOTP window, then previous (Railway clock may drift ±30s)
        totp_obj = pyotp.TOTP(totp_secret)
        totp_codes = [totp_obj.now(),
                      totp_obj.at(int(time.time()) - 30)]

        for totp in totp_codes:
            try:
                obj  = SmartConnect(api_key=api_key)
                data = obj.generateSession(client_id, password, totp)

                if not isinstance(data, dict):
                    raise ValueError(
                        f"Unexpected response type ({type(data).__name__}). "
                        "Angel One may be returning an HTML page — check if "
                        "Railway's IP is blocked or Angel One API is down."
                    )

                if data.get("status") is False:
                    msg = data.get("message", "Unknown error")
                    raise Exception(f"Angel One rejected login: {msg}")

                auth_token    = data["data"]["jwtToken"]
                refresh_token = data["data"]["refreshToken"]
                feed_token    = obj.getfeedToken()

                logger.info(f"Login successful for {client_id} (attempt {attempt})")
                return obj, auth_token, feed_token, refresh_token

            except Exception as e:
                last_err = e
                logger.warning(f"Login attempt {attempt} failed: {e}")

        if attempt < retries:
            time.sleep(5)

    raise Exception(f"Login failed after {retries} attempts. Last error: {last_err}")


if __name__ == "__main__":
    obj, auth, feed, refresh = login()
    profile = obj.getProfile(refresh)
    print(f"Logged in as: {profile['data']['name']}")
    print(f"Exchanges: {profile['data']['exchanges']}")
