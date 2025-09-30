import logging
import signal
import sys
import os
import threading
# Configure logging first, before importing other modules
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

# Set root logger level to ensure all modules log
logging.getLogger().setLevel(logging.INFO)

logger = logging.getLogger(__name__)
logger.info("Logging configured")

# /\/\/\/\/\/\/\ IMPORT & START SERVICES\/\/\/\/\/\/\/\/\/\
try:
    logger.info("Activating GCE")
    import gce
    logger.info("Activating K8s interface")
    import k8s
    logger.info("Activating tailscale provider")
    import tailscale_provider
    tailscale_provider.start()
    logger.info("Activating provisioner")
    import provisioner
    provisioner.start_auto_scaler()
    logger.info("Modules imported successfully")
except ImportError as e:
    logger.error(f"Module import failed: {e}")
    sys.exit(1)


# Wait for SIGTERM to gracefully shutdown
shutdown_event = threading.Event()
signal.signal(signal.SIGTERM, lambda s, f: shutdown_event.set())

logger.info("Waiting for termination signal...")
shutdown_event.wait()
logger.info("Received SIGTERM, shutting down")