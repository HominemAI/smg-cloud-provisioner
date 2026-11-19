# AI Disclosure: AI was used to flesh out common error handling.
import logging
import os
import threading
import uuid
from google.cloud import compute_v1
from google.api_core import exceptions as gcp_exceptions
from cloud_provider import CloudProvider
from instance_audit import record

logger = logging.getLogger(__name__)


class GCEProvider(CloudProvider):
    PROVIDER_NAME = "gce"
    def __init__(self, project=None, zone=None, template=None, startup_script=None, **kwargs):
        super().__init__(startup_script=startup_script, **kwargs)
        self.GCP_PROJECT_ID = project or os.getenv("GCP_PROJECT_ID")
        self.GCP_ZONE = zone or os.getenv("GCP_ZONE")
        self.GCP_INSTANCE_TEMPLATE_NAME = template or os.getenv("GCP_INSTANCE_TEMPLATE_NAME")
        self.GCP_STARTUP_SCRIPT = startup_script if startup_script is not None else os.getenv("GCP_STARTUP_SCRIPT")
        if not all((self.GCP_PROJECT_ID, self.GCP_ZONE, self.GCP_INSTANCE_TEMPLATE_NAME)):
            raise ValueError("GCE project, zone and instance template are required")

    @staticmethod
    def operation_create_instance_submission(client, request, callback):
        try:
            record('create_requested', 'gce', request.instance_resource.name, project=request.project, zone=request.zone)
            operation = client.insert(request=request)
            operation.result()
            record('created', 'gce', request.instance_resource.name, project=request.project, zone=request.zone)
            if callback:
                callback(success=True, instance_name=request.instance_resource.name, error=None)
        except gcp_exceptions.ResourceExhausted as e:
            logger.error(f"Quota exceeded creating instance {request.instance_resource.name}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance_resource.name, error=e)
        except gcp_exceptions.PermissionDenied as e:
            logger.error(f"Permission denied creating instance {request.instance_resource.name}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance_resource.name, error=e)
        except gcp_exceptions.DeadlineExceeded as e:
            logger.error(f"Timeout creating instance {request.instance_resource.name}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance_resource.name, error=e)
        except Exception as e:
            logger.error(f"Failed to create instance {request.instance_resource.name}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance_resource.name, error=e)

    @staticmethod
    def operation_delete_instance_submission(client, request, callback):
        try:
            record('delete_requested', 'gce', request.instance, project=request.project, zone=request.zone)
            operation = client.delete(request=request)
            operation.result()
            record('deleted', 'gce', request.instance, project=request.project, zone=request.zone)
            if callback:
                callback(success=True, instance_name=request.instance, error=None)
        except gcp_exceptions.NotFound as e:
            logger.warning(f"Instance {request.instance} not found, may have already been deleted")
            record("absent", "gce", request.instance, project=request.project, zone=request.zone)
            if callback:
                callback(success=True, instance_name=request.instance, error=None)  # Still success
        except gcp_exceptions.PermissionDenied as e:
            logger.error(f"Permission denied deleting instance {request.instance}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance, error=e)
        except gcp_exceptions.DeadlineExceeded as e:
            logger.error(f"Timeout deleting instance {request.instance}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance, error=e)
        except Exception as e:
            logger.error(f"Failed to delete instance {request.instance}: {e}")
            if callback:
                callback(success=False, instance_name=request.instance, error=e)

    def create_instance(self, callback=None) -> str:
        unique_suffix = str(uuid.uuid4())[:8]
        instance_name = f"{self.NAME_PREFIX}-{unique_suffix}"
        logger.info(f"Using instance name: {instance_name}")

        # Regional template path
        region = self.GCP_ZONE.rsplit("-", 1)[0]
        source_instance_template_url = (
            f"projects/{self.GCP_PROJECT_ID}/regions/{region}/instanceTemplates/{self.GCP_INSTANCE_TEMPLATE_NAME}"
        )

        # Instance resource only needs the name (other props come from template)
        instance_resource = compute_v1.Instance(name=instance_name)

        # Inject startup script (replace placeholder with auth key)
        if self.GCP_STARTUP_SCRIPT:
            startup_script = CloudProvider.substitute_startup_script_vars(
                self.GCP_STARTUP_SCRIPT,
                {
                    "INSTANCE_NAME": instance_name,
                    "INSTANCE_PROVIDER": self.PROVIDER_NAME
                }
            )
            metadata = compute_v1.Metadata()
            metadata.items = [compute_v1.Items(key="startup-script", value=startup_script)]
            instance_resource.metadata = metadata
        client = compute_v1.InstancesClient()

        request = compute_v1.InsertInstanceRequest(
            project=self.GCP_PROJECT_ID,
            zone=self.GCP_ZONE,
            instance_resource=instance_resource,
            source_instance_template=source_instance_template_url,
        )

        creation_thread = threading.Thread(target=GCEProvider.operation_create_instance_submission,
                                           args=(client, request, callback))
        creation_thread.start()

        return instance_name

    def delete_instance(self, instance_identifier: str, callback=None) -> bool:
        if not instance_identifier.startswith(self.NAME_PREFIX + "-"):
            raise ValueError("Instance is outside this pool")
        client = compute_v1.InstancesClient()
        request = compute_v1.DeleteInstanceRequest(
            project=self.GCP_PROJECT_ID,
            zone=self.GCP_ZONE,
            instance=instance_identifier,
        )
        deletion_thread = threading.Thread(target=GCEProvider.operation_delete_instance_submission,
                                           args=(client, request, callback))
        deletion_thread.start()
        return True

    def list_instances(self) -> list:
        client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(project=self.GCP_PROJECT_ID, zone=self.GCP_ZONE)
        instances = client.list(request=request)
        return [instance.name for instance in instances
                if instance.name.startswith(self.NAME_PREFIX + "-")]

    def get_instance(self, instance_identifier: str) -> str:
        client = compute_v1.InstancesClient()
        request = compute_v1.GetInstanceRequest(project=self.GCP_PROJECT_ID, zone=self.GCP_ZONE, instance=instance_identifier)
        if not instance_identifier.startswith(self.NAME_PREFIX + "-"):
            return None
        try:
            instance = client.get(request=request)
        except gcp_exceptions.NotFound:
            return None
        return instance.name if instance else None
