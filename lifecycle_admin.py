"""Explicit recovery for an uncertain launch; never invoked by reconciliation."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'confirm-abandoned'])
    parser.add_argument('--pool', required=True)
    parser.add_argument('--name')
    parser.add_argument('--confirm-no-pending-cloud-operation', action='store_true')
    args = parser.parse_args()
    from pool_configuration import build_pools
    pool = next((p for p in build_pools() if p.name == args.pool), None)
    if pool is None or not hasattr(pool.provider, 'store'):
        parser.error('Select a configured pool using durable lifecycle storage')
    provider = pool.provider
    if args.action == 'confirm-abandoned':
        if not args.name or not args.confirm_no_pending_cloud_operation:
            parser.error('Supply --name and confirm no pending cloud operation after checking cloud operation history')
        provider.confirm_abandoned(args.name, no_pending_operation=True)
    print(json.dumps(provider.store.records(provider.scope), indent=2))


if __name__ == '__main__':
    main()
