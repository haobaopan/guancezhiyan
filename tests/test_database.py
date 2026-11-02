import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.database as module
from app.database import Database, MigrationError


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'app.sqlite3', busy_timeout_ms=4321)
        self.version = self.db.initialize()

    def test_idempotent_connections_and_foreign_keys(self):
        inode = self.db.path.stat().st_ino
        self.assertEqual(self.db.initialize(), self.version)
        self.assertEqual(self.db.path.stat().st_ino, inode)
        self.assertEqual(list(self.root.glob('*.bak')), [])
        con = self.db.connect()
        try:
            self.assertEqual(con.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 4321)
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        finally:
            con.close()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as con:
                con.execute("INSERT INTO conversations(conversation_id,user_id,project_id,title,created_at,updated_at) VALUES('c','missing','missing','','t','t')")

    def migrations(self, sql):
        folder = self.root / 'migrations'
        folder.mkdir()
        for source in module.MIGRATIONS_DIR.glob('*.sql'):
            (folder / source.name).write_bytes(source.read_bytes())
        (folder / f'{self.version+1:03d}_test.sql').write_text(sql, encoding='utf-8')
        return patch.object(module, 'MIGRATIONS_DIR', folder)

    def test_upgrade_backup_preserves_open_connection(self):
        con = self.db.connect()
        self.addCleanup(con.close)
        inode = self.db.path.stat().st_ino
        with self.migrations('CREATE TABLE marker(value TEXT);'):
            self.assertEqual(self.db.initialize(), self.version+1)
        self.assertEqual(inode, self.db.path.stat().st_ino)
        self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone())
        backups = list(self.root.glob('*.bak'))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(backup.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], self.version)
            self.assertEqual(backup.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            backup.close()

    def test_failed_upgrade_rolls_back_ddl(self):
        with self.migrations('CREATE TABLE marker(value TEXT); INSERT INTO projects(project_id,user_id,name,created_at,updated_at) VALUES(\'p\',\'missing\',\'x\',\'t\',\'t\');'):
            with self.assertRaises(MigrationError):
                self.db.initialize()
        con = self.db.connect()
        try:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone())
            self.assertEqual(con.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], self.version)
        finally:
            con.close()
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)


if __name__ == '__main__':
    unittest.main()
