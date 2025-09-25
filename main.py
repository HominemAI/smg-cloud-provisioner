import logging
import sys
import os
from fastapi import FastAPI

# Force Python to use unbuffered output
os.environ['PYTHONUNBUFFERED'] = '1'

# Force unbuffered output immediately
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', 0)

# Print directly to stdout first
print("STARTUP: Application initializing...", flush=True)
sys.stdout.flush()

# Configure logging with more aggressive settings
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

# Test logging immediately
logger.info("STARTUP: Logger configured")
print("STARTUP: Print statement after logger", flush=True)
sys.stdout.flush()

app = FastAPI()

# Add health check first
@app.get("/health")
def health_check():
    logger.info("Health check called")
    return {"status": "healthy", "message": "Service is running"}

# Try to import modules with error handling
print("STARTUP: Importing modules...", flush=True)
try:
    import gce
    logger.info("Successfully imported gce module")
except Exception as e:
    logger.error(f"Failed to import gce: {e}")
    print(f"ERROR importing gce: {e}", flush=True)

try:
    import k8s
    logger.info("Successfully imported k8s module")
except Exception as e:
    logger.error(f"Failed to import k8s: {e}")
    print(f"ERROR importing k8s: {e}", flush=True)

try:
    import tailscale_provider
    logger.info("Successfully imported tailscale_provider module")
except Exception as e:
    logger.error(f"Failed to import tailscale_provider: {e}")
    print(f"ERROR importing tailscale_provider: {e}", flush=True)

print("STARTUP: Modules imported", flush=True)

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
