"""One lazy worker, one latest request/result slot; no Kodi imports or UI refs."""
from threading import Condition, Thread
from .dashboard_http import DashboardStatusReadClient
from .dashboard_http import DashboardResult
from time import time


class DashboardWorker:
    def __init__(self, factory=DashboardStatusReadClient):
        self.factory = factory
        self.condition = Condition()
        self.thread = None
        self.closed = False
        self.token = None
        self.pending = None
        self.result = None

    def request(self, config):
        with self.condition:
            if self.closed:
                raise ValueError('closed')
            token = object()
            self.token = token
            self.pending = (token, config)
            self.result = None
            if self.thread is None:
                self.thread = Thread(target=self._run, name='ChannelsDashboard', daemon=True)
                self.thread.start()
            self.condition.notify()
            return token

    def cancel(self):
        with self.condition:
            self.token = self.pending = self.result = None

    def _cancelled(self, token):
        with self.condition:
            return self.closed or token is not self.token

    def _run(self):
        while True:
            with self.condition:
                while not self.closed and self.pending is None:
                    self.condition.wait()
                if self.closed:
                    return
                assert self.pending is not None
                token, config = self.pending
                self.pending = None
            try:
                result = self.factory(config, lambda: self._cancelled(token)).read()
            except Exception:
                result = DashboardResult(None, time(), tuple((n, None) for n in
                    ('server', 'dvr', 'activity', 'sources', 'passes')), True)
            with self.condition:
                if not self.closed and token is self.token:
                    self.result = (token, result)
                self.condition.notify_all()

    def poll(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.token = self.pending = self.result = None
            self.condition.notify()
