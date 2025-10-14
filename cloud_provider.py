from abc import ABC, abstractmethod
import logging
import os
import re

logger = logging.getLogger(__name__)


class CloudProvider(ABC):
    STARTUP_SCRIPT = os.getenv("STARTUP_SCRIPT", None)
    NAME_PREFIX = os.getenv("INSTANCE_NAME", "smgcp")

    def __init__(self):
        if not self.STARTUP_SCRIPT or not self.NAME_PREFIX:
            raise ValueError("Startup Script and Instance Name Prefix must be set")

    @abstractmethod
    def create_instance(self, callback=None) -> str:
        pass

    @abstractmethod
    def delete_instance(self, instance_identifier: str) -> bool:
        pass

    @abstractmethod
    def list_instances(self) -> list:
        pass

    @abstractmethod
    def get_instance(self, instance_identifier: str) -> str:
        pass

    # /\/\/\/\/\/ Startup Script Handling /\/\/\/\/\/\
    @staticmethod
    def parse_startup_script(script: str) -> tuple:
        pattern = r'__([A-Za-z0-9_]+)__'
        matches = re.findall(pattern, script)
        pattern = r'\{\{([A-Za-z0-9_]+)\}\}'
        dynamic_matches = re.findall(pattern, script)
        return list(set(matches)), list(set(dynamic_matches))

    @staticmethod
    def ensure_startup_required_substitutions(env_vars: list[str]) -> bool:
        missing = [var for var in env_vars if var not in os.environ]
        if missing:
            raise ValueError(f"Missing required environment variables for startup script: {', '.join(missing)}")
        return True

    @staticmethod
    def ensure_startup_dynamic_substitutions(dynamic_vars: list[str], dynamic_values: dict) -> bool:
        missing = [var for var in dynamic_vars if var not in dynamic_values]
        if missing:
            raise ValueError(f"Missing required dynamic values for startup script: {', '.join(missing)}")
        return True

    @staticmethod
    def substitute_startup_script_vars(script: str, dynamic_values: dict = None) -> str:
        sub_script = script
        env_vars, dynamic_vars = CloudProvider.parse_startup_script(script)
        logger.info(f"Substituting startup dynamic variables: {dynamic_vars}")
        logger.info(f"Looking for following Env Variables: {str(env_vars)}")

        if CloudProvider.ensure_startup_required_substitutions(env_vars):
            for var in env_vars:
                sub_script = sub_script.replace(f"__{var}__", os.environ[var])

        if dynamic_values and CloudProvider.ensure_startup_dynamic_substitutions(dynamic_vars, dynamic_values):
            for key, value in dynamic_values.items():
                sub_script = sub_script.replace('{{' + key + '}}', value)

        return sub_script
