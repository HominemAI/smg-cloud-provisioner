import logging
import os
import time

import k8s
from gce import GCEProvider

logger = logging.getLogger(__name__)
logger.info("Starting Kubernetes Provisioner")

# How often to check the gpu pressure
SCALER_INTERVAL = os.environ.get("SCALER_TIME_DELAY", "120")
MAX_NODES = os.environ.get("SCALER_MAX_NODES", "2")
MIN_NODES = os.environ.get("SCALER_MIN_NODES", "0")
SCALER_PROVISIONING_TIMEOUT = int(os.environ.get("SCALER_PROVISIONING_TIMEOUT", "300"))

# How long to wait before dropping a node after excess pressure is detected
SCALER_DROP_DELAY = os.environ.get("SCALER_DROP_DELAY", "300")

# Check for changes every 5 minutes
IS_PROVISIONING = False
IS_DEPROVISIONING = False

PROVIDERS = {
    "gce": GCEProvider(provider_name="gce")
}

PROVISIONING_TIME = time.time()


def get_provider() -> GCEProvider:
    return PROVIDERS["gce"]


def notify_creation_callback(instance_name: str):
    logger.info("Notifying creation callback")


def auto_scale():
    logger.info("Checking auto-scaling conditions...")
    global IS_PROVISIONING, IS_DEPROVISIONING, PROVISIONING_TIME, SCALER_PROVISIONING_TIMEOUT
    if k8s.get_current_node_count() < len(k8s.GPU_PODS):
        logger.info("Pressure detected: More GPU pods than nodes.")
        if not IS_PROVISIONING:
            logger.info("No provisioning in progress, starting new instance creation.")
            IS_PROVISIONING = True
            instance_name = get_provider().create_instance(callback=notify_creation_callback)
            logger.info(f"Instance creation triggered: {instance_name} startup and registration in progress...")
        else:
            logger.info(f"Last node time: {k8s.get_latest_node_time()} vs {PROVISIONING_TIME}")
            logger.info(f"{time.time() - PROVISIONING_TIME} vs {SCALER_PROVISIONING_TIMEOUT}")
            if k8s.get_latest_node_time() > PROVISIONING_TIME:
                IS_PROVISIONING = False
                PROVISIONING_TIME = time.time()
            elif time.time() - PROVISIONING_TIME > SCALER_PROVISIONING_TIMEOUT:
                IS_PROVISIONING = False
                raise RuntimeError("Provisioning timeout exceeded")
    elif k8s.get_current_node_count() > len(k8s.GPU_PODS):
        IS_DEPROVISIONING = True
        response = k8s.request_node_removal(1)
        if response:
            PROVIDERS[response["provider"]].delete_instance(response["identifier"])
        IS_DEPROVISIONING = False
    else:
        IS_PROVISIONING = False
        IS_DEPROVISIONING = False


def start_auto_scaler():
    global SCALER_INTERVAL
    logger.info(f"Starting auto-scaler with interval {SCALER_INTERVAL} seconds")
    while True:
        try:
            k8s.update_nodes_list()
            k8s.update_pods_lists()
            auto_scale()
        except Exception as e:
            logger.exception(f"Error in auto-scaler:")
        time.sleep(int(SCALER_INTERVAL))
