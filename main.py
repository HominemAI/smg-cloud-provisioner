from fastapi import FastAPI
import requests
import os
import base64
import sys

app = FastAPI()

TAILNET = os.getenv("TAILNET")
OAUTH_ID = os.getenv("TS_OAUTH_ID")
OAUTH_SECRET = os.getenv("TS_OAUTH_SECRET")
PORT = int(os.getenv("PORT", "80"))

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
                    "ephemeral": True,
                    "reusable": False,
                    "tags": ["tag:temp-instance", "tag:prod"]
                }
            }
        },
        "expirySeconds": 3600
    }

    r = requests.post(url, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }, json=payload)

    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        # return full error message from Tailscale
        return {"error": r.status_code, "response": r.text}

    return r.json()
