# Z.ai R21–R30 Integration Procedure

Status at time of writing: **no delivery beyond R12 exists anywhere reachable.** This document
is a mechanical procedure for whoever integrates a verified R21–R30 bundle later — it does not
claim one exists now, and it should not be read as evidence that integration has started.

## 0. Fresh reachability check (re-run this, do not trust the result below as still current)

```
git ls-remote origin | grep -i zai
git ls-remote origin | grep -iE 'r2[0-9]|r30|security-kernel'
git fetch origin <claimed-sha>   # expect "not our ref" if still unreachable
git cat-file -t <claimed-sha>    # expect "could not get object info"
```

Re-run as of this document's writing (exact head `192ce7b6e930ba54e5b9777d43c642c6e611eadc`):
only `refs/heads/zai/cognitive-core-r12-release-cc4d2804` exists; the `r2[0-9]` grep's hits are
false positives on unrelated branch names (`pr237`, `pr244`) containing the substring by
accident, not real R2x refs. 1324 total remote refs checked. **Nothing beyond R12 is reachable.**
Treat any claimed SHA for R21–R30 as unverified until the commands above say otherwise.

## 1. Provenance verification (before trusting ANY claimed bundle/branch)

1. `git bundle verify <file>.bundle` — must report valid before anything else happens.
2. Unpack into a scratch repo and inspect `git log --format='%H %an %ae %ad %s'` for every
   commit — author identity and dates should be internally consistent, not fabricated to match
   an expected timeline.
3. Confirm the bundle's tip commit hash matches EXACTLY the SHA claimed in whatever directive
   names it. A bundle that doesn't produce the claimed SHA is not the claimed delivery.
4. `git log --format='%H' <claimed-sha>` inside the unpacked scratch repo, then diff that
   commit's parent chain against this repo's current history to find the actual merge-base —
   do not assume it's a clean descendant of the current integration head without checking.
5. Only after 1–4 pass: fetch the bundle into an isolated worktree (never the shared checkout)
   and proceed to section 4 (conflict reconciliation) before writing any integration code.

This mirrors the discipline already applied to every other claimed external delivery in this
integration effort (Z.ai R20 at `6b725450...`, R21–R24 at `8ea29a6b...`, DeepSeek v4, MiniMax's
`204cdd4f` — all confirmed BLOCKED by exactly these checks, never assumed present).

## 2. Migration slot reservation

**Do not hardcode `0089` as "the" current head when this procedure is actually followed.**
Re-derive the real current head first:

```
cd backend && python -c "
import ast, pathlib
heads = set()
down = set()
for f in pathlib.Path('alembic/versions').glob('*.py'):
    tree = ast.parse(f.read_text())
    rev = down_rev = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], 'id', None)
            if name == 'revision': rev = ast.literal_eval(node.value)
            if name == 'down_revision': down_rev = ast.literal_eval(node.value)
    if rev: heads.add(rev)
    if down_rev: down.add(down_rev)
print('real head(s):', heads - down)
"
```

(AST parsing, not a regex scan — a regex scan on this exact codebase has produced false
positives twice already in this integration effort, once matching a prose line inside a
migration's own module docstring.)

If R21–R30 includes a migration numbered `0090`, its `down_revision` must point to whatever
that command prints as the real current head — not a number copied from this document. If the
real head has moved to something other than `0089` by the time this is followed, that is
expected drift from other work landing in between, not an error in this procedure.

## 3. The existing authorization/"grant" architecture — what must be preserved

**Correction to how this item was originally framed**: there is no existing "dedicated
grant-issuer role" in this codebase today — a prior audit in this same integration effort
(`docs/FOUNDER_ALPHA_INTEGRATION_MANIFEST.md`) explicitly confirmed zero `grant_issuer`/R21-R24
references anywhere in `backend/`, i.e. no scaffolding anticipating it. The phrase most likely
refers to something R21–R30 itself would introduce, not something already present. Do not
invent a "preserve" story for a mechanism that doesn't exist; instead, preserve the real
existing grant-issuance path it would sit alongside:

- **`app/models/execution_envelope.py`**: `ExecutionScopeProposal` (a proposal, never itself
  authority) and `ExecutionAuthorizationEnvelope` (real founder-granted authority — "the only
  path here is `app.execution_envelopes.service.authorize_execution_scope()`", per the model's
  own docstring). Both are **goal-scoped** — `goal_id` is a non-null FK on both tables. This is
  the confirmed, standing gap behind task #61 (no founder-wide, non-goal-scoped authorization
  primitive exists) — see section 5.
- **`app/execution_envelopes/service.py:authorize_execution_scope()`**: the only function that
  may create a real envelope. It never defaults `authorized_by`/`authorized_paths`/
  `authorized_capabilities`/`authorized_risk` from the proposal's own suggested values — the
  caller must pass them explicitly, every time, as a deliberate reviewed act. Any new
  "grant-issuer" mechanism R21–R30 introduces MUST still go through this function to create
  envelopes, or must be rejected as a second, competing authorization path (this project's
  explicit standing rule: no second authorization system — see founder decision on task #60).
  If a new DB role or credential set is part of the delivery, verify it is used only to
  authenticate the *caller* of `authorize_execution_scope()`, never as an alternate way to
  write `execution_authorization_envelopes` rows directly.
- First-governance TOCTOU fencing: `authorize_execution_scope()` locks the goal row
  (`SELECT ... FOR UPDATE`) before transitioning state, the same pattern used in
  `recovery_takeover.py`. Any new code path must not bypass this lock.

## 4. Conflict reconciliation checklist — where a Z.ai delivery most plausibly collides

Based on what this integration effort's own work has touched in the same security-relevant
surface area (most recently through PR #491):

| Area | File(s) | Why it's a likely collision point |
|---|---|---|
| Central egress/lease gate | `backend/app/providers/registry.py` (`chat_with_fallback()`, `prepare_single_provider_chat()`) | Already the single enforcement point for authorization+egress before any provider call, including the new streaming path added this round. A security delivery touching provider routing will almost certainly touch this file. |
| Lease verification | `backend/app/jobs/mainai_job_lease.py` (`verify_active_lease()`) | Referenced by the central gate above; any R21-R30 lease-fencing work lands here. |
| DB privilege policy | `backend/scripts/security/s1a_privilege_policy.py` | Already the site of one real merge-collision bug this effort found (SFA-1: two independently-written privilege fixes silently fighting, no textual conflict). A second security delivery narrowing privileges here needs the same careful check — don't assume no-conflict just because `git merge` reports none. |
| Local-only enforcement | `backend/app/config.py` (`local_only_mode`) | New this integration effort (task #60); a security delivery may reference or extend it. |
| Streaming auth boundary | `backend/app/routers/chat.py` (`POST /api/chat/stream`, `/api/chat/messages/{id}/retry/stream`) | Brand new this round (PR #487) — not yet exercised by any external security review; highest-risk new surface for an incoming delivery to interact with. |
| Founder-wide authorization gap | `backend/app/mainai_execution/lesson_conflicts.py` (`detect_conflict()`) | Deliberately fail-closed pending task #61 — see section 5, do not let an incoming delivery resolve this silently. |
| Migration DAG | `backend/alembic/versions/` | Single head at whatever section 2's AST check reports — check for duplicate revision IDs or orphaned `down_revision` the same way every prior migration-touching PR in this effort has. |

## 5. Non-negotiables

- Integration must not weaken any existing authorization boundary (central egress, RLS,
  `s1a_privilege_policy.py`'s privilege floor, lease fencing) to make a security delivery's own
  code fit more easily.
- Integration must not silently activate a founder-wide (non-goal-scoped) privilege. Task #61
  (the shape of a founder-wide authorization primitive) is **explicitly gated and unresolved**
  by founder decision, not an oversight. If R21–R30's own design assumes that decision has
  already been made — e.g. if it expects `ExecutionAuthorizationEnvelope` to support a null
  `goal_id`, or introduces its own founder-wide grant table — **stop and escalate to the
  founder for an explicit decision**. Do not infer an answer and proceed; do not leave
  `lesson_conflicts.detect_conflict()` (or any other fail-closed gate blocked on #61) open just
  because new code assumes it should be.
- No code written to fill the gap "because the delivery didn't arrive." If R21–R30 remains
  unreachable, the gap stays a documented gap — this procedure's existence is not itself
  permission to fabricate a replacement.
