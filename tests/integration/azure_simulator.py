"""Stateful ARM subset for real Compute/Network SDK HTTP and LRO tests.

Not an Azure implementation: intentionally models only operations used by this
operator, including delayed operations, pagination, lost replies and deletion.
"""
import copy
from urllib.parse import parse_qs


class AzureSimulator:
    def __init__(self):
        self.requests, self.resources, self.operations = [], {}, {}
        self.lose_vm_reply = False
        self.fail_nic_delete = False
        self.fail_second_page = False
        self.pending_polls = 1

    def handle(self, method, path, query, body, headers):
        if headers.get('Authorization') != 'Bearer local-test-token':
            return 401, {'error': {'code': 'Unauthorized', 'message': 'Fake credentials required'}}, {}
        error = {'error': {'code': 'ResourceNotFound', 'message': 'Local resource absent'}}
        if path.startswith('/operations/'):
            op = self.operations[path]
            op['polls'] += 1
            done = op['polls'] > self.pending_polls
            if done:
                if op['delete']:
                    self.resources.pop(op['resource'], None)
                elif op['resource'] in self.resources:
                    self.resources[op['resource']]['properties']['provisioningState'] = 'Succeeded'
            return 200, {'status': 'Succeeded' if done else 'InProgress'}, {'Retry-After': '0'}
        if path.endswith('/skus'):
            sku = {'name': 'Standard_TestGPU', 'resourceType': 'virtualMachines', 'locations': ['westus'],
                   'capabilities': [{'name': 'GPUs', 'value': '1'}, {'name': 'LowPriorityCapable', 'value': 'True'}],
                   'locationInfo': [{'location': 'westus', 'zones': ['1', '2']}], 'restrictions': []}
            return 200, {'value': [sku]}, {}
        if method == 'GET' and path in self.resources:
            return 200, copy.deepcopy(self.resources[path]), {}
        if method == 'GET' and path.endswith(('/virtualMachines', '/disks', '/networkInterfaces', '/publicIPAddresses')):
            values = [copy.deepcopy(v) for k, v in self.resources.items() if k.rsplit('/', 1)[0] == path]
            if 'page' not in parse_qs(query):
                return 200, {'value': [], 'nextLink': self.url + path + '?page=2&api-version=2024-07-01'}, {}
            if self.fail_second_page:
                return 503, {'error': {'code': 'ServiceUnavailable', 'message': 'Injected page failure'}}, {}
            return 200, {'value': values}, {}
        if method == 'PUT':
            resource = copy.deepcopy(body)
            resource.update(id=path, name=path.rsplit('/', 1)[1], type='/'.join(path.split('/')[-3:-1]))
            resource.setdefault('properties', {})['provisioningState'] = 'Creating'
            self.resources[path] = resource
            if '/virtualMachines/' in path:
                disk = resource['properties']['storageProfile']['osDisk']
                disk_id = path.split('/virtualMachines/')[0] + '/disks/' + disk['name']
                disk['managedDisk']['id'] = disk_id
                self.resources[disk_id] = {'id': disk_id, 'name': disk['name'], 'location': resource['location'],
                    'properties': {'provisioningState': 'Succeeded', 'diskSizeGB': disk['diskSizeGB'],
                                   'creationData': {'createOption': 'FromImage'}}}
                if self.lose_vm_reply:
                    resource['properties']['provisioningState'] = 'Succeeded'
                    return 503, {'error': {'code': 'ServiceUnavailable', 'message': 'Reply lost after allocation'}}, {}
            operation = f'/operations/{len(self.operations) + 1}'
            self.operations[operation] = {'resource': path, 'polls': 0, 'delete': False}
            return 201, copy.deepcopy(resource), {'Azure-AsyncOperation': self.url + operation, 'Retry-After': '0'}
        if method == 'DELETE':
            if self.fail_nic_delete and '/networkInterfaces/' in path:
                return 409, {'error': {'code': 'Conflict', 'message': 'Injected deletion conflict'}}, {}
            if path not in self.resources:
                return 404, error, {}
            operation = f'/operations/{len(self.operations) + 1}'
            self.operations[operation] = {'resource': path, 'polls': 0, 'delete': True}
            return 202, None, {'Azure-AsyncOperation': self.url + operation, 'Location': self.url + operation, 'Retry-After': '0'}
        return 404, error, {}
