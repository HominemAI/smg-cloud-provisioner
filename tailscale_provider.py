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

    # Sets the env var for use by startup script parsing
    os.environ["TS_AUTHKEY"] = data["access_token"]

    logger.info("Fetched new access token, expires at " + time.ctime(TOKEN_CACHE["expires_at"]))
    return TOKEN_CACHE["access_token"]


def request_key():
    global TOKEN_CACHE
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
    return r.json()


def token_refresh():
    while True:
        get_access_token()
        if TOKEN_CACHE["access_token"]:
            _ready.set()
        time.sleep(TS_KEY_EXPIRY)


def start():
    thread = threading.Thread(target=token_refresh, daemon=True)
    thread.start()
    logger.info("Waiting for Tailscale to be ready...")
    _ready.wait()
    logger.info("Tailscale provider ready")

