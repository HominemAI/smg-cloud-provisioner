# AI Disclosure: AI was used to flesh out call back logging
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

# How long to wait before removing an unready node (in seconds)
UNREADY_NODE_TIMEOUT = int(os.environ.get("UNREADY_NODE_TIMEOUT", "600"))

# Check for changes every 5 minutes
IS_PROVISIONING = False
IS_DEPROVISIONING = False

PROVIDERS = {
    "gce": GCEProvider(provider_name="gce")
}

PROVISIONING_TIME = time.time()


def get_provider() -> GCEProvider:
    return PROVIDERS["gce"]


def check_and_remove_unready_nodes():
    """
    Check all nodes managed by this provisioner and remove any that have been unready for longer than UNREADY_NODE_TIMEOUT.
    This checks all nodes with the instance identifier label, not just those in NODE_POOL.
    """
    logger.info("Checking for unready nodes...")

    # Get all nodes with our instance identifier label (not just those in NODE_POOL)
    all_nodes = k8s.v1.list_node(label_selector=f"{k8s.INSTANCE_IDENTIFIER_LABEL}")
    current_time = time.time()

    for node in all_nodes.items:
        # Determine if node is ready
        ready = False
        ready_status = "Unknown"
        if node.status and node.status.conditions:
            for condition in node.status.conditions:
                if condition.type == "Ready":
                    ready = condition.status == "True"
                    ready_status = condition.status
                    break

        if not ready:
            creation_timestamp = node.metadata.creation_timestamp
            age_seconds = current_time - creation_timestamp.timestamp()
            instance_identifier = node.metadata.labels.get(k8s.INSTANCE_IDENTIFIER_LABEL)

            logger.warning(f"Unready node detected: {node.metadata.name} (age: {age_seconds:.0f}s, ready status: {ready_status})")

            # Only remove if the node has been unready for longer than the timeout
            if age_seconds > UNREADY_NODE_TIMEOUT:
                if instance_identifier:
                    logger.info(f"Removing unready node (exceeded timeout of {UNREADY_NODE_TIMEOUT}s): {node.metadata.name} - {instance_identifier}")
                    k8s.deregister_k8s_node(instance_identifier)

                    # Delete the instance from the cloud provider
                    provider_name = node.metadata.labels.get(k8s.INSTANCE_PROVIDER_LABEL)
                    if provider_name and provider_name in PROVIDERS:
                        PROVIDERS[provider_name].delete_instance(
                            instance_identifier=instance_identifier,
                            callback=notify_deletion_callback
                        )
                else:
                    logger.warning(f"Node {node.metadata.name} missing instance identifier, skipping cloud deletion")
            else:
                remaining = UNREADY_NODE_TIMEOUT - age_seconds
                logger.info(f"Node {node.metadata.name} is unready but within timeout window ({remaining:.0f}s remaining)")


def notify_creation_callback(success: bool, instance_name: str, error: Exception):
    global IS_PROVISIONING
    if success:
        logger.info(f"Instance {instance_name} created successfully")
    else:
        logger.error(f"Instance {instance_name} creation failed: {error}")
        IS_PROVISIONING = False

def notify_deletion_callback(success: bool, instance_name: str, error: Exception):
    global IS_DEPROVISIONING
    if success:
        logger.info(f"Instance {instance_name} deleted successfully")
    else:
        logger.error(f"Instance {instance_name} deletion failed: {error}")
    IS_DEPROVISIONING = False

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
            PROVIDERS[response["provider"]].delete_instance(instance_identifier=response["identifier"], callback=notify_deletion_callback)
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
            check_and_remove_unready_nodes()
            k8s.update_pods_lists()
            auto_scale()
        except Exception as e:
            logger.exception(f"Error in auto-scaler:")
        time.sleep(int(SCALER_INTERVAL))
