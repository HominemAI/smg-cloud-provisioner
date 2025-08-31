from fastapi import FastAPI
import requests
import os
import base64
import sys

app = FastAPI()

TAILNET = os.getenv("TAILNET")
OAUTH_ID = os.getenv("TS_OAUTH_ID")
OAUTH_SECRET = os.getenv("TS_OAUTH_SECRET")
PORT = int(os.getenv("PORT", "80"))  # default to 80 if not set

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


@app.get("/newkey")
def new_key():
    url = f"https://api.tailscale.com/api/v2/tailnet/{TAILNET}/keys"
    auth = base64.b64encode(f"{OAUTH_ID}:{OAUTH_SECRET}".encode()).decode()

    payload = {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": True,
                    "tags": ["tag:gce"]
                }
            }
        },
        "expirySeconds": 3600
    }

    r = requests.post(url, headers={"Authorization": f"Basic {auth}"}, json=payload)
    r.raise_for_status()
    return r.json()
