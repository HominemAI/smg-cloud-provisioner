import logging
import os
import re
import threading
import uuid
from google.cloud import compute_v1

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT")
ZONE = os.getenv("GCP_ZONE", "us-west1-a")
NAME_PREFIX = os.getenv("INSTANCE_NAME", "ts-node")
TEMPLATE_NAME = os.getenv("INSTANCE_TEMPLATE")
STARTUP_SCRIPT = os.getenv("STARTUP_SCRIPT", "echo Startup Script was not provided.")

if not PROJECT_ID or not TEMPLATE_NAME:
    raise ValueError("GCP_PROJECT and INSTANCE_TEMPLATE must be set")


def parse_startup_script(script: str) -> list:
    pattern = r'__([A-Z_]+)__'
    matches = re.findall(pattern, script)
    print(matches)
    return list(set(matches))

def ensure_startup_required_substitutions(env_vars: list[str]) -> bool:
    missing = [var for var in env_vars if var not in os.environ]
    if missing:
        raise ValueError(f"Missing required environment variables for startup script: {', '.join(missing)}")
    return False

def substitute_startup_script_vars(script: str) -> str:
    sub_script = script
    env_vars = parse_startup_script(script)
    logger.info(sub_script)
    if ensure_startup_required_substitutions(env_vars):
        for var in env_vars:
            sub_script = sub_script.replace(f"__{var}__", os.getenv(var, ""))
            logger.info(sub_script)
    return sub_script

def operation_create_instance_submission(client, request, callback):
    operation = client.insert(request=request)
    operation.result()
    if callback:
        callback(request.instance_resource.name)


def operation_delete_instance_submission(client, request, callback):
    operation = client.delete(request=request)
    operation.result()
    if callback:
        callback(request.instance)


def create_instance_from_template(callback=None) -> str:
    # Unique instance name
    unique_suffix = str(uuid.uuid4())[:8]
    instance_name = f"{NAME_PREFIX}-{unique_suffix}"
    logger.info(f"Using instance name: {instance_name}")

    # Regional template path
    source_instance_template_url = (
        f"projects/{PROJECT_ID}/regions/us-west1/instanceTemplates/{TEMPLATE_NAME}"
    )

    # Instance resource only needs the name (other props come from template)
    instance_resource = compute_v1.Instance(name=instance_name)

    # Inject startup script (replace placeholder with auth key)
    if STARTUP_SCRIPT:
        startup_script = (substitute_startup_script_vars(STARTUP_SCRIPT))
        metadata = compute_v1.Metadata()
        metadata.items = [compute_v1.Items(key="startup-script", value=startup_script)]
        instance_resource.metadata = metadata
    client = compute_v1.InstancesClient()

    request = compute_v1.InsertInstanceRequest(
        project=PROJECT_ID,
        zone=ZONE,
        instance_resource=instance_resource,
        source_instance_template=source_instance_template_url,
    )

    creation_thread = threading.Thread(target=operation_create_instance_submission, args=(client, request, callback))
    creation_thread.start()

    return instance_name


def shutdown_instance(instance_name: str, callback=None):
    client = compute_v1.InstancesClient()
    request = compute_v1.DeleteInstanceRequest(
        project=PROJECT_ID,
        zone=ZONE,
        instance=instance_name,
    )
    deletion_thread = threading.Thread(target=operation_delete_instance_submission, args=(client, request, callback))
    deletion_thread.start()
