import os
import uuid
from google.cloud import compute_v1

def create_instance_from_template():
    project_id = os.getenv("GCP_PROJECT")
    zone = os.getenv("GCP_ZONE", "us-west1-a")
    name_prefix = os.getenv("INSTANCE_NAME", "ts-node")
    template_name = os.getenv("INSTANCE_TEMPLATE")

    if not project_id or not template_name:
        raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set in the environment")

    # Append a short UUID (8 chars) to make the name unique
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{name_prefix}-{unique_suffix}"

    print(f"Using instance name: {instance_name}")

    instance_client = compute_v1.InstancesClient()

    source_template = f"projects/{project_id}/global/instanceTemplates/{template_name}"

    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.source_instance_template = source_template

    op = instance_client.insert(
        project=project_id,
        zone=zone,
        instance_resource=instance,
    )
    print(f"Creating instance {instance_name} from template {template_name}, operation: {op.name}")
    op.result()
    print(f"Instance {instance_name} created!")

if __name__ == "__main__":
    create_instance_from_template()
