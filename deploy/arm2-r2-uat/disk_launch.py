"""R2-only private token loader; use the existing adapter after live identity verification."""
import json
import os
import stat
import sys
from pathlib import Path


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


if __name__ == '__main__':
    try:
        configure()
    except Exception:  # noqa: BLE001 -- provider exceptions can contain signed URLs or credentials
        print('R2 Disk startup verification FAILED; application not started', file=sys.stderr)
        raise SystemExit(1) from None
    os.execvp(sys.argv[1], sys.argv[1:])
