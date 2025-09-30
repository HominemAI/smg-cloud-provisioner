import logging
import os
import sys
import threading
import time

import requests

logger = logging.getLogger(__name__)
_ready = threading.Event()

TAILNET = os.getenv("TAILNET")
OAUTH_ID = os.getenv("TS_OAUTH_ID")
OAUTH_SECRET = os.getenv("TS_OAUTH_SECRET")

TS_KEY_EXPIRY = 3600  # seconds

missing = []

if not TAILNET:
    missing.append("TAILNET")
if not OAUTH_ID:
    missing.append("TS_OAUTH_ID")
if not OAUTH_SECRET:
    missing.append("TS_OAUTH_SECRET")

if missing:
    sys.stderr.write(f"Missing required environment variables: {', '.join(missing)}\n")
    sys.exit(1)

TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0
}

KEY_CACHE = {
    "auth_key": None,
    "expires_at": 0
}


def get_access_token():
    global TOKEN_CACHE, TS_KEY_EXPIRY
    now = int(time.time())
    # refresh 30s before actual expiry
    if TOKEN_CACHE["access_token"] and TOKEN_CACHE["expires_at"] > now + 30:
        return TOKEN_CACHE["access_token"]

    token_url = "https://api.tailscale.com/api/v2/oauth/token"
    r = requests.post(token_url, data={
        "client_id": OAUTH_ID,
        "client_secret": OAUTH_SECRET
    })

    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        raise RuntimeError(f"Failed to fetch token: {r.status_code} {r.text}")

    data = r.json()
    TOKEN_CACHE["access_token"] = data["access_token"]
    TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in", TS_KEY_EXPIRY))

    logger.info("Fetched new access token, expires at " + time.ctime(TOKEN_CACHE["expires_at"]))
    return TOKEN_CACHE["access_token"]


def request_key():
    global KEY_CACHE
    now = int(time.time())

    # Check if we have a valid cached key
    if KEY_CACHE["auth_key"] and KEY_CACHE["expires_at"] > now + 30:
        return {"key": KEY_CACHE["auth_key"]}

    access_token = get_access_token()

    url = f"https://api.tailscale.com/api/v2/tailnet/{TAILNET}/keys"
    payload = {
        "capabilities": {
            "devices": {
                "create": {
                    "ephemeral": True,
                    "reusable": False,
                    "tags": ["tag:temp-instance", "tag:prod"]
                }
            }
        },
        "expirySeconds": TS_KEY_EXPIRY
    }

    r = requests.post(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }, json=payload)

    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        return {"error": r.status_code, "response": r.text}

    logger.info("Requested new Tailscale Auth Key")
    data = r.json()

    # Cache the key
    KEY_CACHE["auth_key"] = data.get("key", "")
    KEY_CACHE["expires_at"] = now + TS_KEY_EXPIRY

    # Set env var for startup script parsing
    os.environ["TS_AUTHKEY"] = KEY_CACHE["auth_key"]

    return data


def token_refresh():
    while True:
        get_access_token()
        if TOKEN_CACHE["access_token"]:
            # Now request the actual Tailscale auth key
            key_response = request_key()
            if "key" in key_response:  # Success
                _ready.set()
        time.sleep(TS_KEY_EXPIRY)


def start():
    thread = threading.Thread(target=token_refresh, daemon=True)
    thread.start()
    logger.info("Waiting for Tailscale to be ready...")
    _ready.wait()
    logger.info("Tailscale provider ready")

