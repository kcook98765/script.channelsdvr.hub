# Add-on identity and state

| Item | Current source identity |
| --- | --- |
| Name | Channels DVR Hub |
| Kodi add-on ID and package folder | `script.channelsdvr.hub` |
| Version | `0.1.0` pre-release |
| Target | Kodi 21 / Omega |
| Entry | Program script, `default.py` |
| Python namespace | `channels_dvr` |
| Repository | `kcook98765/script.channelsdvr.hub` |

The repository name is distinct from the Python namespace and backend brand.
Channels DVR remains the backend's name; Hub is the unofficial Kodi client.
Repository publication does not imply an installed add-on, a packaged release or
native acceptance. Hub has not yet been installed or natively accepted.

## Separate Kodi profile

The former add-on ID was `script.channelsdvr.epg`. Hub is a **separate Kodi add-on
identity and profile**, not an in-place update to that identity. Hub resolves its
own profile through Kodi; connection configuration starts blank and fresh setup
is expected.

There is no automatic discovery, copying, migration, reset or ownership adoption
of the old EPG profile. Existing settings, SQLite state, journals and pending
intents remain associated with the old identity. Standard-library SQLite and
profile-inherited privacy do not add new OS-specific storage guarantees.

## Reconcile before writes

Before any Hub mutation, reconcile unresolved old operations against their bound
source and operation identities while preserving ownership and uncertainty
history. Establish an **exclusive writer**: do not run the old and new clients as
concurrent writers against the same managed state.

A fresh profile must not bypass an unresolved old write or uncertainty barrier.
Do not infer ownership from matching names or similar rules. Explicit Series
adoption is a guarded feature, not an automatic profile migration or a substitute
for reconciliation. Any future migration needs a separately designed and reviewed
process; none is implemented here.

Existing deployment registrations and historical results remain bound to their
original identity. They must not be silently repointed to Hub or described as Hub
installation evidence. Keep operational history intact and secrets out of Git.

See [User guide](USERGUIDE.md) for source-level workflows and
[Limitations](LIMITATIONS.md) for acceptance boundaries.
