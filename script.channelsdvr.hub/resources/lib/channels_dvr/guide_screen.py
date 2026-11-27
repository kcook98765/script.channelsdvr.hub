"""Original Hub Guide presentation and bounded, read-only date/group choosers."""
from bisect import bisect_right
from datetime import date, tzinfo
from itertools import chain

from .guide import Cell, local_stamp, time_label
from .guide_gap import station_label
from .guide_render import GRID_LEFT, GRID_WIDTH, ROW_PITCH, ROW_TOP, draw
from .pass_browser import display_text
from .ui_hits import Hit
from .ui_menu import command, group_label
from .ui_modal import dialog_for
from .ui_routes import route_fence

TITLE, METADATA, DESCRIPTION, RANGE, NOW, PANEL = range(150, 156)
TICK_SECONDS = 1800


def timeline_x(instant: int, left: int, span: int) -> int:
    """Quantize shared UTC edges, not independent widths, in skin coordinates."""
    return GRID_LEFT + (instant - left) * GRID_WIDTH // span


def now_position(now: int, left: int, span: int) -> int | None:
    return timeline_x(now, left, span) if left <= now < left + span else None


def time_axis(left: int, span: int, zone: tzinfo | None = None) -> tuple[tuple[int, str], ...]:
    # Use the same viewport as the cells even when the left edge is not a
    # round half-hour. Local conversion is per instant, including DST folds.
    return tuple((timeline_x(tick, left, span), local_stamp(tick, zone).strftime('%H:%M %z'))
                 for tick in range(left, left + span, TICK_SECONDS))


def cell_rect(cell: Cell, left: int, span: int, split: bool, *,
              horizon_end: int | None = None) -> tuple[int, int, int, int]:
    right = left + span if horizon_end is None else min(left + span, horizon_end)
    x = timeline_x(max(left, cell.start), left, span)
    end = timeline_x(min(right, cell.end), left, span)
    width = max(0, end - x)
    y = ROW_TOP + cell.row * ROW_PITCH + cell.lane * 39
    return x, y, width - min(2, width // 8), 33 if split else 73


def _set(w, ident, method, *args):
    control = w._chrome.control(ident)
    if control is not None:
        w._render_memo.best_effort(control, method, *args)


def hide_guide(w):
    for ident in (TITLE, METADATA, DESCRIPTION, RANGE, NOW, PANEL, *range(126, 132)):
        _set(w, ident, 'setVisible', False)


def paint_clock(w, *, visible=None):
    """Only update the marker, not the grid/hits/focus, on the owner status tick."""
    if visible is None:
        visible = w._render_memo.values.get((w._chrome.control(RANGE), 'setVisible')) == (True,)
    x = None
    if visible:
        guide = w.guide
        now = w.utc_now() if w._live_session is not None else guide.repository.now
        if now < guide.repository.horizon[1]:
            x = now_position(now, guide.viewport, guide.span)
    if x is not None:
        _set(w, NOW, 'setPosition', x, 332)
        _set(w, NOW, 'setWidth', min(4, GRID_LEFT + GRID_WIDTH - x))
    _set(w, NOW, 'setVisible', x is not None)


def station_rows(w, data):
    return (w._live_session is not None and w.guide.prepared_ready and w._guide_runtime.ready
            and bool(w.guide.channels) and data.updated_at is not None)


def paint_guide(w, retained):
    guide = w.guide
    live = w._live_session is not None
    hittable = not retained and w._guide_runtime.ready
    group = display_text(group_label(w.strings, guide.group), 100, w.strings) or w.t('unknown')
    horizon_end = guide.repository.horizon[1]
    end = min(guide.viewport + guide.span, horizon_end)
    w._label(RANGE, w.t('guide_range', group=group,
                       start=time_label(guide.viewport), end=time_label(end)))
    for ident in (TITLE, METADATA, DESCRIPTION, RANGE, PANEL):
        _set(w, ident, 'setVisible', True)
    paint_clock(w, visible=True)
    for index, (x, caption) in enumerate(time_axis(guide.viewport, guide.span)):
        _set(w, 140 + index, 'setPosition', x, 307)
        w._label(140 + index, caption)
    channels = guide.channels[guide.top:guide.top + guide.rows]
    for index in range(guide.rows):
        _set(w, 126 + index, 'setVisible', index < len(channels))
        if index < len(channels):
            name, source = _station(w, channels[index])
            w._label(120 + index, name)
            w._label(126 + index, source)

    cells = guide.cells(overscan=0)
    split_rows = {cell.row for cell in cells if cell.lane == 1}
    limited = guide.truncated
    painted = []
    for cell in cells:
        x, y, width, height = cell_rect(cell, guide.viewport, guide.span, cell.row in split_rows,
                                        horizon_end=horizon_end)
        if width:
            painted.append((cell, (x, y, width, height)))
            if hittable and not cell.overflow:
                w._hit_plan.append(Hit((x, y, width, height), ('cell', cell.channel, cell.event_id, cell.start)))
        else:
            limited = True
            # Keyboard can still select a subpixel airing. Indicate it without
            # inventing a target that could intercept the neighbouring hit.
            if cell.selected:
                painted.append((cell, (x, y, 1, height)))
    # Keep the selected cursor on top, including a subpixel selection.
    painted.sort(key=lambda entry: entry[0].selected)
    w._ensure_pool(len(painted))
    for index, (cell, (x, y, width, height)) in enumerate(painted):
        title = display_text(cell.title, 160, w.strings) or w.t('untitled')
        if live and cell.event_id is None and not cell.overflow:
            title = station_label(guide.channel(cell.channel), title, w.strings)
        caption = ('‹ ' if cell.continues_left else '') + title + (' ›' if cell.continues_right else '')
        draw(w, index, x, y, width, height + 3, caption, cell.selected,
             cell.event_id is None, gutter=0)
    _context(w, retained)
    if limited:
        w._label(113, w.t('guide_dense_notice'))
    footer = w.t('demo_footer' if w.demo_allowed() else 'listings_footer')
    w._label(115, w.t('guide_keys', footer=footer))


def _station(w, channel):
    number = display_text(channel.number, 32, w.strings) or ''
    name = display_text(channel.name, 100, w.strings) or w.t('unknown')
    source = display_text(w._source_label(channel.key), 80, w.strings) or ''
    return f'{number} {name}'.strip(), source


def _context(w, retained):
    guide = w.guide
    channel = guide.channel(guide.selection.channel)
    title = body = metadata = ''
    if retained:
        title = w.t('guide_retained_status', status=w.t('guide_unavailable' if w._guide_runtime.error else 'preparing_guide'))
    elif channel is None:
        title = w.t('guide_no_channels')
    else:
        station = ' · '.join(filter(None, _station(w, channel)))
        event = guide.selected_event()
        if event is None:
            gap = station_label(channel, strings=w.strings)
            title = gap or w.t('no_listing_selected')
            metadata = f'{station}  |  {time_label(guide.selection.anchor)}'
            body = w.t('guide_station_detail' if gap else 'temporal_gap')
        else:
            title = display_text(event.title, 180, w.strings) or w.t('untitled')
            end = time_label(event.end) if event.end is not None else w.t('unknown_end')
            metadata = f'{station}  |  {time_label(event.start)} — {end}'
            body = display_text(event.description, 320, w.strings) or w.t('no_description')
    w._label(TITLE, title)
    w._label(METADATA, metadata)
    _set(w, DESCRIPTION, 'setText', body)


def day_choices(start: int, end: int, zone: tzinfo | None = None) -> tuple[tuple[date, str, tuple[int, ...]], ...]:
    """Group bounded UTC slots by local date without inventing civil times.

    Current Hub products cover at most 72 hours. The cap also bounds this
    owner-side modal if handed a larger horizon. Include a clipped first slot.
    """
    if end <= start:
        return ()
    grouped: dict[date, tuple[str, list[int]]] = {}
    for slot in chain((start,), range(start - start % TICK_SECONDS + TICK_SECONDS,
                                      min(end, start + 3 * 86400), TICK_SECONDS)):
        stamp = local_stamp(slot, zone)
        grouped.setdefault(stamp.date(), (stamp.strftime('%a %d %b %Y'), []))[1].append(slot)
    return tuple((day, label, tuple(slots)) for day, (label, slots) in grouped.items())


def _scope(w):
    return w._chrome.command_activation('selection.anchor', '')


def choose_group(w, gui):
    proof = _scope(w)
    groups = tuple(w.guide.repository.groups)
    if not groups:
        return
    current = groups.index(w.guide.group) if w.guide.group in groups else 0
    labels = [display_text(group_label(w.strings, key), 100, w.strings) or w.t('unknown') for key in groups]
    answer = dialog_for(w, gui).select(w.t('collection'), labels, preselect=current)
    chosen = command(tuple((group, '') for group in groups), answer)
    if (chosen is not None and not w._closed and w.view == 'Guide' and w._guide_runtime.ready
            and proof.matches(_scope(w)) and chosen != w.guide.group
            and chosen in w.guide.repository.groups):
        w.guide.set_group(chosen)
        route_fence(w)


def choose_time(w, gui):
    proof = _scope(w)
    horizon = w.guide.repository.horizon
    days = day_choices(*horizon)
    if not days:
        return

    def current():
        return (not w._closed and w.view == 'Guide' and w._guide_runtime.ready
                and w.guide.repository.horizon == horizon and proof.matches(_scope(w)))

    anchor = w.guide.selection.anchor
    day = local_stamp(anchor).date()
    index = next((i for i, entry in enumerate(days) if entry[0] == day), 0)
    i = command(tuple((n, '') for n in range(len(days))), dialog_for(w, gui).select(
        w.t('guide_day'), [caption for _, caption, _ in days], preselect=index))
    if i is None or not current():
        return
    slots = days[i][2]
    selected = command(tuple((slot, '') for slot in slots), dialog_for(w, gui).select(
        w.t('date_offset'), [time_label(slot) for slot in slots],
        preselect=max(0, bisect_right(slots, anchor) - 1) if i == index else 0))
    if selected is not None and current():
        w.guide.jump(selected)  # In the accepted horizon; no request or cache switch.
