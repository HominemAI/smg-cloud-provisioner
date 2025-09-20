import logging
import sys
from fastapi import FastAPI
import os
import gce
import k8s
import tailscale_provider

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

logger.info("Application starting...")

app = FastAPI()

K3S_KEY = os.getenv("K3S_KEY")
PORT = int(os.getenv("PORT", "80"))

missing = []

if not K3S_KEY:
    missing.append("K3S_KEY")

if missing:
    logger.error(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)


@app.get("/instance/new")
def new_instance():
    logger.info("Creating new instance...")
    key_response = tailscale_provider.request_key()
    if "error" in key_response:
        logger.error(f"Failed to get tailscale key: {key_response}")
        return key_response

    auth_key = key_response.get("key")
    if not auth_key:
        logger.error(f"No key in response: {key_response}")
        return {"error": "No key in response", "response": key_response}

    try:
        instance_name = gce.create_instance_from_template()
        logger.info(f"Created instance: {instance_name}")
        return {"instance": instance_name}
    except Exception as e:
        logger.error(f"Failed to create instance: {str(e)}")
        return {"error": str(e)}


@app.get("/instance/delete/{instance_name}")
def delete_instance(instance_name: str):
    logger.info(f"Deleting instance: {instance_name}")
    try:
        gce.shutdown_instance(instance_name)
        k8s.deregister_k8s_node(instance_name)
        logger.info(f"Successfully deleted instance: {instance_name}")
        return {"instance": instance_name, "status": "deleted"}
    except Exception as e:
        logger.error(f"Failed to delete instance {instance_name}: {str(e)}")
        return {"error": str(e)}

if __name__ == '__main__':
    logger.info("Starting application...")
    tailscale_provider.start()
    logger.info("Tailscale provider started")
    logger.info("Application ready")
