import os
import uuid
from google.cloud import compute_v1
import requests
import time

def create_instance_from_template(auth_key: str) -> str:
    project_id = os.getenv("GCP_PROJECT")
    zone = os.getenv("GCP_ZONE", "us-central1-a")
    name_prefix = os.getenv("INSTANCE_NAME", "ts-node")
    template_name = os.getenv("INSTANCE_TEMPLATE")
    startup_script_template = os.getenv("STARTUP_SCRIPT")

    if not project_id or not template_name:
        raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set in the environment")

    # Unique instance name
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{name_prefix}-{unique_suffix}"
    print(f"Using instance name: {instance_name}")

    print(f"Provisioned Tailscale key: {auth_key}...")

    # 2. Replace placeholder in script
    startup_script = startup_script_template.replace("__TS_AUTHKEY__", auth_key)

    # 3. Create instance from template with injected script
    instance_client = compute_v1.InstancesClient()
    source_template = f"projects/{project_id}/global/instanceTemplates/{template_name}"

    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.source_instance_template = source_template
    instance.metadata = compute_v1.Metadata()
    instance.metadata.items = [
        compute_v1.Items(key="startup-script", value=startup_script)
    ]

    op = instance_client.insert(
        project=project_id,
        zone=zone,
        instance_resource=instance,
    )
    print(f"Creating instance {instance_name}, operation: {op.name}")
    op.result()
    print(f"Instance {instance_name} created!")
    return instance_name
