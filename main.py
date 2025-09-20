from fastapi import FastAPI
import os
import sys
import gce
import k8s
import tailscale_provider
prin("test")
app = FastAPI()

K3S_KEY = os.getenv("K3S_KEY")
PORT = int(os.getenv("PORT", "80"))

missing = []

if not K3S_KEY:
    missing.append("K3S_KEY")

if missing:
    sys.stderr.write(f"Missing required environment variables: {', '.join(missing)}\n")
    sys.exit(1)


@app.get("/instance/new")
def new_instance():
    key_response = tailscale_provider.request_key()
    if "error" in key_response:
        return key_response

    auth_key = key_response.get("key")
    if not auth_key:
        return {"error": "No key in response", "response": key_response}

    try:
        instance_name = gce.create_instance_from_template()
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

if __name__ == '__main__':
    print("start")
    tailscale_provider.start()
    print("something")
