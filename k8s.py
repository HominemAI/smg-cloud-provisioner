import os
from typing import List
from kubernetes import client, config, watch
import asyncio

from urllib3.exceptions import ReadTimeoutError
from async_tools import AsyncSet

config.load_kube_config()
import signal

v1 = client.CoreV1Api()

NODE_POOL = AsyncSet()
GPU_PODS = AsyncSet()
UNSCHEDULED_GPU_PODS = set()

NODE_ROLE_LABEL = os.getenv('NODE_OWNER_LABEL', "smgp-role")
NODE_ROLE_LABEL_VALUE = os.getenv('NODE_ROLE_LABEL_VALUE', "gpu")
INSTANCE_PROVIDER_LABEL = os.getenv('INSTANCE_PROVIDER_LABEL', "smgp-instance-provider")
INSTANCE_IDENTIFIER_LABEL = os.getenv('INSTANCE_IDENTIFIER_LABEL', "smgp-instance-identifier")


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
    print(f"Deregistering instance from k8s cluster - {instance_identifier}")
    nodes = v1.list_node(label_selector=f"{INSTANCE_IDENTIFIER_LABEL}={instance_identifier}")
    for node in nodes.items:
        node_name = node.metadata.name
        print(f"Deleting k8s node {node_name}")
        v1.delete_node(name=node_name)


def pod_gpu_count(pod: object) -> int:
    return sum(
        c.resources.limits.get("nvidia.com/gpu", 0) for c in pod.spec.containers
    )


def on_pod_added(pod: object) -> None:
    global UNSCHEDULED_GPU_PODS, GPU_PODS
    gpu_count = pod_gpu_count(pod)
    if gpu_count > 0:
        if not pod.spec.node_name and pod not in UNSCHEDULED_GPU_PODS:
            UNSCHEDULED_GPU_PODS.add(pod)
            GPU_PODS.add(pod)
            print(f"Pod added: {pod.metadata.name} in namespace {pod.metadata.namespace}")


def on_pod_deleted(pod: object) -> None:
    global GPU_PODS, UNSCHEDULED_GPU_PODS
    gpu_count = pod_gpu_count(pod)
    if gpu_count > 0:
        if pod in UNSCHEDULED_GPU_PODS:
            UNSCHEDULED_GPU_PODS.remove(pod)
            GPU_PODS.add(pod)
            print(f"Pod deleted: {pod.metadata.name} in namespace {pod.metadata.namespace}")
        if pod in GPU_PODS:
            GPU_PODS.remove(pod)


def update_pods_lists():
    global UNSCHEDULED_GPU_PODS, GPU_PODS
    pods = v1.list_pod_for_all_namespaces(_request_timeout=60, watch=False).items
    for pod in pods:
        total_gpus = pod_gpu_count(pod)
        if total_gpus > 0:
            GPU_PODS.add(pod)
            if not pod.node_name and pod not in UNSCHEDULED_GPU_PODS:
                UNSCHEDULED_GPU_PODS.add(pod)
                print(f"Pod added: {pod.name} in namespace {pod.namespace}")
            elif pod.node_name and pod in UNSCHEDULED_GPU_PODS:
                UNSCHEDULED_GPU_PODS.remove(pod)
                print(f"Pod scheduled: {pod.name} in namespace {pod.namespace}")
    UNSCHEDULED_GPU_PODS = UNSCHEDULED_GPU_PODS - set(pods)


def add_node_to_pool(node: object) -> None:
    global NODE_POOL
    NODE_POOL.add(node)


def remove_node_from_pool(node: object) -> None:
    global NODE_POOL
    if node in NODE_POOL:
        NODE_POOL.remove(node)
        print(f"Node removed: {node.metadata.name}")


def request_node_removal(qty: int = 1) -> dict | bool:
    global NODE_POOL
    pods: list = get_all_active_pods()
    for node in NODE_POOL.to_list():
        node_pods = [pod for pod in pods if pod.spec.node_name == node.metadata.name]
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
                    print("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    on_pod_added(event['object'])
                case 'DELETED':
                    print("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    on_pod_deleted(event['object'])
                case _:
                    continue
            await asyncio.sleep(0)
            if count < 10:
                count += 1
                print(event)
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
                    print("Event: %s %s" % (event['type'], event['object'].metadata.name))

                case 'ADDED':
                    print("Event: %s %s" % (event['type'], event['object'].metadata.name))
                    if event['object'].metadata.labels.get(NODE_ROLE_LABEL) == NODE_ROLE_LABEL_VALUE:
                        add_node_to_pool(event['object'])
                case _:
                    continue
            await asyncio.sleep(0)
            if count < 10:
                count += 1
                print(event)
            else:
                break
    except ReadTimeoutError:
        await asyncio.sleep(0)


def shutdown(loop: asyncio.AbstractEventLoop):
    print("Shutting down...")
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.stop()


loop = asyncio.get_event_loop()

for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, shutdown, loop)

try:
    loop.create_task(pods())
    loop.create_task(nodes())
    loop.run_forever()
except KeyboardInterrupt:
    shutdown(loop)
finally:
    loop.close()
    print("Shutdown complete.")
