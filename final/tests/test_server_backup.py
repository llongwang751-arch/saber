import sqlite3
from pathlib import Path

import pytest

from scripts.backup_server import backup, verify_restore


def test_online_backup_retention_and_isolated_restore(tmp_path):
    root = tmp_path / 'server'
    (root / 'runtime').mkdir(parents=True)
    database = root / 'runtime/application.db'
    with sqlite3.connect(database) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        for table in ('users', 'agent_documents', 'agent_rag_chunks', 'agent_long_term_memory', 'agent_chat_history'):
            conn.execute(f'CREATE TABLE {table} (id INTEGER)')
            conn.execute(f'INSERT INTO {table} VALUES (1)')
        conn.commit()
        output = root / 'backups'
        first = backup(root, output, keep=1)
        conn.execute('INSERT INTO users VALUES (2)')
        conn.commit()
        second = backup(root, output, keep=1)
    assert not Path(first['archive']).exists()
    assert Path(second['archive']).exists()
    restored = verify_restore(second['archive'], tmp_path / 'drill')
    assert restored['integrity'] == 'ok' and restored['counts']['users'] == 2
    with pytest.raises(FileExistsError):
        verify_restore(second['archive'], root / 'runtime')
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 2
