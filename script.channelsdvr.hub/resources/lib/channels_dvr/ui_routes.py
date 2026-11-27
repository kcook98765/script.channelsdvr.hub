"""Closed Hub routes and owner-only transitions; never a transport or settings API."""
from dataclasses import dataclass
from .models import ChannelKey

PRIMARY = ('Dashboard', 'Guide', 'Recordings', 'DVR', 'Search', 'Settings')
VIEW_KEYS = dict(zip(PRIMARY, ('dashboard', 'guide', 'recordings', 'dvr_view', 'search', 'settings')))


def launch_view(value: str) -> str:
    if value == 'Scheduled':
        return 'DVR'
    return value if value in PRIMARY else 'Dashboard'


@dataclass(frozen=True)
class Route:
    view: str
    parent: str | None = None

    def __post_init__(self):
        if self.view not in PRIMARY or (self.parent is not None and
                                       (self.view, self.parent) != ('Guide', 'Search')):
            raise ValueError('Unknown Hub route')

    @property
    def back(self) -> str | None:
        return self.parent or ('Dashboard' if self.view != 'Dashboard' else None)


@dataclass(frozen=True)
class SearchReturn:
    airing: tuple[ChannelKey, str, int]
    position: int  # Also determines the page when the result order is unchanged.
    focus: int


def route_fence(w):
    # A queued Back/Select is not navigation in the next logical frame, even
    # when a child and its parent share the same physical WindowXML controls.
    w._dispatcher.fence_input()
    w._chrome.layout = object()
    w._playback_ui.cancel()
    w._guide_runtime.invalidate_view()


def presentation_scope(w):
    """Owner scalar for presentation only, never backend/journal authority."""
    return getattr(getattr(w, '_chrome', None), 'layout', None)


def enter(w, target, *, guide_return=None, search_return=None, returning=False, automatic=False):
    w._dispatcher.assert_owner()
    if w._closed:
        return
    hub = getattr(w, '_hub_session', None)
    if not automatic and hub is not None:
        hub.cancel_startup()
    old = w._route
    route = Route(target, 'Search' if guide_return is not None else None)
    if old == route and target == 'Dashboard':
        return
    with w._dispatcher.modal_input():  # no ingress during native control rebinding
        if old.parent == 'Search' and w._guide_return is not None:
            w.guide.restore_bookmark(w._guide_return)
            w._intended_anchor = None
        preferred = None
        if returning and old.parent == target == 'Search' and w._search_return is not None:
            bookmark = w._search_return
            w.search_selection = bookmark.airing
            w.positions['Search'] = bookmark.position
            w._search_page_key = None
            preferred = bookmark.focus
        w._guide_return = guide_return
        w._search_return = search_return
        if old.view == 'Dashboard' and route.view != 'Dashboard':
            w._dashboard_ui.suspend()
        w._route = route
        route_fence(w)
        w.render()
        w._chrome.initialize_focus(preferred)


def back(w):
    w._dispatcher.assert_owner()
    if w._closed:
        return
    if w.view == 'Recordings':
        with w._dispatcher.modal_input():
            if w.library.back() or w.library.leave_search():
                route_fence(w)
                w.render()
                w._chrome.initialize_focus()
                return
    if w._route.back is not None:
        enter(w, w._route.back, returning=True)
    else:
        w.close()
