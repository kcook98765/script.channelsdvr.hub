# Full-screen Guide (#14): source handoff

Phase 4 extends the exclusive Guide in the existing Hub `WindowXML`; Back
returns to Dashboard or the invoking Search result. The primary Guide keeps its
in-session channel, airing, UTC anchor and viewport. The Search drill-in can
browse its accepted horizon but cannot replace it behind the parent. Nothing is
persisted. The Guide rail has Collection, Date and time, Now, Details, More and
Back. From the grid press Context → Go to Guide controls, or Down at the last
channel; Up from the rail returns to the grid. Page Up/Down move a channel page.
Date and time chooses a local day, then one of the UTC slots in the currently
loaded horizon. E.g. repeated 01:30 slots have different displayed offsets.
The existing explicit previous/next-hour and primary Guide Now navigation retain
their existing live read path when needed; Date and time does not request data.

OK, Info and an airing click open read-only details; clicking selects by full
airing identity before opening details. Explicit Play, Schedule and Series
commands are in More when eligible; their existing guards and workers are
unchanged. There is no live-select preference or automatic playback here.
Subpixel entries have no artificial mouse hit; keyboard travel can still choose
them and show their details. Overflow summaries are not individual hits. A
retained grid is marked as previous listings and does not publish airing hits
or selected programme artwork. Station names and source labels remain visible
with or without a logo. Kodi's image setter does not report decoding/delivery;
assignment alone is not evidence of visible art or automatic decode fallback.

## Independent checks (not run in the source pass)

From the repo root, use the existing offline allowlist and focused cohorts:

```sh
python3 -B tools/test_offline.py --quiet
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_guide test_navigation test_artwork_native test_package test_ui_dispatch test_live_loader test_dashboard test_search test_native_playback -v
```

The focused invocation is outside the runner's process/network audit guard.
The new pure geometry/day, artwork owner and package assertions are in already
allowlisted modules. Unit and fake-window checks do not establish native click
delivery, scaling, font legibility, focus, or decoding. For static validation,
use the documented isolated Omega interpreter, record its `stub-lanes` output,
and use that same interpreter for Toolkit validation with `--branch omega`.
Retain the adapter and catalog size guards; no other lane substitutes for Omega.

## Planned managed Kodi 21.3 (Omega) demo at 5 FPS

Use only `kodi-managed-lab` and reviewed local synthetic fixtures; don't point a
profile at a production DVR or play/schedule anything. Confirm the exact candidate
is running before capture. At 5 FPS, pause on the settled frames and keep captions
short; this rate documents behavior, not navigation or rendering performance.

| Scenario | Show / verify |
| --- | --- |
| Disconnected, then normal Demo Guide | Open Guide from Dashboard with no listings; show hidden destination rail, visible status, and Back. Open demo through existing Developer tools. On initial async load, explicit Up from More enters the grid if it kept focus. Show station text fallback, axes, width differences, gap/overlap, current-time marker, and selected title/time/synopsis (or no-description text). |
| Normal controls and modal return | Arrow horizontally/vertically and Page Down/Up; select with OK, Info and an exact mouse click in a different row or overlap lane. Close Details and cancel Collection and Date dialogs. Use Context → Go to Guide controls; select Favorites, Empty (no channels), another day/time, and Now. The marker is absent when outside the displayed range. Back to Dashboard and re-enter at the retained bookmark. Drill in from a Search result and Back to that result; the primary Guide anchor remains independent. |
| Time and status | At a fold-observing local timezone, show distinct offsets for the normal fixture's repeated hour if that timezone is already available. Otherwise use the offline fold/spring assertions. Use the existing Demo data status controls for partial/stale and recovery; verify warnings and no old clickable targets while a replacement index is pending. Do not present intentionally empty data as a provider outage. |
| Stress and 500-channel / 48-hour fixture | Use existing Developer tools to run the stress fixture (short cells/overlap) and dense 500-channel exercise. Show limit/overflow labeling when triggered, selection through short cells by keyboard, multiple channel pages, and a pointer hit after repaint. No selection should launch playback. |
| Scaled viewport, if supported | Repeat group/day controls, context, a time-boundary click and Back at 640×360 or the supported managed scaled viewport. Check that text and marker remain readable and hits line up with native skin scaling. If unsupported, mark scaled native untested. |
| Artwork limits | Built-in demo DTOs exercise text fallback, not successful logo/synopsis artwork decoding. Use a separately admitted managed fixture only if one already provides safe references; otherwise leave present/late native artwork untested and report the fake assignment/revocation coverage. |

No files, textures, layouts, or code from the TV Guide Fullscreen reference were
used. The layout uses original Hub textures and Kodi controls, and images use
only the pre-existing source-qualified Channels artwork inventory/admission.
Keep private recordings/evidence separate; review suitability before any local
owner publication. Native acceptance and release qualification are pending.
