"""Kodi Omega native player adapter. All SDK work is owner-only.

Callbacks only enqueue bounded plain events. Exact recording video readback is
an observation, not proof of a particular native session. Never control media.
"""
from collections import deque
from enum import Enum
from threading import Lock

import xbmc
import xbmcgui


class RecordingReadback(str, Enum):
    EXACT_VIDEO = 'recording_exact_video'
    IDLE = 'recording_player_idle'
    OTHER_ACTIVE = 'recording_other_active'
    UNKNOWN = 'recording_readback_unknown'


class NativePlayer:
    def __init__(self, assert_owner):
        assert_owner()
        self._owner = assert_owner
        self._events: deque[tuple[str, str]] = deque(maxlen=32)
        self._lock = Lock()
        self._closed = False
        self._dispatch_attempted = False
        self._descriptor = None
        self._player = None

    def handoff(self, descriptor, *, admit=lambda: True, windowed: bool = False) -> bool | None:
        """False if refused, True if sent, None if SDK send outcome is unknown."""
        self._owner()
        if self._closed or self._dispatch_attempted:
            return False
        adapter = self
        token = descriptor.attempt

        class Observer(xbmc.Player):
            def onAVStarted(self):
                adapter._enqueue(token, 'av_started')

            def onPlayBackStopped(self):
                adapter._enqueue(token, 'stopped')

            def onPlayBackEnded(self):
                adapter._enqueue(token, 'ended')

            def onPlayBackError(self):
                adapter._enqueue(token, 'error')

        live = descriptor.selection.kind == 'live'
        label = descriptor.resolved_label if live else descriptor.selection.title
        if type(label) is not str or not label:
            return False
        item = xbmcgui.ListItem(label=label, offscreen=True)
        item.setPath(descriptor.url)
        item.setMimeType(descriptor.mime)
        item.setContentLookup(False)
        item.setProperty('StartOffset', '0')
        if descriptor.art and not live:
            item.setArt(dict(descriptor.art))
        player = Observer()
        if admit() is not True:  # atomic plain-state admission AFTER SDK item preparation
            return False
        # Script context: exactly one API, no plugin handle/resolver/builtin.
        self._dispatch_attempted = True  # do not repeat an ambiguous send if the SDK raises
        self._descriptor, self._player = descriptor, player
        try:
            player.play(descriptor.url, item, windowed=windowed is True)
        except Exception:
            # Kodi may already have accepted the async message. Retain only the
            # same owner-scoped readback and never emit the SDK exception text.
            return None
        return True  # handed off, NOT AV/audio/seek/stop acceptance

    def _enqueue(self, token, kind):
        with self._lock:
            if not self._closed:
                self._events.append((token, kind))

    def recording_readback(self) -> RecordingReadback | None:
        """Owner checkpoint after an admitted recording send; no locator leaves here."""
        self._owner()
        descriptor, player = self._descriptor, self._player
        if descriptor is None or descriptor.selection.kind != 'recording' or player is None:
            return None
        try:
            playing = player.isPlaying()
            if playing is False:
                if (player.isPlayingVideo() is False and player.isPlayingAudio() is False
                        and player.isPlaying() is False):
                    return RecordingReadback.IDLE
                return RecordingReadback.UNKNOWN
            if playing is not True or player.isExternalPlayer() is not False:
                return RecordingReadback.UNKNOWN
            video = player.isPlayingVideo()
            if type(video) is not bool or (not video and player.isPlayingAudio() is not True):
                return RecordingReadback.UNKNOWN
            locator = player.getPlayingFile()
            if type(locator) is not str or not locator:
                return RecordingReadback.UNKNOWN
            confirmed_locator = player.getPlayingFile()
            if (type(confirmed_locator) is not str or confirmed_locator != locator
                    or player.isPlayingVideo() is not video
                    or (not video and player.isPlayingAudio() is not True)
                    or player.isExternalPlayer() is not False or player.isPlaying() is not True):
                return RecordingReadback.UNKNOWN
            # A stable nonmatch is only 'other' for this comparison. No aliases,
            # normalization, same-file takeover detection, or control authority.
            if video and locator == descriptor.url:
                return RecordingReadback.EXACT_VIDEO
            return RecordingReadback.OTHER_ACTIVE
        except Exception:
            return RecordingReadback.UNKNOWN  # do not expose SDK exception text

    def poll(self):
        self._owner()
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
        descriptor = self._descriptor
        accepted = []
        recording_sampled = False
        for token, kind in events:
            if descriptor is None or token != descriptor.attempt:
                continue
            if kind == 'av_started':
                if descriptor.selection.kind == 'recording':
                    if not recording_sampled:
                        recording_sampled = True
                        if self.recording_readback() is RecordingReadback.EXACT_VIDEO:
                            accepted.append('av_started_observed')
                else:
                    try:
                        if self._player is not None and self._player.getPlayingFile() == descriptor.url:
                            accepted.append(kind)
                    except Exception:
                        pass
            else:
                # Terminal callbacks are advisory: Kodi's global callbacks cannot
                # prove which URL stopped/errored. No popup or playback operation.
                accepted.append(kind + '_observed')
        return tuple(accepted)

    def close(self):
        self._owner()
        with self._lock:
            self._closed = True
            self._events.clear()
        self._descriptor = None
        self._player = None
