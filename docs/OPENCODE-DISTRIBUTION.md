# Distribution preparation in OpenCode

The add-on source is `kcook98765/script.channelsdvr.hub`. Its independent Kodi
distribution repository is **`kcook98765/kcook-lab-repo`**, GitHub repository ID
`1428864210`. Keep source and distribution origins distinct. The distribution
checkout may retain the old local folder name `ai-kodi-repository`; inspect its
actual Git remote before reusing it.

From this selected source project, use the separately installed
`github-operator` primary or `/kodi-release-prepare`. The command selects the
operator without changing projects. Ordinary developer/tester/reviewer roles
retain their existing restrictions. Exact source/distribution enrollment is
required; this document alone grants no tool or filesystem access.

The version-controlled restoration templates and staging-only tool live in the
[distribution integration guide](https://github.com/kcook98765/kcook-lab-repo/blob/7fc2bbcca009ece625f71413a9a9471edf1aeb5f/docs/OPENCODE-CROSS-REPOSITORY.md).
They restore the command, operator, project enrollment, source instruction
fragment and the existing maintenance skill. Keep generated profiles, credentials,
host paths and local evidence outside Git. Existing models, providers, private
authentication, services and Toolkit environments remain installation inputs.

Load `kodi-repository-maintenance`, resolve its paths against the verified
distribution checkout, and read its AGENTS.md, operator supplement,
docs/AGENT-INTEGRATION.md, docs/RELEASE-WORKFLOW.md and docs/APPROVAL-CONTRACT.md.
Inspect both local HEADs, remotes and uncommitted changes before selecting inputs.
Preserve version immutability and independent review; source edits need their own
task scope. Missing approvals block publication, not truthful private preparation.

## Published 0.1.2 and current source candidate

The published **0.1.2 ALPHA** source is
`34f1194ad19f3b534b22d1c5aaa41bdd3e4880f8`; the retained published ZIP SHA-256 is
`7c04857b3094415c1028ed85b0434712d924191f2cc890e90a89610607e91e69`.
The artifact has 107 runtime members. Keep that immutable artifact and its evidence
separate from the unpublished **0.1.3 ALPHA** reconciliation candidate, which adds
issue #4's Dashboard refresh behavior. Neither the published artifact's history nor
the prior `0.1.3~issue4` candidate establishes connected/native acceptance of the
combined source.

Current local preparation uses the version-controlled `tools/package.py` after
reviewed runtime changes are committed. It rejects runtime inventory/byte drift
from that commit, including edits hidden by Git index flags, and retains exact
member hashes and deterministic ZIP provenance. An in-progress merge or changed
runtime is not packageable as a final source identity. Preparing the candidate
does not authorize distribution changes or publication.

## Historical 0.1.1 preparation evidence

The historical **0.1.1** source commit
`0c88530efb75e2518c62d1890747bfbc161fed10` changed the manifest from 0.1.0 to
0.1.1 for DVR address normalization across Settings and Connection. Its news,
translations and package-version assertion agree. The OpenCode configuration
repair did not change runtime bytes or bump the add-on version.

The 2026-11-14 private preparation handoff binds exactly that source commit:

- Add-on: `script.channelsdvr.hub` / `0.1.1`.
- ZIP SHA-256: `0cd5c4da84f6664fb68c5c824c8326e0ffcbcff14d1aff7adb1d6a9756e54046`.
- The same 107 runtime files occur in both Nexus/Omega-named package copies.
- Local provenance records the source commit, per-file hashes and separately
  hashed untracked packager input. The whole source checkout was not clean.
- Local packet: `.local/reports/cross-repository-workflow-20261114/HANDOFF.md`.
- All six Omega static stages passed for that snapshot; live URL checks remained
  unverified. Nexus-named packaging is not current Nexus qualification.

This is a dated evidence index, not a release approval or a promise that ignored
artifacts exist in a new clone. Verify retained bytes against these bindings;
otherwise prepare a new packet and disclose unavailable evidence. Do not rewrite
the old packet's source commit after documentation-only integration commits, or
overwrite differing provenance under the same evidence filename. Unchanged
runtime bytes do not require another version bump for documentation changes.
Older 0.1.0 receipts do not qualify 0.1.1.

Independent material/rights, service-use and technical review, current-byte native
acceptance for selected scope, a genuine hash-bound review record, final production
qualification/site review, and exact owner publication authorization remain
outstanding. Neither this setup nor its preparation command authorizes publishing,
Kodi native tests, DVR mutation or service changes.
