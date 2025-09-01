import os
import uuid
from google.cloud import compute_v1

def create_instance_from_template():
    project_id = os.getenv("GCP_PROJECT")
    zone = os.getenv("GCP_ZONE", "us-central1-a")
    name_prefix = os.getenv("INSTANCE_NAME", "ts-node")
    template_name = os.getenv("INSTANCE_TEMPLATE")
    startup_script = os.getenv("STARTUP_SCRIPT")

    if not project_id or not template_name:
        raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set")

    # Generate unique instance name
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{name_prefix}-{unique_suffix}"
    print(f"Using instance name: {instance_name}")

    # Create the InstancesClient
    instances_client = compute_v1.InstancesClient()

    # Construct template reference (assuming global template)
    template_url = f"global/instanceTemplates/{template_name}"

    # Base instance resource
    instance_resource = compute_v1.Instance(
        name=instance_name,
        source_instance_template=template_url,
    )

    # Add startup script if provided
    if startup_script:
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script)
        ]
        instance_resource.metadata = metadata

    # Call the API
    operation = instances_client.insert(
        project=project_id,
        zone=zone,
        instance_resource=instance_resource,
    )

    print(f"Creating instance {instance_name}, operation: {operation.name}")
    operation.result()  # Wait for the op to complete
    print(f"Instance {instance_name} created successfully!")
    return instance_name
