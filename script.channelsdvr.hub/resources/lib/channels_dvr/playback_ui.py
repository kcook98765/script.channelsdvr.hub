"""Owner-only explicit Play orchestration; no profile, file or network I/O."""
from time import monotonic
from dataclasses import dataclass, field, replace
from uuid import uuid4

from .models import ChannelKey, Event
from .normalization import DataState
from .pass_browser import display_text
from .playback_descriptors import PlaybackSelection, LibraryRecording, LiveProviderScope, PlaybackError
from .playback_worker import PlaybackWorker
from .ui_text import text


_LIVE_INTENT_SECONDS = 300


@dataclass(frozen=True, eq=False)
class _GuideOk:
    """Exact visible airing and publication; never holds a retired index product."""
    event: Event
    bookmark: tuple
    updated_at: int
    ticket: object
    expires_at: float


@dataclass(frozen=True, eq=False)
class _LiveConsent:
    """One-use owner intent binding. Deliberately contains NO media descriptor/URL."""
    selection: PlaybackSelection
    attempt: str
    context: tuple = field(repr=False)
    binding: tuple = field(repr=False)
    expires_at: float
    source_pinned: bool
    display_label: str = field(repr=False)
    provider_scope: LiveProviderScope = field(repr=False)


class PlaybackUI:
    def __init__(self, window, *, worker_factory=PlaybackWorker, player_factory=None, clock=monotonic):
        self.window = window
        self._factory = worker_factory
        self._player_factory = player_factory
        self._clock = clock
        self._binding = None
        self._epoch = 0
        self._pending: tuple[PlaybackSelection, str, tuple] | None = None
        self._live_consent: _LiveConsent | None = None
        self._reviewing = False
        self.worker = None
        self.player = None
        self.state = 'idle'

    def _config(self):
        w = self.window
        try:
            if (w._closed or w.transient_demo_mode or w._live_session is None
                    or w._development_blocked is not None or w._live_session.loading
                    or w._guide_runtime._mode is not None
                    or w.get_setting('development_metadata_enabled') is not True):
                return None
            config = w._saved_config()
        except Exception:
            return None
        return config if config == w._development_config else None

    def _source(self):
        config = self._config()
        binding = None if config is None else (config, self.window._live_session,
            self.window._live_session._catalog_epoch)
        if binding != self._binding:
            self.cancel()
            if self.worker is not None:
                self.worker.close()
                self.worker = None
            self._binding = binding
            self._epoch += 1
        return config

    def selection(self, *, rebind=True):
        w = self.window
        w._dispatcher.assert_owner()
        config = self._source() if rebind else self._config()
        if config is None:
            return None
        data = w._current_data()
        if (not data.can_browse or data.loading or data.updated_at is None
                or data.state not in (DataState.READY, DataState.EMPTY)):
            return None
        # Accepted complete recording/channel selections are read hints only.
        # Age alone is not input or media authority. Failed/partial/loading and
        # unaccepted snapshots remain excluded; every descriptor needs fresh reads.
        if w.view in ('Guide', 'Search'):
            if not w._guide_runtime.ready:
                return None
            event = w._selected_search_event() if w.view == 'Search' else None
            key = event.channel if event is not None else w.guide.selection.channel if w.view == 'Guide' else None
            if type(key) is not ChannelKey or key.namespace != config.namespace:
                return None
            channel = w.guide.channel(key)
            if channel is None:
                return None
            # Events (past/future included) never become a catch-up/startover ID.
            return PlaybackSelection('live', key, self._epoch,
                display_text(channel.number + ' ' + channel.name, 512, w.strings) or w.t('untitled'))
        if w.view == 'Recordings':
            if data.state != DataState.READY:
                return None
            record = w.library.selected_recording()
            if record is None or record.namespace != config.namespace:
                return None
            # Current accepted internal-file membership is a read hint, not URL
            # authority, even with unknown origin or aged display metadata. The
            # worker must freshly qualify this exact ID in fixed recordings.
            if record not in data.items:
                return None
            try:
                identity = LibraryRecording.selected(record)
            except PlaybackError:
                return None
            return PlaybackSelection('recording', identity, self._epoch, display_text(record.title, 512, w.strings) or w.t('untitled'))
        return None

    def menu_label(self):
        # A view label is presentation, not selection/connection admission.
        self.window._dispatcher.assert_owner()
        return 'play_live' if self.window.view in ('Guide', 'Search') else 'play_recording'

    def _preference(self, key):
        try:
            return self.window.get_setting(key) is True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def _guide_ok_enabled(self):
        w = self.window
        return (w.view == 'Guide' and w._route.parent is None and w._live_session is not None
                and not w.transient_demo_mode and self._preference('guide_ok_watch_live'))

    def _guide_ok_proof(self, data, expires_at: float) -> _GuideOk | None:
        w = self.window
        now = w.utc_now()
        product = w.guide.current_product
        if (not data.can_browse or data.loading or data.stale or data.state != DataState.READY
                or type(data.updated_at) is not int or not 0 <= data.updated_at <= now
                or product is None or product.events is not data.items
                or product.source.fetched_at != data.updated_at):
            return None
        event = w.selected_indexed_event()
        if (event is None or event.start != w.guide.selection.event_start or event.end is None
                or not event.start <= now < event.end or not w._chrome.guide_airing_visible(event)):
            return None
        return _GuideOk(event, w.guide.bookmark(), data.updated_at, product.source.ticket, expires_at)

    def request_guide_ok(self) -> bool:
        """Only the already-dispatched, focused Guide CONTENT activation calls this."""
        w = self.window
        w._dispatcher.assert_owner()
        if not self._guide_ok_enabled() or not w._owns_playback_foreground():
            return False
        selection = self.selection()
        if selection is None or selection.kind != 'live':
            return False
        data = w._current_data()
        proof = self._guide_ok_proof(data, self._clock() + _LIVE_INTENT_SECONDS)
        if proof is None or proof.event.channel != selection.identity:
            return False
        self.cancel()
        self._detach_player()
        self._submit(selection, data.items, proof)
        return True

    def request(self, gui):
        self.window._dispatcher.assert_owner()
        if not self.window._owns_playback_foreground():
            return
        self.cancel()  # a new explicit command always revokes earlier one-use consent
        self._detach_player()  # new attempt cannot sample or publish the old one
        selection = self.selection()
        if selection is None:
            self.state = 'selection_unavailable'
            gui.Dialog().ok(self.window.t('playback'), self.window.t('play_read_hint_required'))
            return
        self._submit(selection, self.window._current_data().items)

    def _submit(self, selection, items, guide_ok: _GuideOk | None = None):
        if self.worker is None:
            assert self._binding is not None
            self.worker = self._factory(self._binding[0])
        token = str(uuid4())
        # Include current view + exact snapshot lease; refresh/view changes revoke.
        context = (self.window.view, items, self.window._dispatcher.capture_playback_input(), guide_ok)
        if self.worker.submit(selection, token):
            self._pending = (selection, token, context)
            self.state = 'preparing'
        else:
            self.window._dispatcher.cancel_playback_input(context[2])
            self.state = 'prepare_refused'

    def cancel(self, *, keep_input=False):
        if self._pending is not None and not keep_input:
            self.window._dispatcher.cancel_playback_input(self._pending[2][2])
        if self.worker is not None:
            self.worker.cancel()
        self._pending = None
        self._live_consent = None

    def _current(self, pending):
        if self._pending is not pending or pending is None:
            return False
        selection, _, context = pending
        current = self._context_current(selection, context)
        return self._pending is pending and current

    def _context_current(self, selection, context):
        w = self.window
        current = self.selection()
        if (current != selection or not w._dispatcher.playback_input_current(context[2])
                or w.view != context[0]):
            return False
        data = w._current_data()
        if data.items is not context[1]:
            return False
        proof = context[3]
        if proof is None:
            return w._owns_playback_foreground()
        if self._clock() >= proof.expires_at or not self._guide_ok_enabled():
            return False
        fresh = self._guide_ok_proof(data, proof.expires_at)
        return (fresh is not None and fresh.event is proof.event and fresh.bookmark == proof.bookmark
                and fresh.updated_at == proof.updated_at and fresh.ticket is proof.ticket
                and w._owns_playback_foreground())

    def _fresh(self, descriptor):
        return (type(descriptor.prepared_at) in (int, float)
                and 0 <= self._clock() - descriptor.prepared_at <= 30)

    def _consent_matches(self, consent, pending):
        return (type(consent) is _LiveConsent and pending is not None
                and self._live_consent is consent and consent.binding is self._binding
                and consent.selection == pending[0] and consent.attempt == pending[1]
                and consent.context is pending[2] and self._current(pending))

    def _review_live(self, gui, pending: tuple[PlaybackSelection, str, tuple], descriptor,
                     display_label: str, provider_scope: LiveProviderScope):
        """First six-GET result is review intent only, never a playable authority."""
        assert pending is not None
        w = self.window
        scope = w.t('play_source_pin' if descriptor.source_pinned else 'play_source_any')
        self._reviewing = True
        try:
            dialog = gui.Dialog()
            heading = w.t('play_live')
            body = (w.t('play_live_confirm', source=scope) + '\n\n'
                    + w.t('live_recheck_notice', channel=display_label))
            default_no = gui.DLG_YESNO_NO_BTN
            if not self._current(pending):
                if self._pending is pending:
                    self.cancel()
                    self.state = 'selection_changed'
                return
            if not self._fresh(descriptor):
                self.cancel()
                self.state = 'expired'
                return
            confirmed = dialog.yesno(heading, body, defaultbutton=default_no)
        except Exception:
            if self._pending is pending:
                self.cancel()
                self.state = 'confirmation_failed'
            return
        finally:
            self._reviewing = False
        # Do not cancel a newer explicitly requested intent from a reentrant modal.
        if self._pending is not pending:
            return
        if confirmed is not True or not self._current(pending):
            self.cancel()
            self.state = 'cancelled'
            return
        selection, _, context = pending
        assert self.worker is not None and self._binding is not None
        attempt = str(uuid4())
        consent = _LiveConsent(selection, attempt, context, self._binding,
                                self._clock() + _LIVE_INTENT_SECONDS, descriptor.source_pinned,
                                display_label, provider_scope)
        # Same serial worker, NEW client/attempt, original input lease. No GUI post
        # and no old URL/descriptor is carried into the fresh post-consent read.
        if self.worker.submit(selection, attempt):
            self._live_consent = consent
            self._pending = (selection, attempt, context)
            self.state = 'rechecking_live'
        else:
            self.cancel()
            self.state = 'recheck_failed'

    def poll(self, gui):
        w = self.window
        w._dispatcher.assert_owner()
        if w._closed or self._reviewing:
            return
        self._source()
        pending = self._pending
        consent = self._live_consent
        if consent is not None:
            if type(consent) is not _LiveConsent:
                self.cancel()
                self.state = 'consent_invalid'
                return
            if self._clock() >= consent.expires_at:
                self.cancel()
                self.state = 'consent_expired'
                gui.Dialog().ok(w.t('playback'), w.t('live_consent_expired'))
                return
            if not self._consent_matches(consent, pending):
                self.cancel()
                self.state = 'selection_changed'
                return
        if pending is not None and not self._current(pending):
            self.cancel()
            self.state = 'selection_changed'
        result = self.worker.poll() if self.worker is not None else None
        if result is not None and pending is not None and self._current(pending):
            selection, token, _ = pending
            if result.attempt != token or result.selection != selection:
                return
            descriptor = result.descriptor
            if (result.error is not None or descriptor is None):
                self.cancel()
                self.state = result.error or 'metadata_rejected'
                gui.Dialog().ok(w.t('playback'), w.t('play_failed'))
                return
            if descriptor.selection != selection or descriptor.attempt != token:
                self.cancel()
                self.state = 'metadata_rejected'
                return
            if type(descriptor.source_pinned) is not bool:
                self.cancel()
                self.state = 'metadata_rejected'
                return
            if not self._fresh(descriptor):
                self.cancel()
                self.state = 'expired'
                return
            if selection.kind == 'live':
                key = selection.identity
                label, provider = descriptor.resolved_label, descriptor.live_scope
                if (type(key) is not ChannelKey or type(label) is not str or not label.strip()
                        or type(provider) is not LiveProviderScope
                        or not provider.matches(key, descriptor.source_pinned)):
                    self.cancel()
                    self.state = 'metadata_rejected'
                    return
                if consent is None:
                    self._review_live(gui, pending, descriptor, label, provider)
                    return
                if (not self._consent_matches(consent, pending) or label != consent.display_label
                        or provider != consent.provider_scope or descriptor.source_pinned != consent.source_pinned):
                    self.cancel()
                    self.state = 'consent_scope_changed'
                    return
            if not self._current(pending) or not self._fresh(descriptor):
                self.cancel()
                self.state = 'cancelled'
                return
            self.cancel(keep_input=True)
            lease = pending[2][2]
            try:
                factory = self._player_factory
                if factory is None:
                    from .playback_native import NativePlayer
                    factory = NativePlayer
                # A selected past/future programme image is NOT live station art.
                artwork = getattr(w, '_artwork_ui', None) if selection.kind == 'recording' else None
                reference = artwork.ready() if artwork is not None else None
                if reference:
                    descriptor = replace(descriptor, art=(('thumb', reference),))
                self.player = factory(w._dispatcher.assert_owner)
                def admit():
                    return (self._context_current(selection, pending[2])
                        and self._fresh(descriptor)
                        and (selection.kind != 'live' or (type(consent) is _LiveConsent
                             and consent.binding is self._binding and self._clock() < consent.expires_at))
                        and w._dispatcher.commit_playback(lease))
                outcome = self.player.handoff(descriptor, admit=admit,
                                              windowed=self._preference('playback_start_windowed'))
                self.state = ('handoff' if outcome is True else
                              'handoff_unknown' if outcome is None else 'handoff_refused')
            except Exception:
                self.state = 'handoff_failed'
                if not w._closed:
                    gui.Dialog().ok(w.t('playback'), w.t('play_failed'))
            finally:
                w._dispatcher.cancel_playback_input(lease)
        if self.player is not None:
            for state in self.player.poll():
                if self._pending is None and self.state != 'handoff_unknown':
                    self.state = state  # callbacks cannot resolve an ambiguous send

    def diagnostic_text(self):
        self.window._dispatcher.assert_owner()
        # An explicit diagnostic checkpoint, not a poll loop or a control gate.
        sample = (self.player.recording_readback() if self.player is not None and self._pending is None
                  else None)
        state = self.state if sample is None else f'{self.state}; {sample.value}'
        return text(self.window.strings, 'play_diagnostic', state=state)

    def _detach_player(self):
        player, self.player = self.player, None
        if player is not None:
            player.close()  # detach callbacks only; NEVER stop Kodi

    def close(self):
        self.window._dispatcher.assert_owner()
        self.cancel()
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        self._detach_player()
