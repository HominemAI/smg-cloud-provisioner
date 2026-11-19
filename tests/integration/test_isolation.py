import socket
import unittest

from digitalocean_api import DigitalOceanAPI
from support import local_network_only


class IsolationTests(unittest.TestCase):
    def test_external_dns_and_connections_are_rejected_before_network_io(self):
        with local_network_only():
            with self.assertRaises(AssertionError):
                socket.getaddrinfo('api.digitalocean.com', 443)
            with socket.socket() as sock, self.assertRaises(AssertionError):
                sock.connect(('192.0.2.1', 443))

    def test_endpoint_override_cannot_send_tokens_to_an_external_host(self):
        for endpoint in ('https://example.com', 'http://192.0.2.1', 'http://127.0.0.1@evil.example',
                         'http://127.0.0.1:4010/redirect'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                DigitalOceanAPI('fake', base_url=endpoint)
