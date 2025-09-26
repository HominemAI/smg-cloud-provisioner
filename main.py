import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

try:
    import gce
    import k8s
    import tailscale_provider
    logger.info("Modules imported successfully")
except ImportError as e:
    logger.error(f"Module import failed: {e}")
    sys.exit(1)

K3S_KEY = os.getenv("K3S_KEY")
PORT = int(os.getenv("PORT", "80"))

missing = []

if not K3S_KEY:
    missing.append("K3S_KEY")

if missing:
    logger.error(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

if __name__ == "__main__":
    logger.info("Application started successfully")