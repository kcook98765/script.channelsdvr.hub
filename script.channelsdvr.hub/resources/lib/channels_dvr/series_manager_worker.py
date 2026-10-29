"""One FIFO slot and one review, worker-confined HTTP/SQLite, signal-only close."""
import threading

from .newbuild_compatibility import profile_binding
from .series_http import SeriesHTTP
from .series_manager_models import ManagerView, ManagerError
from .series_manager_store import SeriesManagerStore
from .series_manager_commands import SeriesManagerCommands
from .control_lifetime import ControlWorkerTicket


class SeriesManagerWorker:
    def __init__(self, config, profile, *, client_factory=SeriesHTTP, store_factory=SeriesManagerStore):
        self.config = config
        self._profile = profile  # Already translated own add-on profile, not a host path default.
        self._client_factory, self._store_factory = client_factory, store_factory
        self._condition = threading.Condition()
        self._closed = self._busy = False
        self._intent: tuple[str, dict] | None = None
        self._result: ManagerView | None = None
        self._thread: threading.Thread | None = None
        self._ticket = ControlWorkerTicket()

    def __repr__(self):
        return 'SeriesManagerWorker(private=True)'

    def submit(self, operation, **values):
        if operation not in {'list', 'status', 'details', 'prepare', 'abort', 'create', 'adopt',
                             'refresh_consent', 'rename', 'pause', 'resume', 'remove'}:
            return False
        with self._condition:
            # An unread result retains the sole slot; no dropped review/result.
            if self._closed or self._busy or self._result is not None:
                return False
            self._intent, self._busy = (operation, values), True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='channels-series', daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def poll(self):
        with self._condition:
            result, self._result = self._result, None
            return None if self._closed else result

    def close(self):
        with self._condition:
            self._closed = True
            if self._thread is None:
                self._ticket.finish()
            self._condition.notify_all()  # No join, SQL or HTTP on owner.

    def _run(self):
        store = None
        commands = None
        failed = False
        try:
            binding = profile_binding(self.config.origin, self.config.namespace, self.config.expected_version)
            store = self._store_factory(self._profile, binding)
            store.open()
            commands = SeriesManagerCommands(self.config, store, self._client_factory)
        except Exception:
            failed = True
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._intent is not None)
                    if self._closed:
                        return  # A queued unstarted command has no dispatch authority.
                    intent, self._intent = self._intent, None
                assert intent is not None
                operation, values = intent
                try:
                    if failed or commands is None:
                        raise ManagerError('storage')
                    if operation in {'list', 'status', 'details', 'prepare', 'abort'}:
                        result = getattr(commands, operation)(**values)
                    else:
                        result = commands.execute(operation, **values)
                except ManagerError as error:
                    code = error.code
                    if store is not None and store.root.get('pending') is not None:
                        pending = store.root['history'][store.root['pending']]
                        if pending['dispatched'] and code != 'storage':
                            code = 'pending'  # Never claim "no change sent" after dispatch.
                    result = ManagerView(operation, error=code)
                except Exception:
                    result = ManagerView(operation, error='schema')
                # Mutations have fully persisted BEFORE generation/close is considered.
                with self._condition:
                    self._busy = False
                    if not self._closed:
                        self._result = result
        finally:
            try:
                if store is not None:
                    store.close()  # Leaves unknown intent intact; no backend Exit cleanup.
            finally:
                self._ticket.finish()
