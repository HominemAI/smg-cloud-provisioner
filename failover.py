"""Ordered failover without bypassing inventory checks on either provider."""
import logging
import threading

from cloud_provider import CloudProvider

logger = logging.getLogger(__name__)


class OrderedProvider(CloudProvider):
    PROVIDER_NAME = 'ordered'

    def __init__(self, providers, name_prefix):
        super().__init__(name_prefix=name_prefix)
        self.providers = providers
        self.index = 0
        self.lock = threading.RLock()

    def reset_order(self):
        with self.lock:
            self.index = 0

    def advance_after_timeout(self, name):
        with self.lock:
            self.index = (self.providers.index(self._owner(name)) + 1) % len(self.providers)

    def _owner(self, name):
        return next(p for p in self.providers if name.startswith(p.NAME_PREFIX + '-'))

    def list_instances(self):
        # Fail closed if any inventory is unavailable: never assume that a
        # failed provider has no instances and create a duplicate elsewhere.
        return [name for provider in self.providers for name in provider.list_instances()]

    def get_instance(self, instance_identifier):
        return self._owner(instance_identifier).get_instance(instance_identifier)

    def delete_instance(self, instance_identifier, callback=None):
        return self._owner(instance_identifier).delete_instance(instance_identifier, callback)

    def create_instance(self, callback=None):
        with self.lock:
            provider = self.providers[self.index]

        def done(success, instance_name, error):
            if not success:
                with self.lock:
                    self.index = (self.providers.index(provider) + 1) % len(self.providers)
                logger.warning('Emergency launch failed on %s; next attempt will use %s after inventory reconciliation',
                               provider.PROVIDER_NAME, self.providers[self.index].PROVIDER_NAME)
            if callback:
                callback(success=success, instance_name=instance_name, error=error)
        return provider.create_instance(done)
