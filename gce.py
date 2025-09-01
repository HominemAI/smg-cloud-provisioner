import os
import uuid
from google.cloud import compute_v1

def create_instance_from_template(auth_key: str):
    project_id = os.getenv("GCP_PROJECT")
    zone = os.getenv("GCP_ZONE", "us-central1-a")
    name_prefix = os.getenv("INSTANCE_NAME", "ts-node")
    template_name = os.getenv("INSTANCE_TEMPLATE")
    startup_script_template = os.getenv("STARTUP_SCRIPT")

    if not project_id or not template_name:
        raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set")

    # Generate unique instance name
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{name_prefix}-{unique_suffix}"
    print(f"Using instance name: {instance_name}")

    # Build the full template URL
    source_instance_template_url = (
        f"projects/{project_id}/global/instanceTemplates/{template_name}"
    )

    # Prepare request body with just the name
    instance_resource = compute_v1.Instance(name=instance_name)

    # Inject startup script if provided (replace __TS_AUTHKEY__)
    if startup_script_template:
        startup_script = startup_script_template.replace("__TS_AUTHKEY__", auth_key)
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script)
        ]
        instance_resource.metadata = metadata

    # Create client
    client = compute_v1.InstancesClient()

    # Correct API call — pass source_instance_template at the top level
    operation = client.insert(
        project=project_id,
        zone=zone,
        instance_resource=instance_resource,
        source_instance_template=source_instance_template_url,
    )

    print(f"Creating instance {instance_name}, operation: {operation.name}")
    operation.result()  # wait for completion
    print(f"Instance {instance_name} created successfully!")

    return instance_name
