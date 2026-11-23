"""R2-only private token loader; use the existing adapter after live identity verification."""
import ipaddress
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace


def private_token(source):
    meta = source.lstat()
    if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_uid != os.geteuid():
        raise ValueError('Private service-owned token required')
    token = source.read_text().strip()
    if not 20 <= len(token) <= 4096 or any(c.isspace() for c in token):
        raise ValueError('Invalid private token')
    return token


def configure():
    if os.environ.get('APP_ENV') != 'test' or os.environ.get('ORGANIZATIONS_ENABLED') != 'false':
        raise ValueError('R2 test configuration required')
    if os.environ.get('YANDEX_DISK_TOKEN') or os.environ.get('MAX_BOT_TOKEN'):
        raise ValueError('Integration environment token forbidden')
    token = private_token(Path(os.environ['ARM2_R2_DISK_TOKEN_FILE']))
    config = json.loads(Path(os.environ['ARM2_R2_DISK_CONFIG']).read_text())
    os.environ['YANDEX_DISK_TOKEN'] = token
    os.environ['ARM2_DOCUMENT_DISK_ROOT'] = config['document_root']
    sys.path.insert(0, '/service')
    from app.arm2.document_file_storage import document_root
    from app.integrations import YandexDisk
    provider = YandexDisk()
    identity = provider.request('GET', '/', params={'fields': 'user.uid'})
    identity.raise_for_status()
    if str(identity.json()['user']['uid']) != config['account_uid']:
        raise ValueError('Wrong test account')
    response = provider.request('GET', '/resources', params={
        'path': document_root(), 'fields': 'type,resource_id,public_key'})
    response.raise_for_status()
    root = response.json()
    if root['type'] != 'dir' or root['resource_id'] != config['resource_id'] or root.get('public_key'):
        raise ValueError('Wrong or public test root')


def resolve_disk(host, port, *, type=0):
    """Resolve only inside the private gateway; existing safe_url still validates every result."""
    import httpx
    if port != 443 or type != socket.SOCK_STREAM or not host or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host):
        raise ValueError('Invalid Disk DNS request')
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.get('http://r2-disk-egress:3128/r2-disk-resolve/' + host)
        response.raise_for_status()
        values = response.json()
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError('Invalid Disk DNS response')
    addresses = [ipaddress.ip_address(value) for value in values]
    if any(not address.is_global for address in addresses):
        raise ValueError('Non-public Disk DNS response')
    return [(socket.AF_INET if address.version == 4 else socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
        (str(address), 443) if address.version == 4 else (str(address), 443, 0, 0)) for address in addresses]


def install_document_dns():
    # Replace only this module's DNS dependency, not Python's shared socket module.
    # DB/health/application sockets retain ordinary Docker DNS. No URL/TLS guard is removed.
    from app import integrations
    def lookup(host, port, *args, **kwargs):
        if host == 'cloud-api.yandex.net' or host.endswith(('.disk.yandex.net', '.disk.yandex.ru', '.storage.yandex.net')):
            return resolve_disk(host, port, **kwargs)
        return socket.getaddrinfo(host, port, *args, **kwargs)
    integrations.socket = SimpleNamespace(getaddrinfo=lookup, SOCK_STREAM=socket.SOCK_STREAM)


if __name__ == '__main__':
    try:
        configure()
        install_document_dns()
    except Exception:  # noqa: BLE001 -- provider exceptions can contain signed URLs or credentials
        print('R2 Disk startup verification FAILED; application not started', file=sys.stderr)
        raise SystemExit(1) from None
    # Keep the scoped adapter dependency in this process while invoking the same CLI/options.
    import uvicorn
    if sys.argv[1] != 'uvicorn':
        raise SystemExit('R2 uvicorn command required')
    uvicorn.main(args=sys.argv[2:], standalone_mode=True)
