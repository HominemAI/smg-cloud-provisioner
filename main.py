from fastapi import FastAPI
import requests
import os
import base64
import sys
import time
import gce
import k8s

app = FastAPI()

TAILNET = os.getenv("TAILNET")
OAUTH_ID = os.getenv("TS_OAUTH_ID")
OAUTH_SECRET = os.getenv("TS_OAUTH_SECRET")
K3S_KEY = os.getenv("K3S_KEY")
PORT = int(os.getenv("PORT", "80"))

missing = []
if not TAILNET:
    missing.append("TAILNET")
if not OAUTH_ID:
    missing.append("TS_OAUTH_ID")
if not OAUTH_SECRET:
    missing.append("TS_OAUTH_SECRET")
if not OAUTH_SECRET:
    missing.append("K3S_KEY")

if missing:
    sys.stderr.write(f"Missing required environment variables: {', '.join(missing)}\n")
    sys.exit(1)

# cache for the OAuth token
token_cache = {
    "access_token": None,
    "expires_at": 0
}


def get_access_token():
    """Fetch and cache a bearer token from Tailscale OAuth"""
    now = int(time.time())
    # refresh 30s before actual expiry
    if token_cache["access_token"] and token_cache["expires_at"] > now + 30:
        return token_cache["access_token"]

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
    token_cache["access_token"] = data["access_token"]
    token_cache["expires_at"] = now + int(data.get("expires_in", 3600))

    return token_cache["access_token"]


@app.get("/newkey")
def new_key():
    access_token = get_access_token()

    url = f"https://api.tailscale.com/api/v2/tailnet/{TAILNET}/keys"
    payload = {
        "capabilities": {
            "devices": {
                "create": {
                    "ephemeral": True,
                    "reusable": False,
                    # both tags required by your tailnet policy
                    "tags": ["tag:temp-instance", "tag:prod"]
                }
            }
        },
        "expirySeconds": 3600
    }

    r = requests.post(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }, json=payload)

    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        return {"error": r.status_code, "response": r.text}

    return r.json()

@app.get("/instance/new")
def new_instance():
    key_response = new_key()
    if "error" in key_response:
        return key_response

    auth_key = key_response.get("key")
    if not auth_key:
        return {"error": "No key in response", "response": key_response}

    try:
        instance_name = gce.create_instance_from_template(auth_key, K3S_KEY)
        return {"instance": instance_name}
    except Exception as e:
        return {"error": str(e)}

@app.get("/instance/delete/{instance_name}")
def delete_instance(instance_name: str):
    try:
        gce.shutdown_instance(instance_name)
        k8s.deregister_k8s_node(instance_name)
        return {"instance": instance_name, "status": "deleted"}
    except Exception as e:
        return {"error": str(e)}

