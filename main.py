import logging
import signal
import sys
import threading


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)], force=True)
    import k8s
    import provisioner

    pools = provisioner.build_pools()
    if not pools:
        raise ValueError('At least one pool must be enabled')
    k8s.initialize()
    if not all(pool.dry_run for pool in pools):
        import tailscale_provider
        tailscale_provider.start()
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    provisioner.start_auto_scaler(pools, shutdown)


if __name__ == '__main__':
    main()
