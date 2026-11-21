"""Explicit offline, identity-bound R2 test reset. Never called by startup/migrations."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from app.config import settings
from app.db import engine

TABLES = (
    'arm2_bulk_cleanup', 'arm2_bulk_receipts', 'arm2_bulk_sessions', 'arm2_counterparties',
    'arm2_document_file_acceptances', 'arm2_document_file_corrections', 'arm2_document_file_stages',
    'arm2_document_files', 'arm2_document_kinds', 'arm2_document_revision_contexts',
    'arm2_document_revision_files', 'arm2_document_revisions', 'arm2_documents', 'arm2_history_events',
    'arm2_organizations', 'arm2_questionnaire_drafts', 'arm2_questionnaire_exports',
    'arm2_questionnaire_receipts', 'arm2_questionnaire_uploads', 'arm2_vehicle_types', 'arm2_vehicles',
    'nsi_counterparty_representatives', 'nsi_organization_representatives',
)
MARKER = '.arm2-uat-identity.json'
JOURNAL = '.arm2-uat-reset.json'


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.part')
    with temporary.open('w', encoding='utf-8') as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def safe_tree(root):
    for path in (root, *root.parents):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Reset refuses links/junctions')
    for path in root.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or (
                path.is_file() and path.stat().st_nlink != 1):
            raise ValueError('Reset refuses linked/shared FILE entries')


def validate(db, attestation, file_root, data_root, *, execute):
    cfg = settings()
    if cfg.app_env != 'test' or cfg.max_bot_token or cfg.yandex_disk_token or cfg.organizations_enabled:
        raise ValueError('Reset requires isolated test-only environment without integrations')
    if set(attestation) != {'project', 'file_volume', 'data_volume', 'offline'} or (execute and not attestation['offline']):
        raise ValueError('Offline exclusive Docker identity attestation required')
    project = attestation['project']
    if not re.fullmatch(r'arm2-ubuntu-uat-r2(?:-[a-z0-9]+)?', project):
        raise ValueError('Not an R2 project; B1/protected projects rejected')
    if (attestation['file_volume'], attestation['data_volume']) != (
            project + '_r2_file_data', project + '_r2_photo_data'):
        raise ValueError('Unexpected/shared FILE volume identities')
    database = db.scalar(text('SELECT current_database()'))
    if database != 'arm2_r2_uat' and not re.fullmatch(r'arm2_(?:[0-9a-f]{32}|int_issue72_[0-9a-f]{32})_test', database):
        raise ValueError('Not the selected R2/disposable test database')
    if db.scalar(text('SELECT version_num FROM alembic_version')) not in ('0069', '0070', '0071'):
        raise ValueError('Unexpected R2 revision')
    names = set(inspect(db).get_table_names())
    business = {n for n in names if n.startswith('arm2_') or n in TABLES}
    if business != set(TABLES):
        raise ValueError('Unknown/missing business tables: reset refused')
    admin = db.scalar(text("SELECT count(*) FROM users WHERE role='ADMIN_OPERATOR' AND active AND deleted_at IS NULL"))
    if not admin:
        raise ValueError('No administrator bootstrap to preserve')
    roots = [Path(file_root).absolute(), Path(data_root).absolute()]
    for root in roots:
        if not root.is_dir() or root.parent == root or len(root.parts) < 2:
            raise ValueError('Explicit existing FILE/data roots required')
        safe_tree(root)
    a, b = (p.resolve() for p in roots)
    if a == b or a in b.parents or b in a.parents:
        raise ValueError('FILE/data roots overlap')
    if database == 'arm2_r2_uat' and (str(a), str(b)) != ('/files', '/data'):
        raise ValueError('R2 must use its verified container mounts /files and /data')
    if Path(cfg.data_dir).resolve() != b:
        raise ValueError('Runtime root is not the configured DATA_DIR')
    identity = {'project': project, 'database': database,
                'database_oid': db.scalar(text('SELECT oid FROM pg_database WHERE datname=current_database()')),
                'cluster': db.scalar(text('SELECT system_identifier::text FROM pg_control_system()')),
                'file_volume': attestation['file_volume'], 'data_volume': attestation['data_volume'],
                'roots': [str(a), str(b)]}
    for root in roots:
        marker = root / MARKER
        if marker.exists() and json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('Stored DB/FILE identity mismatch; no changes made')
    for path in roots[1].iterdir():
        if path.name not in {MARKER, MARKER + '.part', JOURNAL, JOURNAL + '.part', 'arm2-document-runtime-v1'} and not re.fullmatch(r'\.arm2-reset-[0-9a-f]{32}', path.name):
            raise ValueError('Unknown data/configuration entry; no best-effort deletion')
    journal = roots[1] / JOURNAL
    if journal.exists():
        previous = json.loads(journal.read_text(encoding='utf-8'))
        if (previous.get('identity') != identity or previous.get('phase') not in {'PREPARED', 'COMMITTED'}
                or not re.fullmatch(r'\.arm2-reset-[0-9a-f]{32}', previous.get('quarantine', ''))):
            raise ValueError('Unsafe or foreign reset journal')
    return identity, roots


def counts(db):
    return {n: db.scalar(text(f'SELECT count(*) FROM "{n}"')) for n in TABLES}


def remove_quarantine(root, name):
    if not re.fullmatch(r'\.arm2-reset-[0-9a-f]{32}', name):
        raise ValueError('Unsafe reset journal path')
    target = root / name
    if target.resolve().parent != root.resolve():
        raise ValueError('Quarantine escaped selected root')
    if target.exists():
        safe_tree(target)
        shutil.rmtree(target)


def reset(db_engine, attestation, file_root, data_root, *, execute=False):
    cfg = settings()
    if cfg.app_env != 'test' or cfg.max_bot_token or cfg.yandex_disk_token or cfg.organizations_enabled:
        raise ValueError('Reset requires isolated test-only environment without integrations')
    database = db_engine.url.database or ''
    if (db_engine.url.host not in ('127.0.0.1', 'localhost', 'r2-db') or
            (database != 'arm2_r2_uat' and not re.fullmatch(r'arm2_(?:[0-9a-f]{32}|int_issue72_[0-9a-f]{32})_test', database))):
        raise ValueError('Not the selected R2/disposable test database; no connection attempted')
    # One connection/session lock across FS/SQL recovery; host command keeps HTTP services offline.
    with db_engine.connect() as db:
        identity, roots = validate(db, attestation, file_root, data_root, execute=execute)
        db.execute(text('SELECT pg_advisory_lock(720070, 1)'))
        try:
            before = counts(db)
            revision = db.scalar(text('SELECT version_num FROM alembic_version'))
            db.commit()
            if not execute:
                return {'dry_run': True, 'identity': identity, 'counts': before}
            for root in roots:
                atomic_json(root / MARKER, identity)
            journal_path = roots[1] / JOURNAL
            if journal_path.exists():
                previous = json.loads(journal_path.read_text(encoding='utf-8'))
                if previous['identity'] != identity:
                    raise ValueError('Reset journal belongs to another instance')
                for root in roots:
                    q = root / previous['quarantine']
                    if previous['phase'] == 'PREPARED' and q.exists():
                        for item in q.iterdir():
                            target = root / item.name
                            if target.exists():
                                raise ValueError('Interrupted reset has conflicting FILE entries')
                            os.replace(item, target)
                    remove_quarantine(root, previous['quarantine'])
            qname = '.arm2-reset-' + uuid4().hex
            journal = {'identity': identity, 'quarantine': qname, 'phase': 'PREPARED'}
            atomic_json(journal_path, journal)
            try:
                for root in roots:
                    q = root / qname
                    q.mkdir()
                    for item in list(root.iterdir()):
                        if item.name not in {MARKER, JOURNAL, qname}:
                            os.replace(item, q / item.name)
                db.execute(text('LOCK TABLE ' + ','.join('"' + t + '"' for t in TABLES) + ' IN ACCESS EXCLUSIVE MODE'))
                db.execute(text('TRUNCATE TABLE ' + ','.join('"' + t + '"' for t in TABLES) + ' CONTINUE IDENTITY'))
                after = counts(db)
                if any(after.values()) or db.scalar(text('SELECT version_num FROM alembic_version')) != revision:
                    raise RuntimeError('Reset postcondition failed')
                db.commit()
            except BaseException:
                db.rollback()
                # Journal/quarantine deliberately remain: outcome may be unknown after a connection failure.
                # Re-running this same identity recovers PREPARED, then repeats the complete transaction.
                raise
            atomic_json(journal_path, {**journal, 'phase': 'COMMITTED'})
            for root in roots:
                remove_quarantine(root, qname)
            journal_path.unlink()
            if any(p.name != MARKER for p in roots[0].iterdir()) or any(p.name != MARKER for p in roots[1].iterdir()):
                raise RuntimeError('FILE/runtime reset is incomplete; repeat the same command')
            return {'result': 'PASS', 'identity': identity, 'counts': after, 'migration': revision,
                    'bootstrap_preserved': True, 'file_entries': 0, 'runtime_entries': 0}
        finally:
            db.rollback()
            db.execute(text('SELECT pg_advisory_unlock(720070, 1)'))
            db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--file-root', required=True)
    parser.add_argument('--data-root', required=True)
    args = parser.parse_args()
    try:
        attestation = json.loads(os.environ.get('ARM2_UAT_RESET_ATTESTATION', '{}'))
        result = reset(engine(), attestation, args.file_root, args.data_root, execute=args.execute)
    except Exception:
        raise SystemExit('R2 reset refused or interrupted. No best-effort continuation. Check isolated identity/environment and retry the same instance after recovery; credentials suppressed.') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
