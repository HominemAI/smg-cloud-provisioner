"""Explicit ordered candidates with a durable guard against uncertain submissions."""
import uuid
from failover import OrderedProvider
from lifecycle import LifecycleStore, UnresolvedLaunch


class PlacementProvider(OrderedProvider):
    def __init__(self, providers, name_prefix, ledger_path):
        super().__init__(providers, name_prefix)
        self.store = LifecycleStore(ledger_path)
        self.scope = 'placement/' + name_prefix
        self.active_attempt = None

    def list_instances(self):
        inventory = super().list_instances()
        for entry in self.store.records(self.scope):
            if entry['name'] == self.active_attempt:
                raise UnresolvedLaunch('Placement submission is still running or was interrupted')
            resources = entry['resources']
            new = set(inventory) - set(resources['baseline'])
            expected = resources.get('instance_name')
            owned = [name for name in new if name.startswith(resources['prefix'] + '-')
                     and (expected is None or name == expected)]
            if len(owned) != 1:
                raise UnresolvedLaunch('Placement outcome unknown; inspect cloud operation before abandoning')
            # A single discovered allocation consumes the requested pool slot.
            self.store.update(self.scope, entry['name'], 'deleted')
        return inventory

    def confirm_abandoned(self, name, *, no_pending_operation=False):
        if no_pending_operation is not True:
            raise ValueError('Confirm no pending cloud operation first')
        entry = next((r for r in self.store.records(self.scope) if r['name'] == name), None)
        if entry is not None and entry['name'] == self.active_attempt:
            raise UnresolvedLaunch('Submission is still executing')
        if entry is None:
            raise ValueError('Unknown placement attempt')
        if set(super().list_instances()) - set(entry['resources']['baseline']):
            raise UnresolvedLaunch('New instances exist; reconcile before abandoning')
        self.store.update(self.scope, name, 'deleted')

    def create_instance(self, callback=None):
        baseline = self.list_instances()
        with self.lock:
            provider = self.providers[self.index]
        attempt = uuid.uuid4().hex
        self.store.reserve(self.scope, attempt, {'prefix': provider.NAME_PREFIX, 'baseline': baseline})
        self.store.update(self.scope, attempt, 'submitting')
        self.active_attempt = attempt

        def finish(success, instance_name, error):
            rejected = getattr(error, 'launch_rejected', False) is True
            if instance_name:
                self.store.save_resources(self.scope, attempt, {'instance_name': instance_name})
            self.store.update(self.scope, attempt, 'deleted' if rejected else ('ready' if success else 'uncertain'), error)
            self.active_attempt = None
            with self.lock:
                self.index = 0 if success else (self.providers.index(provider) + 1) % len(self.providers)
            if callback:
                callback(success=success, instance_name=instance_name, error=error)
        try:
            return provider.create_instance(callback=finish)
        except Exception as error:
            # Managed providers validate synchronously before reserving/submitting.
            # Unmanaged providers may submit synchronously: absence is not proof.
            safe = hasattr(provider, 'driver') and not any(
                r['phase'] != 'ready' for r in provider.store.records(provider.scope))
            self.store.update(self.scope, attempt, 'deleted' if safe else 'uncertain', error)
            self.active_attempt = None
            with self.lock:
                self.index = (self.providers.index(provider) + 1) % len(self.providers)
            raise
