# Channels DVR Hub navigation modernization (#10)

This is the source-side design and restart point for the seven-phase Hub work.
It is not a release checklist signed by the owner, native evidence, access to a
DVR, or authorization to start another phase. The Phase 1 source baseline was
`1db6a9f69e1c8b93e7aa64faa814227ce45cac28`. Record the exact candidate and review
disposition outside this document when qualifying a build.

## Sequence and gates

**#10** is the umbrella and decision log. **#11–#17** are the seven phase
checkpoints. #11 lays the foundation for all later routes. #12–#14 can proceed
independently after #11. Review each phase against its dependencies and the
umbrella constraints; dependency order does not authorize combining phases. If the owner issue
record narrows a direction or changes its order/acceptance criteria, resolve that
before implementation. Only #11 is implemented here. **#6 and #9 stay separate**:
do not incorporate their changes, outcomes or qualification into these phases by
implication.

| Phase / dependency | Direction and scope | Restartable handover / acceptance focus |
| --- | --- | --- |
| 1 · **#11**, after #10 scope approval | Dashboard root and six destinations. Exclusive existing Hub Guide; adapt Scheduled and Pass list under DVR, Connection under Settings. Establish Back, focus, pointer, contextual commands and in-memory bookmarks without new features. | Source and focused fakes in this change; **independent static, fake and native review pending**. Start with the #11 matrix below and verify legacy program launch and workflows. |
| 2 · **#12**, depends on #11 | Recordings tabs: Recent, TV Shows, Movies, All, Search; hierarchy, source and sort over existing library models. | Handover tab/hierarchy, source/sort and removed-item fallback evidence; explicit Play stays separate. |
| 3 · **#13**, depends on #11 | Integrated Scheduled and Passes, keeping read-only presentation distinct from the existing explicitly admitted control and reconciliation paths. | Handover read scopes, scheduled/pass distinctions and empty/partial/outage behavior; no production mutation evidence under read-only approval. |
| 4 · **#14**, depends on #11 | Enhanced full-screen Guide: TV Guide as a behavioral reference only, bounded Hub data, logos, details, Now, group and day navigation. No GPL copying or assets without a separate decision. | Handover native geometry, navigation and the bounded data/rights review, including gaps, overlaps and fallback. |
| 5 · **#15**, depends on #11–#14 | Unified Search categories and honest coverage; preserve logical result return and distinguish incomplete coverage from no matches. | Handover per-category coverage, stable result IDs, stale/missing results and cancellation/Back evidence. |
| 6 · **#16**, depends on #11–#15 | Hybrid Settings; Dashboard, Guide, Recordings, DVR or last-primary startup; permitted per-server bookmarks, default Dashboard and disconnected fallback. | Handover permitted fields, separate server identities, fallback, and reset-to-now behavior. Never persist drafts. |
| 7 · **#17**, depends on #11/#14/#16 and media/provider gates | Full-screen default, optional background browsing/Now Playing and opt-in live-select guard. | Bind exact media/provider qualification and opt-in/Play separation to the candidate; no playback approval inferred from earlier read-only review. |

Later phases are planned directions, not Phase 1 features or assertions that their
issue-specific designs or evidence have been approved. Do not infer future API,
mutation, native test or release permission from a phase name.

## Architecture contracts for the next phase

- **Route, not focus, owns Back.** `ui_routes` has closed primary IDs and explicit
  child return. At a primary root, Back returns to Dashboard; at Dashboard it
  exits. Search's Guide drill-in returns to Search and does not overwrite the
  independent Guide bookmark. Recordings uses `LibraryBrowser` for show/season
  parent paths and remembers the exact originating mode and source filter for
  library Search. A native modal owns Back; dismissal is not approval.
- **One WindowXML and one owner.** Guide uses existing Hub content in the same
  window, with the six-destination bar both hidden and disabled. On entry, prefer
  usable content focus; Dashboard and unavailable content use a visible control.
  Left/right traverse a focused horizontal rail, Up/Down cross
  regions at their edges, and content directions browse the current view. Guide
  retains its horizontal time and vertical channel navigation; Page Up/Down
  navigate by viewport steps, and Down at the final channel reaches its rail.
  Context opens More; Info shows details, never opens a folder or plays. Use Kodi
  actions, not raw device scancodes. A cancelled dropdown preserves parent focus.
- **Published intent is not authority.** `ui_navigation` is the closed command
  registry, with shared eligibility for buttons, More and Kodi shortcuts. Capture
  command IDs and source, route, logical path, and selection identity as needed;
  compare with current state on the owner immediately before dispatch. Revalidate
  More's returned ID after the dialog, not by translated label or current row
  number. Keep existing per-operation admission, preview, confirmation, and
  worker checks authoritative. More is contextual and omits commands already on
  available direct/primary buttons. Navigation invalidates old view/selection intents;
  late operation results do not open a chooser over a different route.
- **Callbacks enqueue only.** A bounded mailbox fences input already queued before
  a route/window transition and input arriving during an actual blocking native modal
  boundary, including already-popped commands. Shutdown withdraws inputs and
  doesn't synthesize focus events. Data/art/status paint never calls focus repair.
  Keep window transitions, product publication and worker lifetime separate.
- **Pointer identity.** Destination and action buttons are native hit regions.
  Visible listing rows and Guide cells have bounded native hit controls over the
  existing drawing, bound to a route/source and opaque row or complete airing ID
  (source-qualified channel, event ID and start). A removed control ID is never
  rebound to another entity. A click selects that entity before invoking normal
  read/open behavior, never the old cursor's Play or Schedule. Overflow summaries
  are not individual airings. No coordinates are treated as durable identity.
- **Bookmarks are ephemeral.** Recordings restores by row and folder ID, Search
  by airing key, DVR by job ID and clamped index, and Guide by channel/airing and
  time viewport. Keep a valid parent/nearest in-range position when data moves or
  disappears. Do not retain an old provider index merely to implement Back. A
  source/fixture replacement invalidates data-dependent bookmarks. Search's return
  restores result identity, its page and the valid origin control independently of
  the primary Guide bookmark. In-session anchors survive feature switches. Phase 1
  adds no persistence; later sessions may retain only explicitly approved fields,
  reset Guide time to Now and must never persist drafts.
- **Dashboard stays the root at its own cadence.** The existing cards, absolute
  last success, warning, refresh settings and scheduling are not a navigation
  data source. Navigation changes the hint and chrome, not Dashboard statistic
  admission or rendering policy. Keep the Program script and legacy exact launch
  names (`Dashboard`, `Guide`, `Recordings`, `Scheduled`, `Search`); `Scheduled`
  resolves to DVR. Unknown or extra arguments cannot open URLs or run commands.

## Data, operations and rights

- No new endpoint, provider or admission condition, automatic connection opt-in,
  setting change, persistent data, playback request or write from entering a
  route, opening read details, moving focus, Back, paging, or showing a Search
  result in Guide. Pre-existing admitted reads, including automatic reads for an
  already opted-in connection and Dashboard's independent statistics schedule,
  retain their existing scope. Scheduled details are read-only, not evidence of
  ownership. Existing operations must be chosen explicitly; native Settings and
  Configure/Connect/Disconnect retain their existing explicit effects.
- Explicit Play is separate from normal selection and remains subject to its old
  revalidation and live consent. Explicit recordings/Passes commands keep their
  existing current-source, ownership/adoption, confirmation, durable checkpoint,
  and uncertain-write barriers. Never treat a read, matching listing, focus,
  cancelled dialog, stale activation or failed request as consent. No retries on
  ambiguous writes; preserve history and reconcile before any retry. A navigation
  review does not need a mutation, adoption, tuner reservation, or live playback.
- Preserve the MIT terms, notices and original Hub materials; see [Materials](MATERIALS.md)
  and [Identity](IDENTITY.md). Use existing Hub resources and Kodi APIs; do not
  copy TV Guide code, textures, screenshots, logos, fonts, layouts or bundled
  material. Any new material has its own provenance and rights gate. Provider
  media and metadata require the user's own permission and entitlement; no rights
  or service approval follow from source compatibility.

## Independent qualification for #11

1. **Source:** Verify exact worktree/candidate and targeted diff. Manifest, add-on
   identity, Program entry, provider/admission, settings definitions and operation
   workers remain unchanged. Audit the closed registry, callback/owner boundary,
   stale input and worker result suppression, and lack of new I/O from navigation.
2. **Omega static:** Follow [Development](DEVELOPMENT.md) and the current Toolkit
   development, validation and compatibility guidance. In the isolated Omega lane,
   first record `stub-lanes` evidence (actual interpreter, platform, Kodistubs,
   mypy, checker, cache and qualification scope); then validate the exact add-on
   root with the same interpreter and `--branch omega`. Check syntax, type/API,
   resources, packaging and official addon-check separately. `--branch` alone
   does not select stubs. Preserve warnings and failures, especially missing
   readiness/coverage; no substitution of another Kodi release's results.
3. **Fakes:** Run the bounded public runner plus the focused Dashboard, owner,
   navigation and connection cohorts. One focused invocation, from repo root:

   ```sh
   PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_navigation test_ui_dispatch test_dashboard test_package test_live_loader test_connection_config -v
   ```

   Check affected guide, library, search, artwork, explicit playback and Series
   cohorts too. The focused command is outside the public runner's audit guard;
   inspect scope first and report actual pass/fail/skip separately. Neither fakes
   nor stubs qualify a native skin or server. The public runner's module list is
   unchanged; its documented 372-test baseline predates the new mailbox and Series
   regressions. `test_navigation` is a separate focused cohort.
   The all-suite catalog-size assertion already fails on the unmodified 0.1.4
   baseline (51,570 bytes versus 49,152). Report this separately; do not relax the
   historical threshold or skip localization/ID and safety assertions.
4. **Native:** Pending independent verification. Use the normal authorized Claw
   Omega workflow with its normal network-enabled configuration, not a Lab or
   Toolkit change. No external Kodi, production release, provider setting
   changes, broad cleanup or profile Reset. Use disconnected and explicitly gated
   synthetic scenarios first. The owner authorizes bounded read-only production
   DVR checks as needed; skip them when representative fixtures suffice. That grant
   never permits production mutations or playback. Verify new source is running rather than merely an
   accepted launch dispatch.

   Check all six destinations (including old launcher paths), content entry and
   every focus region's Back; Guide's exclusive bar visibility; modal Back and
   cancellation; rapid Back/Select around a transition; exact mouse targets in
   different rows and overlap lanes and after scroll/refresh; Page/Info/Context
   on remote/keymap; list and Guide restoration; Settings, Scheduled and Passes
   dialog reachability; stale/empty/loading paths with no unsolicited focus move;
    Dashboard visible initial/return focus, action labels at 640×360, cards, warnings
    and cadence; and close/reopen. Verify native control
   ID allocation, transparent hit controls, focus after retirement, pointer
   click delivery and geometry at the actual skin/view scale. Fakes specifically
   do not prove those. No autoplay, unprompted settings changes, server mutation,
   native acceptance outside Omega or promotion of older release evidence.

## Restart, rollback and owner stop

At every phase handover record **privately**: exact baseline, candidate and rollback
commits, evidence identifiers/digests bound to candidate and environment; approved
issue/direction and target; what changed/what is excluded;
expected and observed route/fixture state; actual static/fake/native stages and
failures; current approval and stop condition; the next narrowly scoped action;
and the last verified rollback identity. Transmit only sanitized findings here.
Keep environment paths, credentials, server identifiers, profile data, logs,
screenshots, private evidence and production data out of tracked files. Recheck
source/target identity and pending commands when resuming, not just the issue label.

An unattended campaign requires explicit owner approval first, and may then advance
only preauthorized phases with independent gates. Hold on ownership conflict,
ambiguous operation, privacy/license/rights changes, playback or persistent mutation
scope, failed validation, or unrecoverable environment drift. Record the exact
commit, independently verified evidence, blocker, safe recovery and owner decision
still needed; resume only after re-establishing scope, state and applicable gates.

If a gate fails or scope changes, stop. Isolate the phase; revert its source or
restore the last explicitly approved compatible add-on bytes through the authorized
workflow, preserving add-on ID, settings, profile and operation history. Do not
overwrite retained release bytes or use a Reset, broad cleanup, write retry or
profile deletion as recovery. Verify the rollback's actual navigation, source and
state; do not claim it reconciles uncertain server writes. Review, completion and
rollback evidence stay independently qualified. **Without an explicitly approved
campaign, stop for owner approval before advancing a phase.** Expanding test scope,
changing a DVR or packaging for distribution or publication require separate approval.
