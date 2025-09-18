import os
import uuid
from google.cloud import compute_v1

project_id = os.getenv("GCP_PROJECT")
zone = os.getenv("GCP_ZONE", "us-west1-a")
name_prefix = os.getenv("INSTANCE_NAME", "ts-node")
template_name = os.getenv("INSTANCE_TEMPLATE")
startup_script_template = os.getenv("STARTUP_SCRIPT")
if not project_id or not template_name:
    raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set")


def create_instance_from_template(auth_key: str, k3s_key: str) -> str:
    # Unique instance name
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{name_prefix}-{unique_suffix}"
    print(f"Using instance name: {instance_name}")

    # Regional template path
    source_instance_template_url = (
        f"projects/{project_id}/regions/us-west1/instanceTemplates/{template_name}"
    )

    # Instance resource only needs the name (other props come from template)
    instance_resource = compute_v1.Instance(name=instance_name)

    # Inject startup script (replace placeholder with auth key)
    if startup_script_template:
        startup_script = (startup_script_template.replace("__TS_AUTHKEY__", auth_key)
                          .replace("__K3S_KEY__", k3s_key)
                          .replace("__INSTANCE_NAME__", instance_name))
        metadata = compute_v1.Metadata()
        metadata.items = [compute_v1.Items(key="startup-script", value=startup_script)]
        instance_resource.metadata = metadata

    client = compute_v1.InstancesClient()

    request = compute_v1.InsertInstanceRequest(
        project=project_id,
        zone=zone,
        instance_resource=instance_resource,
        source_instance_template=source_instance_template_url,
    )

    operation = client.insert(request=request)
    print(f"Creating instance {instance_name}, operation: {operation.name}")
    operation.result()  # wait until done
    print(f"Instance {instance_name} created successfully!")

    return instance_name


def shutdown_instance(instance_name: str):
    client = compute_v1.InstancesClient()
    request = compute_v1.DeleteInstanceRequest(
        project=project_id,
        zone=zone,
        instance=instance_name,
    )
    operation = client.delete(request=request)
    print(f"Deleting instance {instance_name}, operation: {operation.name}")
    operation.result()
    print(f"Instance {instance_name} deleted successfully!")
