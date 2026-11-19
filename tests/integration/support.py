"""Loopback-only HTTP fixtures. No credentials, cloud clients, or production state."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import threading
from unittest.mock import patch
from urllib.parse import urlsplit


@contextmanager
def local_network_only():
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def check(host):
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError(f'Integration tests cannot connect outside loopback: {host}')
    def guarded_connect(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return connect(sock, address)
    def guarded_ex(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return connect_ex(sock, address)
    def guarded_resolve(host, *args, **kwargs):
        check(host)
        return resolve(host, *args, **kwargs)
    with patch.object(socket.socket, 'connect', guarded_connect), \
         patch.object(socket.socket, 'connect_ex', guarded_ex), patch.object(socket, 'getaddrinfo', guarded_resolve):
        yield


class JSONServer:
    def __init__(self, app):
        self.app = app
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(body) if body else None
                parsed = urlsplit(self.path)
                app.requests.append((self.command, parsed.path, body))
                status, payload, headers = app.handle(self.command, parsed.path, parsed.query, body, self.headers)
                data = json.dumps(payload).encode() if payload is not None else b''
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        app.url = self.url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def completion(test, invoke):
    done, output = threading.Event(), {}
    def callback(**values):
        output.update(values)
        done.set()
    value = invoke(callback=callback)
    test.assertTrue(done.wait(15), 'Local provider operation did not complete')
    return value, output
