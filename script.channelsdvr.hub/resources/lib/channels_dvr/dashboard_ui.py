"""Owner-only Dashboard coordinator; renders sanitized cached DTOs only."""
from time import monotonic, time
from .dashboard_models import DashboardState
from .dashboard_worker import DashboardWorker
from .guide import time_label


class DashboardUI:
    def __init__(self, window, worker=None):
        self.window = window
        self.worker = worker or DashboardWorker()
        self.state = DashboardState()
        self.binding = None
        self.token = None
        self.due = 0.0
        self.painted = 0.0
        self.admitted = False
        self._summary_scope = None
        self.ready_cards: set[int] = set()

    def _source(self):
        w = self.window
        s = w._live_session
        if (w._closed or w._development_blocked is not None or s is None
                or w._development_config is None or w._guide_runtime._mode is not None):
            return None
        return (w._development_config, s, s._generation, s.actual_version, s._version_blocked)

    def poll(self):
        w = self.window
        source = self._source()
        changed = False
        if source != self.binding:
            self.worker.cancel()
            self.binding, self.token = source, None
            self.state = DashboardState()
            self.due = 0
            changed = True
        if w.view != 'Dashboard':
            if self.token is not None:
                self.worker.cancel()
                self.token = None
            return changed  # no saved-setting/profile lookup on Guide/leaf Info
        admitted = (source is not None and w.get_setting('development_metadata_enabled') is True
                    and w._saved_config() == source[0])
        if admitted != self.admitted:
            self.admitted = admitted
            changed = True
        if not admitted:
            if self.token is not None or self.state.values or self.state.reported_version is not None:
                changed = True
            self.worker.cancel()
            self.token = None
            self.state = DashboardState()
            self.due = 0
            return changed
        if self._summary_scope is None or self._summary_scope[0] is not source[1]:
            self._summary_scope = (source[1], source[0])
        result = self.worker.poll()
        if result is not None and self.token is not None and result[0] is self.token:
            self.state.merge(result[1])
            self.token = None
            self.due = monotonic()+30
            changed = True
        now = monotonic()
        if self.token is None and now >= self.due:
            self.token = self.worker.request(source[0])
            self.due = now+30
        if now-self.painted >= 1:
            self.painted = now
            changed = True
        return changed

    def refresh(self):
        self.worker.cancel()
        self.token = None
        self.due = 0

    def suspend(self):
        self.worker.cancel()
        self.token = None

    def close(self):
        self.worker.close()

    def _running_label(self, value):
        # Pure presentation of the accepted server duration, never host uptime.
        if type(value) is not int or not 0 <= value <= 2**53:
            return self.window.t('unknown')
        days, remainder = divmod(value, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return self.window.t('dashboard_duration', days=days, hours=hours, minutes=minutes, seconds=seconds)

    def cards(self):
        w = self.window
        unknown = w.t('unknown')
        source = self._source()
        admitted = source is not None and source == self.binding and self.admitted
        state = self.state if admitted else DashboardState()
        def value(name, fields):
            resource = state.values.get(name)
            values = dict(resource.values) if resource else {}
            result = {k: values.get(k) if values.get(k) is not None else unknown for k in fields}
            result = {k: w.t('yes' if v else 'no') if type(v) is bool else v for k, v in result.items()}
            age = w.t('updated_ago', count=max(0, int(time()-resource.fetched_at))) if resource else w.t('update_age_unknown')
            if resource and (resource.unavailable or time()-resource.fetched_at > 60):
                age += ' · ' + w.t('cache_previous')
            result['age'] = age
            return result
        server = value('server', ('version', 'os', 'arch', 'running'))
        # Rejected status diagnostics must not caption old A values as build B.
        server['version'] = state.unsupported_version or state.accepted_version or unknown
        server['running'] = self._running_label(server['running'])
        session = w._live_session
        summary_scope = self._summary_scope
        summaries = (session.summaries if admitted and session and not session._version_blocked
                     and state.unsupported_version is None and summary_scope is not None
                     and summary_scope[0] is session and summary_scope[1] == source[0] else {})
        version = state.accepted_version or (session.actual_version if session else None)
        summaries = {k: v for k, v in summaries.items() if v.version == version and v.namespace == source[0].namespace}
        def loaded(name):
            item = summaries.get(name)
            if item is None:
                return unknown
            age = w.t('updated_ago', count=max(0, int(time()-item.fetched_at)))
            if state.error:
                age += ' · ' + w.t('cache_previous')
            return str(item.loaded) + ' (' + age + ')'
        channels = summaries.get('channels')
        events = summaries.get('events')
        counts = dict(channels.counts) if channels else {}
        return (
            w.t('dashboard_server', **server),
            w.t('dashboard_dvr', **value('dvr', ('files', 'total', 'jobs', 'rules', 'groups', 'storage', 'enabled', 'busy'))),
            w.t('dashboard_sources', **value('sources', ('sources', 'tuners', 'reporting', 'unknown'))),
            w.t('dashboard_passes', **value('passes', ('total', 'paused', 'active', 'unknown'))),
            w.t('dashboard_loaded', channels=loaded('channels'), hidden=counts.get('hidden', unknown),
                events=loaded('events'), recordings=loaded('recordings'),
                start=time_label(events.horizon[0]) if events else unknown,
                end=time_label(events.horizon[1]) if events else unknown),
            w.t('dashboard_activity', **value('activity', ('streams', 'recordings', 'workers', 'other'))),
        )

    def render(self):
        w = self.window
        w._ensure_pool(0)
        w._label(110, w.t('header', view=w.t('dashboard')))
        for ident in (112, 113, 114, *range(120, 126), *range(140, 144)):
            w._label(ident, '')
        for ident, body in enumerate(self.cards(), 400):
            memo = w._chrome.memo
            control = w._chrome.control(ident)
            if control is None:
                self.ready_cards.discard(ident)
                continue
            if memo.values.get((control, 'setText')) != (body,):
                self.ready_cards.discard(ident)
                memo.best_effort(control, 'setVisible', False)
            if memo.best_effort(control, 'setText', body):
                self.ready_cards.add(ident)
            else:
                self.ready_cards.discard(ident)
                memo.best_effort(control, 'setVisible', False)
                memo.best_effort(control, 'setText', '')
        w._label(112, w.t('connection_prompt') if not self.admitted or self._source() != self.binding else
                 w.t('live_unsupported') if self.state.unsupported_version is not None
                 else w.t('live_outage') if self.state.error else '')
        w._label(115, w.t('dashboard_hint'))
