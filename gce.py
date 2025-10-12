import logging
import os
import threading
import uuid
from google.cloud import compute_v1
from cloud_provider import CloudProvider
logger = logging.getLogger(__name__)


class GCEProvider(CloudProvider):
    PROJECT_ID = os.getenv("GCP_PROJECT")
    ZONE = os.getenv("GCP_ZONE", "us-west1-a")
    NAME_PREFIX = os.getenv("INSTANCE_NAME", "ts-node")
    TEMPLATE_NAME = os.getenv("INSTANCE_TEMPLATE")
    STARTUP_SCRIPT = os.getenv("STARTUP_SCRIPT", "echo Startup Script was not provided.")

    def __init__(self):
        super().__init__()

    @staticmethod
    def operation_create_instance_submission(client, request, callback):
        operation = client.insert(request=request)
        operation.result()
        if callback:
            callback(request.instance_resource.name)

    @staticmethod
    def operation_delete_instance_submission(client, request, callback):
        operation = client.delete(request=request)
        operation.result()
        if callback:
            callback(request.instance)

    def create_instance(self, callback=None) -> str:
        unique_suffix = str(uuid.uuid4())[:8]
        instance_name = f"{self.NAME_PREFIX}-{unique_suffix}"
        logger.info(f"Using instance name: {instance_name}")

        # Regional template path
        source_instance_template_url = (
            f"projects/{self.PROJECT_ID}/regions/us-west1/instanceTemplates/{self.TEMPLATE_NAME}"
        )

        # Instance resource only needs the name (other props come from template)
        instance_resource = compute_v1.Instance(name=instance_name)

        # Inject startup script (replace placeholder with auth key)
        if self.STARTUP_SCRIPT:
            startup_script = CloudProvider.substitute_startup_script_vars(
                self.STARTUP_SCRIPT,
                {
                    "INSTANCE_NAME": instance_name,
                    "INSTANCE_PROVIDER": "gce"
                }
            )
            metadata = compute_v1.Metadata()
            metadata.items = [compute_v1.Items(key="startup-script", value=startup_script)]
            instance_resource.metadata = metadata
        client = compute_v1.InstancesClient()

        request = compute_v1.InsertInstanceRequest(
            project=self.PROJECT_ID,
            zone=self.ZONE,
            instance_resource=instance_resource,
            source_instance_template=source_instance_template_url,
        )

        creation_thread = threading.Thread(target=GCEProvider.operation_create_instance_submission,
                                           args=(client, request, callback))
        creation_thread.start()

        return instance_name

    def delete_instance(self, instance_identifier: str, callback=None) -> bool:
        client = compute_v1.InstancesClient()
        request = compute_v1.DeleteInstanceRequest(
            project=self.PROJECT_ID,
            zone=self.ZONE,
            instance=instance_identifier,
        )
        deletion_thread = threading.Thread(target=GCEProvider.operation_delete_instance_submission,
                                           args=(client, request, callback))
        deletion_thread.start()
        return True

    def list_instances(self) -> list:
        client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(project=self.PROJECT_ID, zone=self.ZONE)
        instances = client.list(request=request)
        return [instance.name for instance in instances]

    def get_instance(self, instance_identifier: str) -> str:
        client = compute_v1.InstancesClient()
        request = compute_v1.GetInstanceRequest(project=self.PROJECT_ID, zone=self.ZONE, instance=instance_identifier)
        instance = client.get(request=request)
        return instance.name if instance else None