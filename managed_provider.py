"""Provider-independent orchestration; drivers own cloud requests, stores own durability.

Each instance has an independent durable record. Submissions are serialized; an
uncertain submission blocks new capacity until inventory proves its outcome.
"""
import threading
import uuid
from typing import Protocol
from enum import Enum

from cloud_provider import CloudProvider
from instance_audit import record
from lifecycle import LifecycleRepository, UnresolvedLaunch


class InstanceState(str, Enum):
    READY = 'ready'
    FAILED = 'failed'
    PENDING = 'pending'
    ORPHANED = 'orphaned'


class ResourceDriver(Protocol):
    scope: str
    def plan(self, name: str) -> dict: ...
    def validate(self) -> None: ...
    def validate_script(self, script: str) -> None: ...
    def ensure_absent(self, resources: dict) -> None: ...
    def create(self, name: str, resources: dict, script: str, resources_changed) -> None: ...
    def discover_resources(self, name: str, resources: dict) -> dict: ...
    def inventory(self) -> dict[str, InstanceState]: ...
    def delete(self, name: str, resources: dict) -> None: ...


class ManagedProvider(CloudProvider):
    def __init__(self, driver: ResourceDriver, store: LifecycleRepository, **kwargs):
        super().__init__(**kwargs)
        self.driver, self.store = driver, store
        self.scope = driver.scope
        self._busy = threading.Lock()

    def _run(self, name, operation, callback, failure_phase='uncertain'):
        def work():
            error = None
            try:
                operation()
            except Exception as exc:
                error = exc
                self.store.update(self.scope, name, failure_phase, exc)
                record('operation_uncertain', self.PROVIDER_NAME, name, error_type=type(exc).__name__)
            finally:
                self._busy.release()
            if callback:
                # Do not expose SDK exceptions which can contain rendered credentials.
                callback(success=error is None, instance_name=name,
                         error=UnresolvedLaunch(type(error).__name__) if error else None)
        threading.Thread(target=work, daemon=True).start()

    def create_instance(self, callback=None):
        if not self._busy.acquire(blocking=False):
            raise UnresolvedLaunch('A lifecycle operation is in progress')
        try:
            inventory = self.driver.inventory()
            entries = self.store.records(self.scope)
            if set(inventory) - {entry['name'] for entry in entries}:
                raise UnresolvedLaunch('Owned resources lack durable records')
            if any(entry['phase'] != 'ready' for entry in entries):
                raise UnresolvedLaunch('Pool has an unresolved launch or cleanup')
            self.driver.validate()
            name = f'{self.NAME_PREFIX}-{uuid.uuid4().hex[:16]}'
            script = self.substitute_startup_script_vars(self.startup_script,
                       {'INSTANCE_NAME': name, 'INSTANCE_PROVIDER': self.PROVIDER_NAME})
            self.driver.validate_script(script)
            resources = self.driver.plan(name)
            self.driver.ensure_absent(resources)
            self.store.reserve(self.scope, name, resources)
            record('resources_reserved', self.PROVIDER_NAME, name, resources=resources)
        except Exception:
            self._busy.release()
            raise

        def create():
            self.store.update(self.scope, name, 'submitting')
            def resources_changed(discovered):
                self.store.save_resources(self.scope, name, discovered)
                resources.update(discovered)
                record('resources_discovered', self.PROVIDER_NAME, name, resources=discovered)
            self.driver.create(name, resources, script, resources_changed)
            self.store.update(self.scope, name, 'ready')
            record('created', self.PROVIDER_NAME, name, resources=resources)
        self._run(name, create, callback)
        return name

    def list_instances(self):
        if not self._busy.acquire(blocking=False):
            raise UnresolvedLaunch('A lifecycle operation is in progress')
        try:
            inventory = dict(self.driver.inventory())  # Exceptions must never mean empty.
            records = self.store.records(self.scope)
            known = {entry['name'] for entry in records}
            if set(inventory) - known:
                raise UnresolvedLaunch('Owned resources exist without local records; restore the lifecycle database')
            for entry in records:
                name = entry['name']
                state = inventory.get(name)
                discovered = self.driver.discover_resources(name, entry['resources'])
                self.store.save_resources(self.scope, name, discovered)
                if entry['phase'] == 'deleting':
                    inventory[name] = 'Deleting'
                    continue
                if state in (InstanceState.READY, InstanceState.FAILED):
                    self.store.update(self.scope, name, 'ready')
                elif state not in (None, InstanceState.ORPHANED) or entry['phase'] != 'ready':
                    raise UnresolvedLaunch(f'Launch outcome unresolved for {name}')
                else:
                    # Keep the vanished VM in inventory until the normal Pool
                    # deletion path cleans auxiliaries. Listing never mutates cloud.
                    inventory[name] = 'Missing'
            return list(inventory)
        finally:
            self._busy.release()

    def get_instance(self, instance_identifier):
        return instance_identifier if instance_identifier in self.list_instances() else None

    def delete_instance(self, instance_identifier, callback=None):
        if not self._busy.acquire(blocking=False):
            raise UnresolvedLaunch('A lifecycle operation is in progress')
        try:
            entry = next((r for r in self.store.records(self.scope) if r['name'] == instance_identifier), None)
            if entry is None:
                raise ValueError('Deletion requires this pool’s durable resource record')
            state = self.driver.inventory().get(instance_identifier)
            if state not in (None, InstanceState.ORPHANED, InstanceState.READY, InstanceState.FAILED):
                raise UnresolvedLaunch('VM operation is still in progress')
            if state in (None, InstanceState.ORPHANED) and entry['phase'] not in ('ready', 'deleting'):
                raise UnresolvedLaunch('Cannot delete an unresolved launch; verify the cloud operation first')
            self.store.update(self.scope, instance_identifier, 'deleting')
        except Exception:
            self._busy.release()
            raise

        def delete():
            self.driver.delete(instance_identifier, entry['resources'])
            self.store.update(self.scope, instance_identifier, 'deleted')
            record('deleted', self.PROVIDER_NAME, instance_identifier, resources=entry['resources'])
        self._run(instance_identifier, delete, callback, failure_phase='deleting')
        return True

    def confirm_abandoned(self, name, *, no_pending_operation=False):
        """Operator recovery only, after independent confirmation of a terminal cloud request.

        Absence alone is not proof: this is deliberately never called by the scaler.
        It makes auxiliaries eligible for ordinary, ownership-checked cleanup.
        """
        if no_pending_operation is not True:
            raise ValueError('Explicit confirmation of no pending cloud operation is required')
        if not self._busy.acquire(blocking=False):
            raise UnresolvedLaunch('A lifecycle operation is in progress')
        try:
            entry = next((r for r in self.store.records(self.scope) if r['name'] == name), None)
            if entry is None or entry['phase'] not in ('reserved', 'submitting', 'uncertain'):
                raise ValueError('Instance has no unresolved launch reservation')
            if self.driver.inventory().get(name) not in (None, InstanceState.ORPHANED):
                raise UnresolvedLaunch('VM still exists; reconcile its terminal state instead')
            record('operator_confirmed_abandoned', self.PROVIDER_NAME, name, resources=entry['resources'])
            self.store.update(self.scope, name, 'ready')
        finally:
            self._busy.release()
