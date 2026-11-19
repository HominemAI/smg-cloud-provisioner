"""Durable, credential-free reservations for providers with multi-resource launches."""
import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Protocol


class LifecycleRepository(Protocol):
    def reserve(self, scope: str, name: str, resources: dict) -> None: ...
    def update(self, scope: str, name: str, phase: str, error=None) -> None: ...
    def records(self, scope: str) -> list[dict]: ...
    def save_resources(self, scope: str, name: str, resources: dict) -> None: ...


class UnresolvedLaunch(RuntimeError):
    """Inventory cannot yet prove a launch or cleanup reached a terminal state."""


class LifecycleStore:
    def __init__(self, path):
        if not path or path == ':memory:':
            raise ValueError('A persistent lifecycle database path is required')
        self.path = str(Path(path))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS launches (
                scope TEXT NOT NULL, name TEXT NOT NULL, phase TEXT NOT NULL,
                resources TEXT NOT NULL, updated REAL NOT NULL, error TEXT,
                PRIMARY KEY(scope, name))''')
            db.execute('DROP INDEX IF EXISTS one_active_launch')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_pending_launch
                ON launches(scope) WHERE phase IN ('reserved', 'submitting', 'uncertain') ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, scope, name, resources):
        try:
            with self.connect() as db:
                db.execute('INSERT INTO launches VALUES (?, ?, ?, ?, ?, NULL)',
                           (scope, name, 'reserved', json.dumps(resources), time.time()))
        except sqlite3.IntegrityError as exc:
            raise UnresolvedLaunch('An active reservation already exists for this pool') from exc

    def update(self, scope, name, phase, error=None):
        # Store only exception type names, never cloud messages/request bodies.
        with self.connect() as db:
            result = db.execute('UPDATE launches SET phase=?, updated=?, error=? WHERE scope=? AND name=?',
                                (phase, time.time(), type(error).__name__ if error else None, scope, name))
            if result.rowcount != 1:
                raise ValueError('Unknown launch reservation')

    def records(self, scope):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM launches WHERE scope=? AND phase != 'deleted'", (scope,)).fetchall()
        return [dict(row, resources=json.loads(row['resources'])) for row in rows]

    def save_resources(self, scope, name, resources):
        """Append server-assigned identifiers without rewriting established identity."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT resources FROM launches WHERE scope=? AND name=?', (scope, name)).fetchone()
            if row is None:
                raise ValueError('Unknown launch reservation')
            previous = json.loads(row['resources'])
            if any(key in resources and resources[key] != value for key, value in previous.items()):
                raise ValueError('Cannot replace a recorded resource identity')
            merged = previous | resources
            db.execute('UPDATE launches SET resources=?, updated=? WHERE scope=? AND name=?',
                       (json.dumps(merged), time.time(), scope, name))
