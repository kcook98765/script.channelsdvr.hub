"""Worker-only portable SQLite operation roots, claims and explicit recovery.

Profile isolation/inherited permissions are policy, not a Unix permission proof.
No transaction survives an API call. Durable claims have no lease or takeover.
"""
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import os
from pathlib import Path, PureWindowsPath
import secrets
from types import ModuleType

try:
    import sqlite3 as _sqlite3
except ImportError:
    sqlite3: ModuleType | None = None
else:
    sqlite3 = _sqlite3

DB_NAME = 'operation-history.sqlite3'
BUSY_MS = 250
MAX_COUNTER = 9223372036854775806
LEGACY_PATHS = (('recording-owned.json',), ('recording-owned.tmp',),
                ('programme-passes', 'owned.json'), ('programme-passes', 'owned.tmp'))


class StorageError(Exception):
    """Sanitized diagnostic; never exposes database paths or SQL exceptions."""


class Scope(Enum):
    RECORDING = 'recording'
    PROGRAMME = 'programme'
    SERIES = 'series'  # Typed recovery API; operator UI/manager integration separate.

    @property
    def max_bytes(self):
        return 131072 if self is Scope.RECORDING else 8388608


def normalize_profile(profile):
    """Lexical native-local normalization only; no filesystem access on UI owner."""
    if (type(profile) is not str or not profile.strip() or '\x00' in profile
            or '://' in profile or not os.path.isabs(profile)):
        raise StorageError('profile_unavailable')
    # Pure syntax on every host: reject recognizable shares/device namespaces,
    # including forward-slash UNC, but retain extended local drive spelling.
    windows = PureWindowsPath(profile)
    drive = windows.drive
    extended_local = (len(drive) == 6 and drive.startswith('\\\\?\\')
                      and drive[4].isascii() and drive[4].isalpha() and drive[5] == ':'
                      and windows.root == '\\')
    if (profile.startswith(('//', '\\\\')) or drive.startswith('\\\\')) and not extended_local:
        raise StorageError('profile_unavailable')
    path = os.path.abspath(os.path.normpath(profile))
    if os.path.dirname(path) == path:
        raise StorageError('profile_unavailable')
    return path


def _scope(scope):
    if type(scope) is not Scope:
        raise StorageError('checkpoint_failed')
    return scope


def _legacy(profile):
    from .storage_migration import inventory, read_legacy
    found = inventory(profile)
    db = None
    try:
        if os.path.exists(os.path.join(profile, DB_NAME)):
            db = _readonly(profile)
            if db.execute("SELECT name FROM sqlite_master WHERE name='imports'").fetchone():
                _schema(db)
                receipts = _imports(db)
                if receipts:
                    loaded = read_legacy(profile)
                    if {r.scope.value:r.original for r in loaded} != {r[0]:r[1] for r in receipts}:
                        raise StorageError('checkpoint_failed')
                    return
        if found:
            raise StorageError('legacy history requires exclusive migration')
    finally:
        if db is not None:
            db.close()


def _fingerprint(payload):
    return hashlib.sha256(payload).hexdigest() if payload is not None else None


@dataclass(frozen=True)
class Snapshot:
    scope: Scope
    fence: int
    revision: int
    owner: str | None = field(repr=False)
    fingerprint: str | None = field(repr=False)
    payload: bytes | None = field(repr=False)
    barrier: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ProfileInspection:
    schema: int
    roots: tuple[Snapshot, ...] = field(repr=False)
    legacy: tuple[int, ...]
    provenance: tuple = field(default=(), repr=False)


def _readonly(profile):
    if sqlite3 is None:
        raise StorageError('checkpoint_failed')
    uri = Path(os.path.join(profile, DB_NAME)).as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, timeout=BUSY_MS / 1000, isolation_level=None)
    try:
        db.execute('BEGIN')
        return db
    except Exception:
        db.close()
        raise


def _schema(db, version=None):
    if version is None:
        version = db.execute('PRAGMA user_version').fetchone()[0]
    expected = (('kind','TEXT',0,None,1), ('fence','INTEGER',1,None,0), ('revision','INTEGER',1,None,0),
                ('owner','TEXT',0,None,0), ('fingerprint','TEXT',0,None,0), ('payload','BLOB',0,None,0))
    tables: dict[str, tuple] = {'scopes':expected}
    if version == 2:
        tables['imports'] = (('kind','TEXT',0,None,1), ('original','BLOB',1,None,0),
                             ('digest','TEXT',1,None,0), ('canonical_digest','TEXT',1,None,0))
        tables['barriers'] = (('kind','TEXT',0,None,1), ('token','TEXT',1,None,0), ('reason','TEXT',1,None,0))
    elif version != 1:
        raise StorageError('checkpoint_failed')
    objects = set(db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    if objects != {('table',name) for name in tables}:
        raise StorageError('checkpoint_failed')
    for name, columns in tables.items():
        # Identifiers are closed constants above, never external input.
        actual = tuple((r[1],r[2],r[3],r[4],r[5]) for r in db.execute('PRAGMA table_info(' + name + ')'))
        kinds = db.execute('SELECT kind FROM ' + name).fetchall()
        if actual != columns or len(kinds) > len(Scope) or any(r[0] not in {s.value for s in Scope} for r in kinds):
            raise StorageError('checkpoint_failed')
    if version == 2:
        for kind, token, reason in db.execute('SELECT kind,token,reason FROM barriers'):
            if type(token) is not str or len(token) != 64 or any(c not in '0123456789abcdef' for c in token) or reason not in {'import','recovery'}:
                raise StorageError('checkpoint_failed')
    return version


def _upgrade(db):
    if _schema(db) == 1:
        db.execute('CREATE TABLE imports (kind TEXT PRIMARY KEY, original BLOB NOT NULL, digest TEXT NOT NULL, canonical_digest TEXT NOT NULL)')
        db.execute('CREATE TABLE barriers (kind TEXT PRIMARY KEY, token TEXT NOT NULL, reason TEXT NOT NULL)')
        db.execute('PRAGMA user_version=2')


def _imports(db):
    from .storage_migration import decode_root
    rows = []
    for kind, size in db.execute('SELECT kind,length(original) FROM imports'):
        scope = Scope(kind)
        if scope is Scope.SERIES or type(size) is not int or not 0 < size <= scope.max_bytes:
            raise StorageError('checkpoint_failed')
        original, digest, canonical_digest = db.execute('SELECT original,digest,canonical_digest FROM imports WHERE kind=?',(kind,)).fetchone()
        if type(original) is not bytes or _fingerprint(original) != digest or _fingerprint(decode_root(scope,original)[1]) != canonical_digest:
            raise StorageError('checkpoint_failed')
        current = _read(db,scope)
        if current.payload is None:
            raise StorageError('checkpoint_failed')  # Lost imported root is never a new empty store.
        decode_root(scope,current.payload)
        rows.append((kind,original,digest,canonical_digest))
    return tuple(sorted(rows))


def _inspection(db, profile):
    from .storage_migration import inventory
    version = _schema(db)
    if db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
        raise StorageError('checkpoint_failed')
    receipts = _imports(db) if version == 2 else ()
    return ProfileInspection(version,tuple(_read(db,s) for s in Scope),inventory(profile),
                             tuple((r[0],r[2],r[3]) for r in receipts))


def _snapshot(scope, row):
    if row is None:
        return Snapshot(scope, 0, 0, None, None, None)
    fence, revision, owner, fingerprint, payload = row
    if (type(fence) is not int or not 1 <= fence <= MAX_COUNTER
            or type(revision) is not int or not 0 <= revision <= MAX_COUNTER
            or (owner is not None and (type(owner) is not str or len(owner) != 64
                                      or any(c not in '0123456789abcdef' for c in owner)))
            or (payload is None and (fingerprint is not None or revision != 0))
            or (payload is not None and (type(payload) is not bytes
                or not 0 < len(payload) <= scope.max_bytes or revision == 0))
            or fingerprint != _fingerprint(payload)):
        raise StorageError('checkpoint_failed')
    return Snapshot(scope, fence, revision, owner, fingerprint, payload)


def _read(db, scope):
    # Check size in SQL before materializing a potentially damaged oversized BLOB.
    size = db.execute('SELECT length(payload) FROM scopes WHERE kind=?', (scope.value,)).fetchone()
    if size is not None and size[0] is not None and not 0 < size[0] <= scope.max_bytes:
        raise StorageError('checkpoint_failed')
    result = _snapshot(scope, db.execute(
        'SELECT fence, revision, owner, fingerprint, payload FROM scopes WHERE kind=?', (scope.value,)).fetchone())
    if db.execute("SELECT name FROM sqlite_master WHERE name='barriers'").fetchone():
        row = db.execute('SELECT token FROM barriers WHERE kind=?',(scope.value,)).fetchone()
        if row is not None:
            if type(row[0]) is not str or len(row[0]) != 64 or any(c not in '0123456789abcdef' for c in row[0]):
                raise StorageError('checkpoint_failed')
            from dataclasses import replace
            result = replace(result,barrier=row[0])
    return result


class OperationStore:
    """Opaque bounded roots; domain adapters alone decode/validate envelopes.

    Claim owner + fence is the write capability. Revision + fingerprint is CAS.
    Every failure poisons this instance; release then leaves its claim in place.
    """
    def __init__(self, profile, scope):
        self._profile = normalize_profile(profile)
        self.scope = _scope(scope)
        self._db = None
        self._owner = None
        self._fence = None
        self._baseline = None
        self._loaded = False
        self._poison = False
        self._reconciling = False
        self._series_request: object | None = None

    def _connect(self, *, legacy_check=True):
        if sqlite3 is None:
            raise StorageError('checkpoint_failed')
        if legacy_check:
            _legacy(self._profile)
        os.makedirs(self._profile, exist_ok=True)
        db = sqlite3.connect(os.path.join(self._profile, DB_NAME), timeout=BUSY_MS / 1000,
                             isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=' + str(BUSY_MS))
            if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                raise StorageError('checkpoint_failed')
            db.execute('PRAGMA synchronous=FULL')
            if db.execute('PRAGMA synchronous').fetchone()[0] != 2:
                raise StorageError('checkpoint_failed')
            db.execute('BEGIN IMMEDIATE')
            # Another first opener may have initialized while this connection
            # waited. Decide schema identity only under the initialization lock.
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise StorageError('checkpoint_failed')
            if version == 0:
                # Never reinterpret an unknown pre-existing database as an empty store.
                if db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise StorageError('checkpoint_failed')
                db.execute('CREATE TABLE scopes (kind TEXT PRIMARY KEY, fence INTEGER NOT NULL, '
                           'revision INTEGER NOT NULL, owner TEXT, fingerprint TEXT, payload BLOB)')
                db.execute('PRAGMA user_version=1')
            _schema(db, 1 if version == 0 else version)
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _commit(self):
        assert self._db is not None
        self._db.commit()

    def _fail(self, error=None):
        self._series_request = None
        self._poison = True
        if self._db is not None:
            try:
                self._db.rollback()
            except Exception:
                pass
        message = 'legacy history requires exclusive migration' if (
            isinstance(error, StorageError) and str(error) == 'legacy history requires exclusive migration') else 'checkpoint_failed'
        raise StorageError(message) from None

    def _require(self):
        if self._poison or self._db is None or self._owner is None:
            raise StorageError('checkpoint_failed')

    def _check_claim(self, snapshot):
        if snapshot.owner != self._owner or snapshot.fence != self._fence:
            raise StorageError('checkpoint_failed')

    def acquire(self):
        self._series_request = None
        if self._db is not None or self._poison:
            raise StorageError('checkpoint_failed')
        try:
            self._db = self._connect()
            self._db.execute('BEGIN IMMEDIATE')
            old = _read(self._db, self.scope)
            if old.owner is not None or old.fence >= MAX_COUNTER:
                raise StorageError('checkpoint_failed')
            self._owner, self._fence = secrets.token_hex(32), old.fence + 1
            if old.fence == 0:
                self._db.execute('INSERT INTO scopes VALUES (?,?,?,?,?,?)',
                                 (self.scope.value, self._fence, 0, self._owner, None, None))
            else:
                self._db.execute('UPDATE scopes SET fence=?, owner=? WHERE kind=?',
                                 (self._fence, self._owner, self.scope.value))
            self._commit()  # durable claim precedes any domain/HTTP operation
        except Exception as error:
            self._fail(error)

    def load(self):
        self._require()
        assert self._db is not None
        try:
            _legacy(self._profile)
            self._db.execute('BEGIN')
            current = _read(self._db, self.scope)
            self._check_claim(current)
            if self._loaded and current != self._baseline:
                raise StorageError('checkpoint_failed')
            self._db.rollback()  # read snapshot ends before returning to worker
            self._baseline, self._loaded = current, True
            return current.payload
        except Exception as error:
            self._fail(error)

    def save(self, payload):
        self._series_request = None  # Any attempted root transition consumes older challenges.
        self._require()
        assert self._db is not None
        try:
            if (not self._loaded or type(payload) is not bytes or not 0 < len(payload) <= self.scope.max_bytes
                    or (self.pending_reconciliation and (self.scope is Scope.SERIES or not self._reconciling))):
                raise StorageError('checkpoint_failed')
            _legacy(self._profile)
            self._db.execute('BEGIN IMMEDIATE')
            current = _read(self._db, self.scope)
            self._check_claim(current)
            if current != self._baseline or current.revision >= MAX_COUNTER:
                raise StorageError('checkpoint_failed')
            fingerprint = _fingerprint(payload)
            self._db.execute('UPDATE scopes SET revision=?, fingerprint=?, payload=? WHERE kind=?',
                             (current.revision + 1, fingerprint, payload, self.scope.value))
            self._commit()
            self._baseline = Snapshot(self.scope, current.fence, current.revision + 1,
                                      self._owner, fingerprint, payload, current.barrier)
        except Exception as error:
            self._fail(error)

    def poison(self):
        """Domain decoding/encoding failure also forbids later release/adoption."""
        self._poison = True
        self._series_request = None

    @property
    def pending_reconciliation(self):
        return bool(self._baseline is not None and self._baseline.barrier is not None)

    def reconciliation_guard(self, operation):
        self._reconciling = operation == 'status' and self.scope is not Scope.SERIES
        if self.pending_reconciliation and operation not in {'status','history','abort'}:
            raise StorageError('checkpoint_failed')

    def finish_reconciliation(self):
        """Worker-only callback AFTER qualified existing domain read-only proof."""
        self._series_request = None
        self._require()
        assert self._db is not None
        try:
            if self.scope is Scope.SERIES or not self._reconciling or not self.pending_reconciliation:
                raise StorageError('checkpoint_failed')
            _legacy(self._profile)
            self._db.execute('BEGIN IMMEDIATE')
            current = _read(self._db,self.scope)
            self._check_claim(current)
            if current != self._baseline:
                raise StorageError('checkpoint_failed')
            self._db.execute('DELETE FROM barriers WHERE kind=? AND token=?',(self.scope.value,current.barrier))
            self._commit()
            from dataclasses import replace
            self._baseline = replace(current,barrier=None)
        except Exception as error:
            self._fail(error)

    def begin_series_reconciliation(self, source):
        """Load/claim first; issue a private exact-root challenge before fresh GETs."""
        from .series_reconciliation import issue_request
        self._series_request = None
        self._require()
        if self.scope is not Scope.SERIES or not self._loaded or not self.pending_reconciliation:
            raise StorageError('checkpoint_failed')
        self._series_request = issue_request(self._baseline,source)
        return self._series_request

    def begin_series_manager_reconciliation(self, source):
        from .series_reconciliation import issue_manager_request
        self._series_request = None
        self._require()
        if self.scope is not Scope.SERIES or not self._loaded:
            raise StorageError('checkpoint_failed')
        self._series_request = issue_manager_request(self._baseline, source)
        return self._series_request

    def apply_series_manager_reconciliation(self, proof):
        """Closed manager successor CAS; observations cannot mint write consent."""
        from .series_reconciliation import checked_manager_successor, challenged_snapshot
        request, self._series_request = self._series_request, None
        self._require()
        assert self._db is not None
        try:
            successor, disposition = checked_manager_successor(proof, request)
            challenged = challenged_snapshot(request)
            _legacy(self._profile)
            self._db.execute('BEGIN IMMEDIATE')
            current = _read(self._db, self.scope)
            self._check_claim(current)
            if self.scope is not Scope.SERIES or current != self._baseline or current != challenged:
                raise StorageError('checkpoint_failed')
            revision = current.revision
            if successor != current.payload:
                if type(successor) is not bytes or not 0 < len(successor) <= self.scope.max_bytes or revision >= MAX_COUNTER:
                    raise StorageError('checkpoint_failed')
                revision += 1
                self._db.execute('UPDATE scopes SET revision=?,fingerprint=?,payload=? WHERE kind=?',
                                 (revision,_fingerprint(successor),successor,self.scope.value))
            if disposition == 'clear' and current.barrier is not None:
                self._db.execute('DELETE FROM barriers WHERE kind=? AND token=?', (self.scope.value,current.barrier))
            self._commit()
            from dataclasses import replace
            self._baseline = replace(current, revision=revision, payload=successor, fingerprint=_fingerprint(successor),
                                     barrier=None if disposition == 'clear' else current.barrier)
            self._series_request = None
            return disposition
        except Exception as error:
            self._fail(error)

    def apply_series_reconciliation(self, proof):
        """Atomic checked successor + barrier clear; no caller boolean/callback."""
        from .series_reconciliation import checked_successor, challenged_snapshot
        request, self._series_request = self._series_request, None
        self._require()
        assert self._db is not None
        try:
            if self.scope is not Scope.SERIES or request is None or not self.pending_reconciliation:
                raise StorageError('checkpoint_failed')
            successor = checked_successor(proof,request)
            challenged = challenged_snapshot(request)
            _legacy(self._profile)
            self._db.execute('BEGIN IMMEDIATE')
            current = _read(self._db,self.scope)
            self._check_claim(current)
            if current != self._baseline or current != challenged:
                raise StorageError('checkpoint_failed')
            revision = current.revision
            if successor != current.payload:
                if type(successor) is not bytes or not 0 < len(successor) <= self.scope.max_bytes or revision >= MAX_COUNTER:
                    raise StorageError('checkpoint_failed')
                revision += 1
                self._db.execute('UPDATE scopes SET revision=?,fingerprint=?,payload=? WHERE kind=?',
                                 (revision,_fingerprint(successor),successor,self.scope.value))
            self._db.execute('DELETE FROM barriers WHERE kind=? AND token=?',(self.scope.value,current.barrier))
            self._commit()
            from dataclasses import replace
            self._baseline = replace(current,revision=revision,payload=successor,fingerprint=_fingerprint(successor),barrier=None)
            self._series_request = None
        except Exception as error:
            self._fail(error)

    def release(self):
        self._series_request = None
        db = self._db
        if db is None:
            return
        try:
            if not self._poison and self._owner is not None:
                db.execute('BEGIN IMMEDIATE')
                current = _read(db, self.scope)
                self._check_claim(current)
                if self._loaded and current != self._baseline:
                    raise StorageError('checkpoint_failed')
                db.execute('UPDATE scopes SET owner=NULL WHERE kind=? AND owner=? AND fence=?',
                           (self.scope.value, self._owner, self._fence))
                self._commit()
        except Exception:
            self._poison = True
            # No retry: commit may already have happened. Inspection is read-only.
        finally:
            db.close()
            self._db = None

    @staticmethod
    def inspect(profile, scope):
        """Read-only local intent, NOT backend reconciliation or ownership authority."""
        profile, scope = normalize_profile(profile), _scope(scope)
        db = None
        try:
            if sqlite3 is None:
                raise StorageError('checkpoint_failed')
            db = _readonly(profile)
            _schema(db)
            return _read(db, scope)
        except Exception:
            raise StorageError('checkpoint_failed') from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def recover(profile, snapshot, *, confirmed_exclusive=False, reconciled_read_only=False):
        """Explicit operator-only abandoned-claim release; never changes root bytes.

        Caller must stop all writers, reconcile the inspected domain intent using
        read-only backend evidence, and confirm both facts. No UI currently wires
        this API. Flags cannot establish exclusivity against old JSON writers.
        """
        if (confirmed_exclusive is not True or reconciled_read_only is not True
                or type(snapshot) is not Snapshot or snapshot.owner is None):
            raise StorageError('checkpoint_failed')
        expected = OperationStore.inspect_profile(profile)
        if expected.roots[list(Scope).index(snapshot.scope)] != snapshot:
            raise StorageError('checkpoint_failed')
        return OperationStore.recover_exclusive(profile,expected,snapshot.scope,confirmed=True)

    @staticmethod
    def inspect_profile(profile):
        from .storage_migration import inventory
        profile = normalize_profile(profile)
        if sqlite3 is None:
            raise StorageError('checkpoint_failed')
        db = None
        try:
            if not os.path.exists(os.path.join(profile,DB_NAME)):
                return ProfileInspection(0,tuple(_snapshot(s,None) for s in Scope),inventory(profile))
            db = _readonly(profile)
            return _inspection(db,profile)
        except Exception:
            raise StorageError('checkpoint_failed') from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def recover_exclusive(profile, expected, scope, *, confirmed=False):
        if confirmed is not True or type(expected) is not ProfileInspection or type(scope) is not Scope:
            raise StorageError('checkpoint_failed')
        store = OperationStore(profile,scope)
        try:
            store._db = store._connect()
            store._db.execute('BEGIN IMMEDIATE')
            snapshot = _read(store._db,scope)
            if (_inspection(store._db,store._profile) != expected or snapshot.owner is None
                    or snapshot.fence >= MAX_COUNTER):
                raise StorageError('checkpoint_failed')
            _upgrade(store._db)
            store._db.execute('UPDATE scopes SET owner=NULL, fence=fence+1 WHERE kind=?', (scope.value,))
            store._db.execute('INSERT OR REPLACE INTO barriers VALUES (?,?,?)',(scope.value,secrets.token_hex(32),'recovery'))
            store._commit()
        except Exception:
            raise StorageError('checkpoint_failed') from None
        finally:
            if store._db is not None:
                store._db.close()

    @staticmethod
    def import_legacy(profile, expected, *, confirmed=False):
        from .storage_migration import read_legacy, decode_root, root_binding
        if confirmed is not True or type(expected) is not ProfileInspection:
            raise StorageError('checkpoint_failed')
        profile = normalize_profile(profile)
        store = OperationStore(profile,Scope.RECORDING)
        try:
            if OperationStore.inspect_profile(profile) != expected:
                raise StorageError('checkpoint_failed')
            roots = read_legacy(profile)  # no read until explicit consent
            if not roots:
                raise StorageError('checkpoint_failed')
            store._db = store._connect(legacy_check=False)
            store._db.execute('BEGIN IMMEDIATE')
            actual = _inspection(store._db,profile)
            # A fresh schema1 with zero rows is the sole allowed absent-DB initialization.
            if expected.schema == 0:
                from dataclasses import replace
                actual = replace(actual,schema=0)
            if actual != expected or any(r.owner is not None for r in actual.roots):
                raise StorageError('checkpoint_failed')
            bindings = [r.binding for r in roots if r.binding is not None]
            for saved in actual.roots:
                if saved.payload is not None and saved.scope in (Scope.RECORDING,Scope.PROGRAMME):
                    binding = root_binding(saved.scope,decode_root(saved.scope,saved.payload)[0])
                    if binding is not None:
                        bindings.append(binding)
            if len(set(bindings)) > 1:
                raise StorageError('checkpoint_failed')
            _upgrade(store._db)
            if _imports(store._db):
                raise StorageError('checkpoint_failed')
            for root in roots:
                existing = _read(store._db,root.scope)
                if existing.payload is not None or existing.barrier is not None or existing.fence >= MAX_COUNTER:
                    raise StorageError('checkpoint_failed')
                store._db.execute('INSERT INTO imports VALUES (?,?,?,?)',
                    (root.scope.value,root.original,_fingerprint(root.original),_fingerprint(root.canonical)))
                if existing.fence == 0:
                    store._db.execute('INSERT INTO scopes VALUES (?,?,?,?,?,?)',
                        (root.scope.value,1,1,None,_fingerprint(root.canonical),root.canonical))
                else:
                    store._db.execute('UPDATE scopes SET fence=fence+1,revision=1,fingerprint=?,payload=? WHERE kind=?',
                        (_fingerprint(root.canonical),root.canonical,root.scope.value))
                store._db.execute('INSERT INTO barriers VALUES (?,?,?)',(root.scope.value,secrets.token_hex(32),'import'))
            if read_legacy(profile) != roots:
                raise StorageError('checkpoint_failed')
            store._commit()
        except Exception:
            raise StorageError('checkpoint_failed') from None
        finally:
            if store._db is not None:
                store._db.close()
