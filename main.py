import logging
import sys
import os
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

if __name__ == "__main__":
    try:
        logger.info("STARTUP: Application started successfully")
        print("STARTUP: Application started successfully", flush=True)
    except Exception as e:
        logger.error(f"Application failed to start: {e}")
        print(f"ERROR during startup: {e}", flush=True)
        sys.exit(1)