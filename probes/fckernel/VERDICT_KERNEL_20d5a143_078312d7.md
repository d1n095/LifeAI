# Independent examination — Codex Security Kernel candidates (STOP-clear + FA-PRIV)

Archive handoff delivered 2026-10-26; examined read-only. `first_cycle_go=false`.

| Candidate | Commit | Branch | Migration head |
|---|---|---|---|
| FA-9 / CO-10 authenticated STOP-clear | `20d5a14301f574efe0ba3cc1d2d2f694b6cc1ef4` | `codex/founder-stop-clear-reauthorization` | `0097_authenticated_stop_clear` |
| FA-PRIV STOP-revocation boundary | `078312d7abce3f5c0f87c9e7b30161973e55d532` | `codex/fa-priv-closure-integration-readiness` | `0098_fa_priv_stop_revocation_boundary` |

## 1. Archive integrity + identity — INDEPENDENTLY VERIFIED
- **Bytes:** both `.tar.gz` are valid gzip; every file matches the in-archive `SHA256SUMS` (independent recompute).
- **Git bundles:** `git bundle verify` reports a **complete history**; the bundle heads are exactly the
  claimed SHAs. Fetched into the examiner repo: both commits + ancestry present.
- **Commit/tree identity (the cryptographic anchor):** fetched tree SHAs equal the manifest's claimed
  values — STOP-clear `tree=be77815cec…`, FA-PRIV `tree=f15043b81a…`; parents correct
  (FA-PRIV's parent **is** STOP-clear; STOP-clear's parent is `7fbe6771`, the GitHub-App-publication
  line — a newer base than #306/#303, examined here on its own delta).
- **Migrations:** `0097_authenticated_stop_clear.py` and `0098_fa_priv_stop_revocation_boundary.py`
  exist in the respective trees. (Minor: FA-PRIV manifest labels the head `0098_fa_priv_stop_boundary`
  vs the actual filename `…_stop_revocation_boundary.py` — immaterial.)
- **No self-certification:** both manifests declare `independently_certified=false`,
  `published_to_github=false`, `first_cycle_go=false`. Codex's `EVIDENCE.md`/XML (e.g. "3491 passed",
  "61 focused security") are read as **claims only**; certification rests on the independent checks below.
- The git author identity ("Dennis Torildson") is a commit config value, **not** an authorization
  signal; trust here comes from the verified SHA/tree + the executable checks.

## 2. Security-boundary examination — `probes/fc_kernel_v1`, 7 passed (drives the real kernel)
| ID | Boundary | Result |
|---|---|---|
| K1 | **FA-PRIV:** ordinary `mainai_app` runtime cannot UPDATE `workforce_authority_epoch` | PASS — `permission denied` |
| K2 | **FA-PRIV / CO-10:** the legacy direct `clear_owner_stop` string-ack path is dead for the runtime | PASS — refused ("legacy acknowledgement text is not STOP-clear authority; use authenticated Founder STOP-clear") |
| K3 | **FA-PRIV:** runtime cannot mint a `founder`-role identity (migration 0095) | PASS — insert blocked |
| K4 | **Authority UNKNOWN fails closed** (`read_stop_authority_snapshot`) — closes the PI-STOP C2 gap | PASS — `StopAuthorityBlocked: STOP_AUTHORITY_UNKNOWN` when epoch rows absent |
| K5 | **CO-10:** a non-Founder cannot issue a STOP-clear authorization | PASS — `FounderAuthorityError` |
| K6 | **CO-10:** even the identity-pinned Founder needs a live access session **and** correct password | PASS — `StopClearError` on missing `access_jti` and on wrong password |
| K7 | **CO-10:** consuming a fabricated/non-existent authorization cannot clear STOP | PASS — `StopClearError` |

### What this establishes
- **CO-10 CLOSED:** STOP-clear is now an **authenticated one-shot proof**, not a string. The code
  (`founder_authority/stop_clear.py`) binds one authorization to the identity-pinned Founder
  (`user.id == FOUNDER_USER_ID`), the correct password, a **non-revoked current access session**, the
  exact stopped epoch, a replay-guarded `operation_id`, and a TTL'd authorization consumed exactly once
  (`clear_stop_with_authorization`). K5/K6/K7 exercise the identity/password/session/fabrication legs
  executably; the full issue→consume→replay-rejection happy path is additionally code-verified (it
  requires a live signed session that the examiner harness cannot mint, by design — itself a control).
- **FA-PRIV CLOSED (DB-level):** the ordinary runtime role cannot write the authority epoch (K1), use
  the legacy clear (K2), or mint a Founder identity (K3); a new SECURITY-DEFINER trigger
  (`security_kernel_revoke_authority_after_stop`, `mainai_app_execute=false`) and REVOKEs on the
  stop-clear tables route all authority mutation through the authenticated kernel path, with erasure
  via the governed `erase_own_workforce_authority_epoch()` function. Authority is **never implicitly
  granted** — UNKNOWN is a fail-closed exception (K4).

## 3. Cross-reference to prior examiner findings
- **Closes CO-10** (previously `coord_accept_v1` CO-10 failing; `pistop_v1` C1 blocked-on-Codex).
- **Closes the PI-STOP "authority UNKNOWN → fail closed" leg** (`pistop_v1` C2) via
  `read_stop_authority_snapshot`.
- **Strengthens FA-PRIV** (previously `fa_core_v1` FAC-1..5 failing): runtime DB-write to authority
  state is denied and Founder-identity minting is blocked.
- Note: my older `coord_accept_v1`/`fa_core_v1` suites were written against an earlier coordination
  base and no longer apply verbatim to this newer line (`7fbe6771`); `fc_kernel_v1` is the
  base-correct replacement that drives the actual kernel surfaces.

## 4. Verdict
**STOP-clear `20d5a143` and FA-PRIV `078312d7`: archive integrity, commit/tree identity, migrations and
the core security boundaries are INDEPENDENTLY VERIFIED, and CO-10 + FA-PRIV are CONFIRMED CLOSED at
the examined boundaries (fc_kernel_v1, 7 passed).** These are Codex-owned kernel candidates; this is an
independent examination, not a modification or self-certification — I did not run the archive's
`verify-handoff.sh`. Not yet a release certification: the candidates sit on a newer base
(`7fbe6771`, GitHub-App publication line) that itself has not been through full examiner certification,
and the authenticated-clear happy path's live-session leg is code-verified rather than executed. No
merge/deploy/GO/publication. `first_cycle_go=false`.

## Reproduce
```
# integrity (independent): shasum -a 256 -c SHA256SUMS in each extracted dir; git bundle verify <bundle>
# identity: git fetch <bundle> <sha>; git log -1 --format='%H %T %P' <sha>
probes/fc_kernel_v1/run.sh ../../wtcert-078312d7     # 7 passed (K1..K7)
```
