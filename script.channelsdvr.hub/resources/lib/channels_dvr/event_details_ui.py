"""Inert exact programme text and small owner adapters; no SDK import or I/O.

Only the explicit display lazily loads the existing owned manual scroller.
No built-in viewer fallback, command, token, worker or profile authority here.
"""
from .models import Channel, Event
from .pass_browser import display_text, utc
from .programme_pass_ui import SourceScope
from .ui_text import FALLBACK, text
from .artwork import event_key


def event_start(event: Event) -> str:
    # Public Event DTO seconds are nonnegative integers. Bound datetime's range
    # explicitly (including on Windows); never guess units or format bad values.
    if type(event.start) is not int or not 0 <= event.start <= 253402300799:
        raise ValueError('invalid_event_time')
    return utc(event.start)


def event_text(event: Event, channel: Channel | None, source: str, strings=FALLBACK) -> str:
    start = event_start(event)
    if event.end is None:
        end, duration = text(strings, 'unknown_end'), text(strings, 'unknown')
    else:
        if (type(event.end) is not int or not event.start < event.end <= 253402300799):
            raise ValueError('invalid_event_time')
        end, duration = utc(event.end), text(strings, 'seconds', count=event.end - event.start)
    joined = channel is not None and channel.key == event.channel
    name = (' '.join(filter(None, (display_text(channel.number, 128, strings), display_text(channel.name, 512, strings))))
            if channel is not None and joined else text(strings, 'unknown'))
    return text(strings, 'event_fields', title=display_text(event.title, 160, strings) or text(strings, 'untitled'),
        start=start, end=end, duration=duration, channel=name or text(strings, 'unknown'),
        source=(display_text(source, 128, strings) or text(strings, 'unknown')) if joined else text(strings, 'unknown'),
        full_title=display_text(event.title, 512, strings) or text(strings, 'untitled'),
        description=display_text(event.description, 4096, strings) or text(strings, 'no_description'),
        notice=text(strings, 'details_notice'))


def show_scope(addon_path, heading, text, strings=FALLBACK, *, art_path=None):
    from .programme_scope_ui import show_scope as owned_scope
    return owned_scope(addon_path, heading, text, strings=strings, art_path=art_path)


def selected_text(window, event: Event) -> str:
    channel = window.guide.channel(event.channel)
    # Resolve only from the selected full key in the current indexed catalog.
    # Synthetic IDs and private source/channel selectors are never display labels.
    catalog = window.guide.repository.channels
    source = window.guide.projection.update(catalog).source_label(event.channel)
    return event_text(event, channel, source, getattr(window, 'strings', FALLBACK))


def show_event_details(window, event: Event, dialog) -> None:
    window._dispatcher.assert_owner()
    if window.selected_indexed_event(event) is None:
        return
    strings = getattr(window, 'strings', FALLBACK)
    body = None
    try:
        body = selected_text(window, event)
        art_ui = getattr(window, '_artwork_ui', None)
        art_path = art_ui.ready(event_key(event)) if art_ui is not None else None
        options = {'art_path': art_path} if art_path else {}
        if show_scope(window.path, text(strings, 'event_details'), body, strings, **options) is not True:
            raise RuntimeError('details_unavailable')
    except Exception:
        if body is not None:
            try:
                dialog.textviewer(text(strings, 'event_details'), body)
                return
            except Exception:
                pass
        dialog.ok(text(strings, 'details_unavailable'), text(strings, 'details_failure'))


def recording_scope(window, event: Event) -> bool:
    strings = getattr(window, 'strings', FALLBACK)
    body = selected_text(window, event) + '\n\n' + text(strings, 'recording_scope_title') + '\n' + text(strings, 'recording_scope')
    body += '\n' + text(strings, 'scope_confirmation')
    return show_scope(window.path, text(strings, 'recording_scope_title'), body, strings)


def frame_context(window):
    """Frozen references + source epoch: equal Events on a new dataset differ."""
    session, repository = window._live_session, window.repository
    scope = SourceScope(session, getattr(session, '_generation', None), repository,
                        repository.events, repository.channels, getattr(session, 'loading', None))
    return scope, window.guide, window._guide_runtime.revision, window.view, window.guide.group
