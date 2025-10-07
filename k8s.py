# AI disclosure: AI was used to grunt out the removal of terminated pods from tracking
import asyncio
import logging
import os
import signal
import threading
import time
from typing import List
from kubernetes import client, config, watch

from urllib3.exceptions import ReadTimeoutError
from async_tools import AsyncSet

logger = logging.getLogger(__name__)

os.environ.setdefault('NODE_ROLE_LABEL', "smgp-role")
os.environ.setdefault('NODE_ROLE_LABEL_VALUE', "gpu")
os.environ.setdefault('INSTANCE_PROVIDER_LABEL', "smgp-instance-provider")
os.environ.setdefault('INSTANCE_IDENTIFIER_LABEL', "smgp-instance-identifier")

NODE_ROLE_LABEL = os.environ['NODE_ROLE_LABEL']
NODE_ROLE_LABEL_VALUE = os.environ['NODE_ROLE_LABEL_VALUE']
INSTANCE_PROVIDER_LABEL = os.environ['INSTANCE_PROVIDER_LABEL']
INSTANCE_IDENTIFIER_LABEL = os.environ['INSTANCE_IDENTIFIER_LABEL']

config.load_incluster_config()

v1 = client.CoreV1Api()

NODE_POOL = AsyncSet()
GPU_PODS = AsyncSet()
UNSCHEDULED_GPU_PODS = AsyncSet()

LATEST_NODE_TIME = time.time()


def get_current_node_count() -> int:
    logger.info(len(NODE_POOL.to_list()))
    logger.info(NODE_POOL.to_list())
    return NODE_POOL.size()


def get_current_gpu_pod_count() -> int:
    return GPU_PODS.size()


def get_all_active_pods() -> List:
    pods = v1.list_pod_for_all_namespaces(
        field_selector=f"metadata.namespace notin (kube-system,kube-public), watch=False").items
    active_pods = []
    for pod in pods:
        if pod.spec.node_name and pod.metadata.owner_references:
            for owner in pod.metadata.owner_references:
                if owner.kind != "DaemonSet":
                    active_pods.append(pod)
    return active_pods


def deregister_k8s_node(instance_identifier: str):
    logger.info(f"Deregistering instance from k8s cluster - {instance_identifier}")
    nodes = v1.list_node(label_selector=f"{INSTANCE_IDENTIFIER_LABEL}={instance_identifier}")
    for node in nodes.items:
        node_name = node.metadata.name
        logger.info(f"Deleting k8s node {node_name}")
        v1.delete_node(name=node_name)


def pod_gpu_count(pod: object) -> int:
    added = sum(
        int(c.resources.limits.get("nvidia.com/gpu", 0)) for c in pod.spec.containers
        if c.resources and c.resources.limits
    )
    return added


def on_pod_added(pod: object) -> None:
    global UNSCHEDULED_GPU_PODS, GPU_PODS
    if pod.status.phase in ("Succeeded", "Failed"):
        logger.info(f"Skipping terminated pod: {pod.metadata.name} in namespace {pod.metadata.namespace}")
        return

    gpu_count = pod_gpu_count(pod)
    if gpu_count > 0:
        logger.info(f"GPU pod detected: pod {pod.metadata.name} requests {gpu_count} GPUs")
        if not pod.spec.node_name and pod not in UNSCHEDULED_GPU_PODS:
            UNSCHEDULED_GPU_PODS.add(pod)
            GPU_PODS.add(pod)
            logger.info(f"Pod added: {pod.metadata.name} in namespace {pod.metadata.namespace}")


def on_pod_modified(pod: object) -> None:
    global GPU_PODS, UNSCHEDULED_GPU_PODS
    # If pod transitioned to terminal state, remove it from tracking
    if pod.status.phase in ("Succeeded", "Failed"):
        if pod in UNSCHEDULED_GPU_PODS:
            UNSCHEDULED_GPU_PODS.remove(pod)
            logger.info(f"Pod terminated (unscheduled): {pod.metadata.name} in namespace {pod.metadata.namespace}")
        if pod in GPU_PODS:
            GPU_PODS.remove(pod)
            logger.info(f"Pod terminated: {pod.metadata.name} in namespace {pod.metadata.namespace}")


def on_pod_deleted(pod: object) -> None:
    global GPU_PODS, UNSCHEDULED_GPU_PODS
    gpu_count = pod_gpu_count(pod)
    if gpu_count > 0:
        if pod in UNSCHEDULED_GPU_PODS:
            UNSCHEDULED_GPU_PODS.remove(pod)
            logger.info(f"Pod deleted: {pod.metadata.name} in namespace {pod.metadata.namespace}")
        if pod in GPU_PODS:
            GPU_PODS.remove(pod)


def update_pods_lists():
    logger.info("Updating pods lists.")
    global UNSCHEDULED_GPU_PODS, GPU_PODS
    pods = v1.list_pod_for_all_namespaces(_request_timeout=60, watch=False).items

    # First pass: remove terminated pods from tracking
    for pod in GPU_PODS.to_list():
        if pod.status.phase in ("Succeeded", "Failed"):
            if pod in UNSCHEDULED_GPU_PODS:
                UNSCHEDULED_GPU_PODS.remove(pod)
            GPU_PODS.remove(pod)
            logger.info(f"Removing terminated pod from tracking: {pod.metadata.name}")

    # Second pass: add/update active GPU pods
    for pod in pods:
        # Skip terminated pods (Succeeded or Failed)
        if pod.status.phase in ("Succeeded", "Failed"):
            continue

        total_gpus = pod_gpu_count(pod)
        if total_gpus > 0:
            GPU_PODS.add(pod)
            logger.info(f"Pod added: {pod.metadata.name} in namespace {pod.metadata.namespace}")
            node_name = getattr(pod.spec, 'node_name', None)
            if not node_name and pod not in UNSCHEDULED_GPU_PODS:
                UNSCHEDULED_GPU_PODS.add(pod)
            elif node_name and pod in UNSCHEDULED_GPU_PODS:
                UNSCHEDULED_GPU_PODS.remove(pod)
    UNSCHEDULED_GPU_PODS = UNSCHEDULED_GPU_PODS - pods


def add_node_to_pool(node: object, source="watch") -> None:
    global NODE_POOL, LATEST_NODE_TIME
    if node not in NODE_POOL:
        NODE_POOL.add(node)
        LATEST_NODE_TIME = time.time()  # New node detected
        logger.info(f"New node added from {source}: {node.metadata.name}")


def remove_node_from_pool(node: object) -> None:
    global NODE_POOL
    if node in NODE_POOL:
        NODE_POOL.remove(node)
        logger.info(f"Node removed: {node.metadata.name}")


def get_latest_node_time() -> float:
    global LATEST_NODE_TIME
    return LATEST_NODE_TIME


def update_nodes_list():
    logger.info(f"Updating nodes list.")
    global LATEST_NODE_TIME, NODE_POOL
    nodes = v1.list_node(label_selector=f"{NODE_ROLE_LABEL}={NODE_ROLE_LABEL_VALUE}", _request_timeout=60).items
    for node in nodes:
        if len(nodes) > NODE_POOL.size():
            LATEST_NODE_TIME = time.time()
        NODE_POOL.add(node)
        logger.info(f"Node added: {node.metadata.name}")


def request_node_removal(qty: int = 1) -> dict | bool:
    global NODE_POOL
    pods: list = get_all_active_pods()
    for node in NODE_POOL.to_list():
        node_pods = [pod for pod in pods if pod.spec.node_name and pod.spec.node_name == node.metadata.name]
        if node_pods:
            continue
        remove_node_from_pool(node)
        return {
            "provider": node.metadata.labels.get(INSTANCE_PROVIDER_LABEL),
            "identifier": node.metadata.labels.get(INSTANCE_IDENTIFIER_LABEL)
        }
    return False


resp = v1.list_pod_for_all_namespaces(watch=False)
resource_version = resp.metadata.resource_version
w = watch.Watch()


async def pods():
    count = 0
    try:
        for event in w.stream(v1.list_pod_for_all_namespaces, resource_version=resource_version, _request_timeout=60):
            match event['type']:
                case 'ADDED':
                    logger.debug("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    on_pod_added(event['object'])
                case 'MODIFIED':
                    logger.debug("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    on_pod_modified(event['object'])
                case 'DELETED':
                    logger.debug("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    on_pod_deleted(event['object'])
                case _:
                    continue
            await asyncio.sleep(0)
            if count < 10:
                count += 1
                logger.debug(str(event))
            else:
                break
    except ReadTimeoutError:
        await asyncio.sleep(0)


async def nodes():
    count = 0
    try:
        for event in w.stream(v1.list_node, resource_version=resource_version, _request_timeout=60):
            match event['type']:
                case 'DELETED':
                    logger.info("Event: %s %s" % (event['type'], event['object'].metadata.name))

                case 'ADDED':
                    logger.info("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    logger.info(f"{event['object'].metadata.labels}")
                    if event['object'].metadata.labels.get(NODE_ROLE_LABEL) == NODE_ROLE_LABEL_VALUE:
                        add_node_to_pool(event['object'])
                case _:
                    continue
            await asyncio.sleep(0)
            if count < 10:
                count += 1
                logger.debug(str(event))
            else:
                break
    except ReadTimeoutError:
        await asyncio.sleep(0)


def shutdown(loop: asyncio.AbstractEventLoop):
    logger.info("Shutting down...")
    for task in asyncio.all_tasks(loop):
        task.cancel()
        loop.stop()


def run_loop():
    new_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(new_loop)
        new_loop.create_task(pods())
        new_loop.create_task(nodes())
        new_loop.run_forever()
    except Exception as e:
        shutdown(new_loop)
        raise e


try:
    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
except Exception as e:
    logger.error(f"Error starting k8s watcher threads: {e}")
    raise e
