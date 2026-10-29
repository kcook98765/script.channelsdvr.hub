"""Kodi Omega native player adapter. All SDK work is owner-only.

Callbacks only enqueue bounded plain events. They do not prove ownership of later
Kodi playback; only a matching current URL can confirm AVStarted. Never stop media.
"""
from collections import deque
from threading import Lock

import xbmc
import xbmcgui


class NativePlayer:
    def __init__(self, assert_owner):
        assert_owner()
        self._owner = assert_owner
        self._events: deque[tuple[str, str]] = deque(maxlen=32)
        self._lock = Lock()
        self._closed = False
        self._descriptor = None
        self._player = None

    def handoff(self, descriptor, *, admit=lambda: True):
        self._owner()
        if self._closed:
            return False
        self._descriptor = descriptor
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
        self._player = player
        if admit() is not True:  # atomic plain-state admission AFTER SDK item preparation
            self._descriptor = None
            self._player = None
            return False
        # Script context: exactly one API, no plugin handle/resolver/builtin.
        player.play(descriptor.url, item)
        return True  # handed off, NOT AV/audio/seek/stop acceptance

    def _enqueue(self, token, kind):
        with self._lock:
            if not self._closed:
                self._events.append((token, kind))

    def poll(self):
        self._owner()
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
        descriptor = self._descriptor
        accepted = []
        for token, kind in events:
            if descriptor is None or token != descriptor.attempt:
                continue
            if kind == 'av_started':
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
