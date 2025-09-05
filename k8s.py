from kubernetes import client, config

config.load_incluster_config()
v1 = client.CoreV1Api()


def deregister_k8s_node(instance_name: str):
    print(f"Deregistering instance from k8s cluster - {instance_name}")

    nodes = v1.list_node(label_selector=f"provider-instance-name={instance_name}")
    for node in nodes.items:
        node_name = node.metadata.name
        print(f"Deleting k8s node {node_name}")
        v1.delete_node(name=node_name)
