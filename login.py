import pyotp
import os
from dotenv import load_dotenv
from SmartApi import SmartConnect
import logzero
from logzero import logger

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")


def get_totp():
    return pyotp.TOTP(TOTP_SECRET).now()


def login():
    obj = SmartConnect(api_key=API_KEY)
    totp = get_totp()
    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)

    if data["status"] is False:
        logger.error(f"Login failed: {data['message']}")
        raise Exception(f"Login failed: {data['message']}")

    auth_token = data["data"]["jwtToken"]
    refresh_token = data["data"]["refreshToken"]
    feed_token = obj.getfeedToken()

    logger.info(f"Login successful for {CLIENT_ID}")
    return obj, auth_token, feed_token, refresh_token


if __name__ == "__main__":
    obj, auth, feed, refresh = login()
    profile = obj.getProfile(refresh)
    print(f"Logged in as: {profile['data']['name']}")
    print(f"Exchanges: {profile['data']['exchanges']}")
