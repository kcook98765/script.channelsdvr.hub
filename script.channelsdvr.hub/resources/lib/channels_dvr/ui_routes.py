"""Closed Hub routes and owner-only transitions; never a transport or settings API."""
from dataclasses import dataclass, field
from .models import Job
from .search_projection import SearchCategory, SearchId

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
        if self.view not in PRIMARY or (self.parent is not None and not (
                self.view == 'Search' and self.parent in ('Guide', 'Recordings', 'DVR')
                or self.parent == 'Search' and self.view in ('Guide', 'DVR'))):
            raise ValueError('Unknown Hub route')

    @property
    def back(self) -> str | None:
        return self.parent or ('Dashboard' if self.view != 'Dashboard' else None)


@dataclass(frozen=True)
class ScheduledReveal:
    job: Job
    observation: tuple = field(repr=False)


@dataclass(frozen=True)
class SearchReturn:
    identity: SearchId
    category: SearchCategory
    query: str
    position: int  # Also determines the page when the result order is unchanged.
    focus: int
    scheduled: ScheduledReveal | None = None


def route_fence(w, *, guide=True):
    # A queued Back/Select is not navigation in the next logical frame, even
    # when a child and its parent share the same physical WindowXML controls.
    w._dispatcher.fence_input()
    w._chrome.layout = object()
    w._playback_ui.cancel()
    if guide:
        w._guide_runtime.invalidate_view()


def presentation_scope(w):
    """Owner scalar for presentation only, never backend/journal authority."""
    return getattr(getattr(w, '_chrome', None), 'layout', None)


def enter(w, target, *, guide_return=None, dvr_return=None, search_return=None,
          search_context=None, returning=False, automatic=False):
    w._dispatcher.assert_owner()
    if w._closed:
        return
    hub = getattr(w, '_hub_session', None)
    if not automatic and hub is not None:
        hub.cancel_startup()
    old = w._route
    origin = getattr(w, '_search_origin', None)
    from_result = returning and old.parent == target == 'Search'
    from_context = returning and old.view == 'Search' and old.parent == target
    parent = ('Search' if search_return is not None else old.view if search_context is not None
              else origin[0] if from_result and origin is not None else None)
    route = Route(target, parent)
    if old == route and target == 'Dashboard':
        return
    with w._dispatcher.modal_input():  # no ingress during native control rebinding
        if old.parent == 'Search' and w._guide_return is not None:
            w.guide.restore_bookmark(w._guide_return)
            w._intended_anchor = None
        if old.parent == 'Search' and w._dvr_return is not None:
            w._dvr.restore_bookmark(w._dvr_return)
        preferred = None
        if from_result and w._search_return is not None:
            bookmark = w._search_return
            w._search_ui.category = bookmark.category
            w.search_query = bookmark.query
            w.search_selection = bookmark.identity
            w.positions['Search'] = bookmark.position
            w._search_ui.invalidate()
            preferred = bookmark.focus
        if search_context is not None:
            category, focus = search_context
            w._search_origin = old.view, focus
            w._search_ui.set_category(category)
        elif search_return is None and not from_result:
            if from_context and origin is not None:
                preferred = origin[1]
            w._search_origin = None
        w._guide_return = guide_return
        w._dvr_return = dvr_return
        w._search_return = search_return
        if old.view == 'Dashboard' and route.view != 'Dashboard':
            w._dashboard_ui.suspend()
        w._route = route
        route_fence(w)
        if from_context and target == 'DVR':
            w._dvr.resume_search_origin()
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
