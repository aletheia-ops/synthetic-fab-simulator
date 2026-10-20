"""Disposable, bounded-page-cache identity lookup over immutable history.

Only canonical archive segments are authoritative. This database is private to
one runtime, is never checkpointed/reused from disk, and can be rebuilt with one
bounded archive segment resident at a time.
"""
import json
import sqlite3
import tempfile
from pathlib import Path
from .checkpoint_format import CheckpointError


class HistoryLookup:
    def __init__(self):
        self._temporary=tempfile.TemporaryDirectory(prefix='fab-history-index-')
        self.path=Path(self._temporary.name)/'lookup.sqlite'
        self._connection=sqlite3.connect(self.path)
        self._connection.execute('PRAGMA cache_size=-2048')
        self._connection.execute('PRAGMA temp_store=FILE')
        # Disposable and never reopened, so durability syncs buy nothing.
        self._connection.execute('PRAGMA synchronous=OFF')
        self._connection.execute('CREATE TABLE identities(namespace TEXT, identity TEXT, PRIMARY KEY(namespace,identity)) WITHOUT ROWID')
        self._heads={}

    def _namespace(self,head,kind):
        return json.dumps([head['directory'],kind],ensure_ascii=True,separators=(',',':'))

    def _ensure(self,head,kind):
        namespace=self._namespace(head,kind)
        if self._heads.get(kind)==(namespace,head['head']):return namespace
        from .history_archive import HistoryArchive
        with self._connection:
            old=self._heads.get(kind)
            if old:self._connection.execute('DELETE FROM identities WHERE namespace=?',(old[0],))
            self._connection.execute('DELETE FROM identities WHERE namespace=?',(namespace,))
            for item_kind,identity in HistoryArchive(head['directory']).identities(head):
                if item_kind!=kind:raise CheckpointError('history lookup kind mismatch')
                try:self._connection.execute('INSERT INTO identities VALUES (?,?)',(namespace,identity))
                except sqlite3.IntegrityError as error:raise CheckpointError('duplicate archived identity') from error
        self._heads[kind]=(namespace,head['head'])
        return namespace

    def prime(self,head,kind,identities):
        """Index identities already read from `head` (e.g. during restore)."""
        if head is None:return
        namespace=self._namespace(head,kind)
        with self._connection:
            self._connection.execute('DELETE FROM identities WHERE namespace=?',(namespace,))
            try:self._connection.executemany('INSERT INTO identities VALUES (?,?)',((namespace,i) for i in identities))
            except sqlite3.IntegrityError as error:raise CheckpointError('duplicate archived identity') from error
        self._heads[kind]=(namespace,head['head'])

    def contains(self,head,kind,identity):
        if head is None:return False
        namespace=self._ensure(head,kind)
        return self._connection.execute('SELECT 1 FROM identities WHERE namespace=? AND identity=?',
            (namespace,identity)).fetchone() is not None

    def advance(self,previous,head,kind,records):
        namespace=self._namespace(head,kind)
        if previous is None or self._heads.get(kind)!=(self._namespace(previous,kind),previous['head']):return
        with self._connection:
            for item in records:
                if item['kind']!=kind:raise CheckpointError('history lookup kind mismatch')
                try:self._connection.execute('INSERT INTO identities VALUES (?,?)',(namespace,item['id']))
                except sqlite3.IntegrityError as error:raise CheckpointError('duplicate archived identity') from error
        self._heads[kind]=(namespace,head['head'])

    def stats(self):
        return {'namespaces':len(self._heads),'page_cache_kib':2048,
            'disk_bytes':self.path.stat().st_size}

    def close(self):
        self._connection.close();self._heads.clear();self._temporary.cleanup()
