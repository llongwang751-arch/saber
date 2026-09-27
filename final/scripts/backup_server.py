"""Online SQLite backup with integrity verification, private archives and retention.

Run as the deployment operator. Chroma is derived and deliberately rebuilt on
restore, rather than copying its live index files without a consistent snapshot.
"""
from pathlib import Path
import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone


def backup_database(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup(root, output, keep=7, *, application_db=None, run_db=None, require_runs=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if keep < 1:
        raise ValueError('Retention must be positive')
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = output / ('agi-saber-' + stamp + '.tar.gz')
    # Keep incomplete snapshots out of the retention set.
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=output) as temporary:
        temporary = Path(temporary)
        db = temporary / 'application.db'
        application_source = Path(application_db) if application_db else root / 'runtime/application.db'
        backup_database(application_source, db)
        manifest = {'created_at': stamp, 'sqlite_sha256': digest(db),
                    'derived_chroma_included': False, 'schema': 2,
                    'consistency': 'independent-online-snapshots; drain writes for a coordinated restore',
                    'databases': {'application.db': digest(db)}}
        candidates = [Path(run_db)] if run_db else [
            application_source.with_name(application_source.stem + '.agent-runs.sqlite3'),
            application_source.with_name('agent_runs.sqlite3'),
        ]
        run_source = next((path for path in candidates if path.is_file()), None)
        if run_source is None and (run_db or require_runs):
            raise FileNotFoundError('Required run ledger is missing')
        if run_source is not None:
            run_copy = temporary / 'agent_runs.sqlite3'
            backup_database(run_source, run_copy)
            manifest['databases']['agent_runs.sqlite3'] = digest(run_copy)
        (temporary / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        pending = temporary / 'archive.tar.gz'
        with tarfile.open(pending, 'w:gz') as archive:
            archive.add(db, arcname='runtime/application.db')
            if run_source is not None:
                archive.add(run_copy, arcname='runtime/agent_runs.sqlite3')
            archive.add(temporary / 'manifest.json', arcname='manifest.json')
            for name in ('server.env', 'recovery.env', 'app/config'):
                path = root / name
                if path.exists():
                    archive.add(path, arcname=name)
            artifacts = root / 'runtime/home'
            if artifacts.exists():
                archive.add(artifacts, arcname='runtime/home')
        os.chmod(pending, 0o600)
        os.replace(pending, destination)
    # Delete only archives produced by this script, after the new one is complete.
    archives = sorted(output.glob('agi-saber-*.tar.gz'), reverse=True)
    for expired in archives[keep:]:
        if expired.is_file() and not expired.is_symlink() and expired.parent.resolve() == output:
            expired.unlink()
    return {'archive': str(destination), 'bytes': destination.stat().st_size,
            'sha256': digest(destination)}


def verify_restore(archive, destination):
    """Restore a separate database for a drill; never overwrite live data."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Restore drill destination already exists')
    destination.mkdir(parents=True, mode=0o700)
    db = destination / 'application.db'
    with tarfile.open(archive, 'r:gz') as bundle:
        manifest = json.load(bundle.extractfile('manifest.json'))
        # Extract just the named data member, never arbitrary archive paths.
        with bundle.extractfile('runtime/application.db') as src, db.open('xb') as dst:
            import shutil
            shutil.copyfileobj(src, dst)
        if 'agent_runs.sqlite3' in manifest.get('databases', {}):
            ledger = destination / 'agent_runs.sqlite3'
            with bundle.extractfile('runtime/agent_runs.sqlite3') as src, ledger.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(ledger, 0o600)
            if digest(ledger) != manifest['databases']['agent_runs.sqlite3']:
                raise RuntimeError('Restored run ledger checksum mismatch')
            with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as connection:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Restored run ledger integrity check failed')
                if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise RuntimeError('Restored run ledger has broken references')
    os.chmod(db, 0o600)
    if digest(db) != manifest['sqlite_sha256']:
        raise RuntimeError('Restored database checksum mismatch')
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        counts = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('users', 'agent_documents', 'agent_rag_chunks', 'agent_long_term_memory', 'agent_chat_history')}
    if integrity != 'ok':
        raise RuntimeError('Restored database integrity check failed')
    return {'integrity': integrity, 'counts': counts, 'destination': str(db),
            'run_ledger_restored': 'agent_runs.sqlite3' in manifest.get('databases', {})}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/agi-saber')
    parser.add_argument('--output', default='/opt/agi-saber/backups')
    parser.add_argument('--keep', type=int, default=7)
    parser.add_argument('--verify-archive')
    parser.add_argument('--restore-dir')
    parser.add_argument('--application-db')
    parser.add_argument('--run-db')
    parser.add_argument('--require-runs', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    result = verify_restore(args.verify_archive, args.restore_dir) if args.verify_archive else backup(
        args.root, args.output, args.keep, application_db=args.application_db, run_db=args.run_db,
        require_runs=args.require_runs,
    )
    print(json.dumps(result, ensure_ascii=True))
