"""Exercise the real Requests/urllib3 path, not a mocked HTTPAdapter.send."""

import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from scraper.fetching import FetchError, Fetcher, _PinnedHTTPConnection, normalize_url


class TransportTests(unittest.TestCase):
    def server(self, tls=False):
        received, names = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get('Host')))
                self.send_response(200)
                self.send_header('Content-Length', '7')
                self.end_headers()
                self.wfile.write(b'article')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if tls:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            cert, key = Path(directory.name) / 'cert.pem', Path(directory.name) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=blog.example.org', '-addext', 'subjectAltName=DNS:blog.example.org'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            context.set_servername_callback(lambda sock, name, ctx: names.append(name))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            bundle = patch('requests.adapters.DEFAULT_CA_BUNDLE_PATH', str(cert))
            bundle.start()
            self.addCleanup(bundle.stop)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port, received, names

    def fetch_local(self, port, scheme='http', host='blog.example.org'):
        original_connect = socket.socket.connect
        connected = []

        def connect(sock, address):
            connected.append(address)
            self.assertEqual(address, ('93.184.216.34', port))
            # Test-only routing of the checked PUBLIC address to our fixture.
            # Production contains no private-network opt-out.
            return original_connect(sock, ('127.0.0.1', port))

        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]
        rebound = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        fetcher = Fetcher(delay=0)
        self.addCleanup(fetcher.close)
        with patch('socket.getaddrinfo', side_effect=[public, rebound]) as dns, \
                patch.object(socket.socket, 'connect', connect):
            try:
                result = fetcher.fetch(f'{scheme}://{host}:{port}/post/', check_robots=False)
            finally:
                self.assertEqual(dns.call_count, 1, 'DNS must not be resolved again while connecting')
                self.assertEqual(connected, [('93.184.216.34', port)])
        return result

    def test_real_http_pins_dns_and_preserves_host_header(self):
        port, received, _ = self.server()
        self.assertEqual(self.fetch_local(port).body, b'article')
        self.assertEqual(received, [('/post/', f'blog.example.org:{port}')])

    def test_real_https_preserves_sni_hostname_verification_and_host(self):
        port, received, names = self.server(tls=True)
        self.assertEqual(self.fetch_local(port, 'https').body, b'article')
        self.assertEqual(names, ['blog.example.org'])
        self.assertEqual(received, [('/post/', f'blog.example.org:{port}')])

    def test_https_wrong_certificate_is_rejected_before_http(self):
        port, received, names = self.server(tls=True)
        with self.assertRaises(FetchError) as caught:
            self.fetch_local(port, 'https', 'wrong.example.org')
        self.assertEqual(caught.exception.failure_kind, 'tls_error')
        self.assertEqual(names, ['wrong.example.org'])
        self.assertEqual(received, [])

    def test_ipv6_fallback_uses_only_validated_numeric_addresses(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2606:4700:4700::1111', 80, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        first, second = Mock(), Mock()
        first.connect.side_effect = OSError('IPv6 unavailable')
        with patch('socket.socket', side_effect=[first, second]), \
                patch('socket.getaddrinfo', side_effect=AssertionError('Second DNS lookup')):
            connection = _PinnedHTTPConnection('example.org', timeout=1, pinned_addresses=addresses)
            self.assertIs(connection._new_conn(), second)
        first.connect.assert_called_once_with(addresses[0][4])
        first.close.assert_called_once()
        second.connect.assert_called_once_with(addresses[1][4])

    def test_ipv6_translation_cannot_tunnel_to_private_ipv4(self):
        for address in ('64:ff9b::7f00:1', '64:ff9b:1::a00:1', '2002:7f00:1::', '::ffff:127.0.0.1'):
            with self.subTest(address=address):
                self.assertIsNone(normalize_url(f'http://[{address}]/'))


if __name__ == '__main__':
    unittest.main()
