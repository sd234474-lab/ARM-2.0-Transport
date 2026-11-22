"""R2-only TLS tunnel. No TLS interception, OAuth, request URL or payload logging."""
import ipaddress
import json
import re
import select
import socket
import socketserver
import threading
import time
from pathlib import Path

CONFIG = Path('/run/r2-disk/config.json')
SLOTS = threading.BoundedSemaphore(16)
UPLOAD_POOL = re.compile(r'uploader[a-z0-9]{1,24}\.disk\.yandex\.net')


def provider_host(host):
    return host in {'cloud-api.yandex.net', 'downloader.disk.yandex.ru'} or bool(re.fullmatch(
        r'(?:uploader[a-z0-9-]+\.disk\.yandex\.(?:net|ru)|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.storage\.yandex\.net)', host))


def allowed_hosts(config):
    hosts = config.get('allowed_hosts', [])
    if (not isinstance(hosts, list) or not 2 <= len(hosts) <= 16 or len(set(hosts)) != len(hosts)
            or 'cloud-api.yandex.net' not in hosts or not all(isinstance(h, str) and provider_host(h) for h in hosts)):
        raise ValueError('Invalid R2 Disk destination policy')
    return frozenset(hosts)


def resolved_addresses(authority, allowlist):
    match = re.fullmatch(r'([a-z0-9.-]{1,253}):443', authority)
    if not match or not (match[1] in allowlist or (
            UPLOAD_POOL.fullmatch(match[1]) and any(UPLOAD_POOL.fullmatch(h) for h in allowlist))):
        raise ValueError('Destination denied')
    addresses = socket.getaddrinfo(match[1], 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Non-public destination denied')
    return addresses


def destination(authority, allowlist):
    # Connect to the already checked IP; never resolve a second time after the guard.
    family, kind, protocol, _, address = resolved_addresses(authority, allowlist)[0]
    return family, kind, protocol, address


class Tunnel(socketserver.StreamRequestHandler):
    timeout = 15

    def handle(self):
        if not SLOTS.acquire(blocking=False):
            self.wfile.write(b'HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n')
            return
        upstream = None
        try:
            line = self.rfile.readline(4097)
            if not line:
                return  # Local TCP health probe.
            if len(line) > 4096:
                raise ValueError('Request too long')
            parts = line.decode('ascii').strip().split()
            if len(parts) != 3 or parts[2] not in {'HTTP/1.0', 'HTTP/1.1'}:
                raise ValueError('Protocol rejected')
            total = 0
            while True:
                header = self.rfile.readline(4097)
                total += len(header)
                if not header or total > 16384 or len(header) > 4096:
                    raise ValueError('Headers rejected')
                if header == b'\r\n':
                    break
            if parts[0] == 'GET' and parts[1].startswith('/r2-disk-resolve/'):
                host = parts[1].removeprefix('/r2-disk-resolve/')
                addresses = resolved_addresses(host + ':443', self.server.allowlist)
                body = json.dumps(sorted({a[4][0] for a in addresses})).encode()
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\n\r\n' + body)
                return
            if parts[0] != 'CONNECT':
                raise ValueError('CONNECT only')
            family, kind, protocol, address = destination(parts[1], self.server.allowlist)
            upstream = socket.socket(family, kind, protocol)
            upstream.settimeout(15)
            upstream.connect(address)
            self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.wfile.flush()
            deadline, transferred = time.monotonic() + 300, 0
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.connection, upstream], [], [], 15)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    transferred += len(data)
                    if transferred > 256 * 1024**2:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        except (ValueError, UnicodeError, OSError):
            if upstream is None:
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
        finally:
            if upstream is not None:
                upstream.close()
            SLOTS.release()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def handle_error(self, request, client_address):
        pass  # Never log tunnel headers, URLs, TLS bytes or exception payloads.


if __name__ == '__main__':
    with Server(('0.0.0.0', 3128), Tunnel) as server:
        server.allowlist = allowed_hosts(json.loads(CONFIG.read_text()))
        server.serve_forever()
