"""Owner-only Dashboard coordinator; renders sanitized cached DTOs only."""
from time import monotonic
from datetime import datetime
import re
from .dashboard_models import DashboardState
from .dashboard_worker import DashboardWorker
from .guide import time_label
from .build_status_ui import current as current_status, fresh as fresh_status
from .metadata_http import valid_status_sample


def refresh_seconds(value):
    """Closed persisted minute values, not a spinner index or arbitrary duration."""
    return int(value) * 60 if type(value) is str and value in ('0', '1', '2', '5', '10', '15', '30') else 300


def format_timestamp(stamp, region=None):
    """Local absolute time; optional Kodi region provider is adapter-injected."""
    local = datetime.fromtimestamp(stamp).astimezone()
    fallback = '%Y-%m-%d %H:%M:%S %z'
    if region is not None:
        try:
            date, clock = region('dateshort'), region('time')
            if (type(date) is str and type(clock) is str and date and clock
                    and len(date) + len(clock) <= 128
                    and re.search(r'%[-#]?[Yyx]', date)):
                pattern = re.sub(r'(%[HI])\1', r'\1', date + ' ' + clock)
                # Reject unknown directives/control characters, not regional text.
                remainder = re.sub(r'%[-#]?[aAbBdeHIjmMpSUwWxXyYzZ%]', '', pattern)
                if '%' not in remainder and not any(ord(c) < 32 for c in pattern):
                    return local.strftime(pattern + ('' if '%z' in pattern else ' %z'))
        except (RuntimeError, AttributeError, TypeError, ValueError, OverflowError):
            pass
    return local.strftime(fallback)


class DashboardUI:
    def __init__(self, window, worker=None, *, timestamp=format_timestamp):
        self.window = window
        self.worker = worker or DashboardWorker()
        self.state = DashboardState()
        self.binding = None
        self.token = None
        self.completed = None
        self.accepted_at = None
        self.timestamp = timestamp
        self.interval = 300
        self.manual = False
        self._presentation = None
        self.admitted = False
        self._summary_scope = None
        self.ready_cards: set[int] = set()
        self._seen_status = None
        self._requested_at = None
        self._verified_after = None
        self._pending_version = None

    def _source(self):
        w = self.window
        s = w._live_session
        if (w._closed or w._development_blocked is not None or s is None
                or w._development_config is None or w._guide_runtime._mode is not None):
            return None
        # Generation, actual_version and block describe listing admission. Stats
        # have their own serialized, version-bookended read and source token.
        return (w._development_config, s)

    def _reset(self):
        self.worker.cancel()
        self.token = None
        self.state = DashboardState()
        self.completed = self.accepted_at = None
        self._requested_at = self._verified_after = None
        self._seen_status = self._pending_version = None
        self.manual = False

    def _observe_version(self):
        # Reuse the exact current receipt/config/session/settings checks. Neither
        # a listing block nor a cached actual_version is fresh status evidence.
        published = self.window._guide_runtime.latest_status
        if published is None or published is self._seen_status:
            return False
        sample = getattr(published[0], 'sample', None)
        if (sample is not None and valid_status_sample(sample) and not fresh_status(sample)
                and sample.observed_at <= monotonic()):
            self._seen_status = published  # expired evidence cannot become fresh
            return False
        latest = current_status(self.window)
        if latest is None:
            return False  # a still-fresh publication can become eligible later
        sample = latest[0].sample
        if not fresh_status(sample):
            return False
        self._seen_status = published
        if self._verified_after is not None and sample.observed_at < self._verified_after:
            return False  # older than a subsequently admitted stats read
        version = sample.reported_version
        known = self.state.accepted_version or self.state.unsupported_version
        changed = known is not None and version != known
        if changed:
            # This is a real contradiction of accepted stats, not listing churn.
            # An earlier pending failure must not complete/renew this old scope.
            self.worker.cancel()
            self.token = self._requested_at = self._pending_version = None
            self.state = DashboardState()
            self.completed = self.accepted_at = None
        if (self.token is not None and self._requested_at is not None
                and sample.observed_at >= self._requested_at):
            # Keep an initial/current read through ordinary corroboration. A
            # contradictory ready/late result must be retried AFTER this proof.
            self._pending_version = version
        return changed

    def poll(self):
        w = self.window
        if w._closed:
            return False
        source = self._source()
        changed = False
        if source != self.binding:
            self._reset()
            self.binding = source
            changed = True
        if w.view != 'Dashboard':
            if self.token is not None:
                self.suspend()
            return changed  # no saved-setting/profile lookup on Guide/leaf Info
        admitted = (source is not None and w.get_setting('development_metadata_enabled') is True
                    and w._saved_config() == source[0])
        if admitted != self.admitted:
            self.admitted = admitted
            changed = True
        if not admitted:
            if (self.token is not None or self.state.values or self.state.reported_version is not None
                    or self.state.error or self.completed is not None):
                changed = True
                self._reset()
            else:
                self.worker.cancel()
                self.manual = False
            return self._changed() or changed
        try:
            self.interval = refresh_seconds(w.get_setting('dashboard_refresh_minutes'))
        except (RuntimeError, AttributeError, TypeError, ValueError):
            self.interval = 300
        if self._summary_scope is None or self._summary_scope[0] is not source[1]:
            self._summary_scope = (source[1], source[0])
        changed = self._observe_version() or changed
        result = self.worker.poll()
        if result is not None and self.token is not None and result[0] is self.token:
            item = result[1]
            positive = item.coherent is True or item.unsupported
            if positive and self._pending_version is not None and item.version != self._pending_version:
                # The result predates contradictory current status evidence.
                # Cancel its token; only a fresh bookended read can replace it.
                self.worker.cancel()
                self.state = DashboardState()
                self.completed = self.accepted_at = None
            else:
                if self.state.merge(item):
                    self.accepted_at = monotonic()
                if positive:
                    self._verified_after = self._requested_at
                self.completed = monotonic()
                self.manual = False
            self.token, self._pending_version = None, None
            changed = True
        now = monotonic()
        if self.token is None and (self.completed is None or self.manual
                or (self.interval and now >= self.completed + self.interval)):
            self._requested_at = now
            self._pending_version = None
            self.token = self.worker.request(source[0])
        return self._changed() or changed

    def _changed(self):
        # Compare only compact owner-side semantics. No translations, timestamp
        # formatting or card construction on the 50-ms idle owner-loop path.
        admitted = self.admitted and self._source() == self.binding
        summaries = self._summaries() if admitted else {}
        presentation = (admitted, self.state, self.state.revision, self.token is not None,
                        tuple((name, self._stale(value)) for name, value in self.state.values.items()),
                        tuple((name, value, self.window._live_session.summary_health(name))
                              for name, value in summaries.items()))
        changed = presentation != self._presentation
        self._presentation = presentation
        return changed

    def refresh(self):
        # An in-flight cycle already satisfies Refresh; never restart/starve it.
        if self.token is None:
            self.manual = True

    def suspend(self):
        self.worker.cancel()
        self.token = None
        self._requested_at = self._pending_version = None

    def close(self):
        self.worker.close()

    def _stale(self, resource):
        return resource.unavailable or (self.accepted_at is not None
            and monotonic() >= self.accepted_at + (self.interval or 300))

    def status_line(self):
        w = self.window
        admitted = self.admitted and self._source() == self.binding
        state = self.state if admitted else DashboardState()
        stamp = w.t('dashboard_never')
        if state.last_success is not None:
            stamp = self.timestamp(state.last_success)
        if not admitted:
            status = w.t('dashboard_unavailable')
        elif self.token is not None:
            status = w.t('dashboard_refreshing')
        elif state.unsupported_version is not None:
            status = w.t('live_unsupported')
        elif state.partial:
            status = w.t('dashboard_partial')
        elif state.error:
            status = w.t('dashboard_failed')
        elif any(self._stale(v) for v in state.values.values()):
            status = w.t('dashboard_stale')
        else:
            status = w.t('dashboard_current') if state.values else w.t('dashboard_unavailable')
        return w.t('dashboard_status', timestamp=stamp, status=status)

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
            result['status'] = (w.t('dashboard_retained') if resource.unavailable else
                                w.t('dashboard_stale') if self._stale(resource) else '') if resource else w.t('dashboard_unavailable')
            return result
        server = value('server', ('version', 'os', 'arch', 'running'))
        # Rejected status diagnostics must not caption old A values as build B.
        server['version'] = state.unsupported_version or state.accepted_version or unknown
        server['running'] = self._running_label(server['running'])
        session = w._live_session
        summaries = self._summaries()
        def loaded(name):
            item = summaries.get(name)
            if item is None:
                return unknown
            stale, failed = session.summary_health(name)
            return w.t('dashboard_listing_count', count=item.loaded,
                       status=w.t('dashboard_listing_failed' if failed else
                                  'dashboard_listing_stale' if stale else 'dashboard_listing_cache'))
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

    def _summaries(self):
        w = self.window
        source = self._source()
        admitted = source is not None and source == self.binding and self.admitted
        state = self.state
        session = w._live_session
        summary_scope = self._summary_scope
        summaries = (session.summaries if admitted and session and not session._version_blocked
                     and state.unsupported_version is None and summary_scope is not None
                     and summary_scope[0] is session and summary_scope[1] == source[0] else {})
        version = state.accepted_version or (session.actual_version if session else None)
        return {k: v for k, v in summaries.items()
                if k in ('channels', 'events', 'recordings')
                and v.version == version and v.namespace == source[0].namespace}

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
            if memo.best_effort(control, 'setText', body):
                self.ready_cards.add(ident)
            else:
                self.ready_cards.discard(ident)
                memo.best_effort(control, 'setVisible', False)
                memo.best_effort(control, 'setText', '')
        w._label(112, w.t('connection_prompt') if not self.admitted or self._source() != self.binding else '')
        w._label(115, w.t('dashboard_hint'))
        w._label(116, self.status_line())
