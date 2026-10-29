"""Public-field presentation only; no station inference or event admission.

Groups do not establish backend mapping state, guide-row presence or the raw
reason a station value is unusable. Accepted events always take precedence.
"""

from .ui_text import FALLBACK, text

_GAPS = ('Guide station unqualified', 'Guide station missing')


def station_label(channel, default='', strings=FALLBACK):
    return text(strings, 'station_unavailable') if channel is not None and any(g in channel.groups for g in _GAPS) else default


def selected_gap(guide, strings=FALLBACK):
    return station_label(next((c for c in guide.channels if c.key == guide.selection.channel), None), strings=strings)


def station_footer(channels, strings=FALLBACK):
    unavailable = sum(_GAPS[0] in c.groups for c in channels)
    blank = sum(_GAPS[1] in c.groups for c in channels)
    return (text(strings, 'gap_footer', count=unavailable + blank, unavailable=unavailable, blank=blank)
            if unavailable or blank else '')


def show_station_gap(window, channel, dialog):
    """Handled even on display failure; never fall through to programme actions.

    Reuse the owned manual scroller/neutral Close footer. window.path is the
    entry-point add-on path, already used for the guide's packaged resources.
    No settings/profile lookup, native built-in IDs or automatic fallback viewer.
    """
    window._dispatcher.assert_owner()
    strings = getattr(window, 'strings', FALLBACK)
    if not station_label(channel):
        return False
    try:
        from .programme_scope_ui import show_scope
        if show_scope(window.path, text(strings, 'station_unavailable'), text(strings, 'gap_detail'), strings=strings) is not True:
            raise RuntimeError('guide explanation unavailable')
    except Exception:
        try:
            dialog.ok(text(strings, 'gap_failed_heading'), text(strings, 'gap_failed'))
        except Exception:
            pass
    return True
