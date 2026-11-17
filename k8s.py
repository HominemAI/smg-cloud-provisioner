# AI disclosure: AI was used to grunt out the removal of terminated pods from tracking
import asyncio
import logging
import os
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

v1 = None


def initialize():
    global v1
    config.load_incluster_config()
    v1 = client.CoreV1Api()

NODE_POOL = AsyncSet()
NODE_POOLS = {NODE_ROLE_LABEL_VALUE: NODE_POOL}
ALL_NODES = []
GPU_PODS = AsyncSet()
UNSCHEDULED_GPU_PODS = AsyncSet()

LATEST_NODE_TIME = time.time()


def get_current_node_count() -> int:
    return NODE_POOL.size()


def get_current_gpu_pod_count(role=None) -> int:
    if role is None:
        return GPU_PODS.size()
    # Pending unqualified workloads remain in the original pool. Once scheduled,
    # count against the actual node pool, even when no selector was supplied.
    node_roles = {node.metadata.name: (node.metadata.labels or {}).get(NODE_ROLE_LABEL)
                  for node in ALL_NODES}
    count = 0
    for pod in GPU_PODS.to_list():
        selected = (getattr(pod.spec, 'node_selector', None) or {}).get(NODE_ROLE_LABEL)
        assigned = node_roles.get(getattr(pod.spec, 'node_name', None))
        if (assigned or selected or NODE_ROLE_LABEL_VALUE) == role:
            count += 1
    return count


def get_node_details() -> List[dict]:
    node_details = []
    current_time = time.time()
    for node in NODE_POOL.to_list():
        creation_timestamp = node.metadata.creation_timestamp
        age_seconds = current_time - creation_timestamp.timestamp()

        # Determine if node is ready
        ready = False
        if node.status and node.status.conditions:
            for condition in node.status.conditions:
                if condition.type == "Ready":
                    ready = condition.status == "True"
                    break

        node_details.append({
            "name": node.metadata.name,
            "provider": node.metadata.labels.get(INSTANCE_PROVIDER_LABEL),
            "instance_identifier": node.metadata.labels.get(INSTANCE_IDENTIFIER_LABEL),
            "role": node.metadata.labels.get(NODE_ROLE_LABEL),
            "creation_timestamp": creation_timestamp,
            "age_seconds": age_seconds,
            "ready": ready,
            "labels": node.metadata.labels
        })

    return node_details


def get_all_active_pods() -> List:
    pods = v1.list_pod_for_all_namespaces(
        field_selector="metadata.namespace!=kube-system,metadata.namespace!=kube-public",
        watch=False).items
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

    # Rebuild sets from scratch using current API data
    new_gpu_pods = AsyncSet()
    new_unscheduled_gpu_pods = AsyncSet()

    for pod in pods:
        # Skip terminated pods (Succeeded or Failed)
        if pod.status.phase in ("Succeeded", "Failed"):
            logger.debug(f"Skipping terminated pod: {pod.metadata.name} (phase: {pod.status.phase})")
            continue

        total_gpus = pod_gpu_count(pod)
        if total_gpus > 0:
            new_gpu_pods.add(pod)
            logger.debug(f"Active GPU pod: {pod.metadata.name} in namespace {pod.metadata.namespace}")
            node_name = getattr(pod.spec, 'node_name', None)
            if not node_name:
                new_unscheduled_gpu_pods.add(pod)

    # Log what changed
    old_gpu_count = GPU_PODS.size()
    new_gpu_count = new_gpu_pods.size()
    if old_gpu_count != new_gpu_count:
        logger.info(f"GPU pod count changed: {old_gpu_count} -> {new_gpu_count}")

    # Replace the global sets with fresh data
    GPU_PODS = new_gpu_pods
    UNSCHEDULED_GPU_PODS = new_unscheduled_gpu_pods


def add_node_to_pool(node: object, source="watch") -> None:
    global NODE_POOL, LATEST_NODE_TIME
    if node not in NODE_POOL:
        NODE_POOL.add(node)
        LATEST_NODE_TIME = time.time()  # New node detected
        logger.debug(f"New node added from {source}: {node.metadata.name}")


def remove_node_from_pool(node: object) -> None:
    global NODE_POOL
    if node in NODE_POOL:
        NODE_POOL.remove(node)
        logger.info(f"Node removed: {node.metadata.name}")


def get_latest_node_time() -> float:
    global LATEST_NODE_TIME
    return LATEST_NODE_TIME


def update_nodes_list():
    global LATEST_NODE_TIME, NODE_POOL, NODE_POOLS, ALL_NODES
    nodes = v1.list_node(_request_timeout=60).items
    pools = {}
    for node in nodes:
        labels = node.metadata.labels or {}
        role = labels.get(NODE_ROLE_LABEL)
        if role:
            pools.setdefault(role, AsyncSet()).add(node)
    gpu = pools.setdefault(NODE_ROLE_LABEL_VALUE, AsyncSet())
    old_ids = {node.metadata.uid for node in NODE_POOL.to_list()}
    if any(node.metadata.uid not in old_ids for node in gpu.to_list()):
        LATEST_NODE_TIME = time.time()
    NODE_POOLS, NODE_POOL, ALL_NODES = pools, gpu, nodes


def pool_nodes(role):
    pool = NODE_POOLS.get(role)
    return pool.to_list() if pool is not None else []


def node_ready(node):
    return any(c.type == "Ready" and c.status == "True"
               for c in (node.status.conditions or [])) if node.status else False


def available_worker_count():
    return sum(
        node_ready(node) and not node.spec.unschedulable
        and "node-role.kubernetes.io/control-plane" not in (node.metadata.labels or {})
        and "node-role.kubernetes.io/master" not in (node.metadata.labels or {})
        and (node.metadata.labels or {}).get("smg-cloud-provisioner") != "true"
        for node in ALL_NODES
    )


def drain_node(node):
    """Make one drain pass; return true only after evictable pods are gone."""
    name = node.metadata.name
    v1.patch_node(name, {"spec": {"unschedulable": True}})
    pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={name}").items
    pending = False
    for pod in pods:
        if any(owner.kind == "DaemonSet" for owner in (pod.metadata.owner_references or [])):
            continue
        if (pod.metadata.annotations or {}).get("kubernetes.io/config.mirror"):
            continue
        if pod.status.phase in ("Succeeded", "Failed"):
            continue
        pending = True
        for volume in pod.spec.volumes or []:
            if volume.persistent_volume_claim:
                logger.warning("Draining %s: claim %s/%s may contain node-local data that teardown destroys",
                               name, pod.metadata.namespace, volume.persistent_volume_claim.claim_name)
        if pod.metadata.deletion_timestamp:
            continue
        try:
            v1.create_namespaced_pod_eviction(
                pod.metadata.name, pod.metadata.namespace,
                client.V1Eviction(metadata=client.V1ObjectMeta(
                    name=pod.metadata.name, namespace=pod.metadata.namespace,
                    ), delete_options=client.V1DeleteOptions(
                        preconditions=client.V1Preconditions(uid=pod.metadata.uid))),
            )
        except client.exceptions.ApiException as error:
            if error.status not in (404, 429):
                raise
            logger.info("Eviction pending for %s/%s: HTTP %s", pod.metadata.namespace,
                        pod.metadata.name, error.status)
    return not pending


def request_node_removal(qty: int = 1) -> dict | bool:
    global NODE_POOL
    pods: list = get_all_active_pods()
    for node in NODE_POOL.to_list():
        node_pods = [pod for pod in pods if pod.spec.node_name and pod.spec.node_name == node.metadata.name]
        if node_pods:
            continue
        remove_node_from_pool(node)
        deregister_k8s_node(node.metadata.labels.get(INSTANCE_IDENTIFIER_LABEL))
        logger.info(
            f"Node removed: {node.metadata.name} - {node.metadata.labels.get(INSTANCE_PROVIDER_LABEL)} = {node.metadata.labels.get(INSTANCE_IDENTIFIER_LABEL)}")
        return {
            "provider": node.metadata.labels.get(INSTANCE_PROVIDER_LABEL),
            "identifier": node.metadata.labels.get(INSTANCE_IDENTIFIER_LABEL)
        }
    logger.info(f"Requesting node removal failed. - No idle nodes available.")
    return False
