import logging
import os
import time

import k8s
import gce
import tailscale

logger = logging.getLogger(__name__)

# How often to check the gpu pressure
SCALER_INTERVAL = os.environ.get("SCALER_TIME_DELAY", "60")
MAX_NODES = os.environ.get("SCALER_MAX_NODES", "2")
MIN_NODES = os.environ.get("SCALER_MIN_NODES", "0")
SCALER_PROVISIONING_TIMEOUT = int(os.environ.get("SCALER_PROVISIONING_TIMEOUT", "300"))

# How long to wait before dropping a node after excess pressure is detected
SCALER_DROP_DELAY = os.environ.get("SCALER_DROP_DELAY", "300")

# Check for changes every 5 minutes
IS_PROVISIONING = False
IS_DEPROVISIONING = False

PROVISIONING_TIME = time.time()

def notify_creation_callback(instance_name: str):
    logger.info("Notifying creation callback")


def auto_scale():
    global IS_PROVISIONING, IS_DEPROVISIONING, PROVISIONING_TIME, SCALER_PROVISIONING_TIMEOUT
    if k8s.get_current_node_count() < len(k8s.GPU_PODS):
        if not IS_PROVISIONING:
            IS_PROVISIONING = True
            key_response = tailscale.request_key()
            if "error" in key_response:
                IS_PROVISIONING = False
                raise RuntimeError(f"Could not generate Tailscale key: {key_response}")
            auth_key = key_response.get("key")
            if not auth_key:
                IS_PROVISIONING = False
                raise RuntimeError(f"Could not generate Tailscale key: {key_response}")
            instance_name = gce.create_instance_from_template(auth_key, os.getenv("K3S_KEY", ""), callback=notify_creation_callback)
            logger.info(f"Instance creation triggered: {instance_name} startup and registration in progress...")
        else:
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
            gce.shutdown_instance(response["instance_identifier"])
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
            logger.error(f"Error in auto-scaler: {e}")
        time.sleep(int(SCALER_INTERVAL))