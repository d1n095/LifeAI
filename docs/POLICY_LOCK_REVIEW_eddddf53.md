# Policy-lock review: commit `eddddf53`

Independent technical input for the founder's own review and re-signing decision.
This document makes no change to `security/POLICY_LOCK.json` and asserts no authority to
approve it — regenerating the lock is explicitly reserved for founder-authorized human review
(see `backend/scripts/security/verify_policy_lock.py`'s own docstring: "Adding a control is
therefore an explicit, reviewed act... never a side effect").

## 1. Exact locked file and changed hashes

`security/POLICY_LOCK.json` pins SHA-256 digests (`algorithm: "sha256"`) of 27 protected
control files, regenerated only via `verify_policy_lock.py --update` in a reviewed commit.
The entry in question:

```
"security/POLICY_LOCK.json" entry for
".github/workflows/real-local-ai-verification.yml":
  locked (stored):  09076e19a88749e04a9d31f062441afe967d6e2dc0d36eadfedfb32636176750
  actual (at 192ce7b6, current head): 5febc65c8064d7b9eca3cfb3317ba0acaabd07ec88004eb8e996c57356706091
```

Computed directly with `sha256sum .github/workflows/real-local-ai-verification.yml` against
the real file at the current candidate head — not assumed. This is the **only** entry out of
27 that mismatches; every other locked control still verifies.

## 2. The changed workflow — exact diff

`eddddf53` touches exactly one file, with no other hunks:

```
git diff 28954b31 eddddf53 -- .github/workflows/real-local-ai-verification.yml

@@ -204,8 +204,12 @@ jobs:
         env:
           # Dedicated database: tests/conftest.py force-DROPs the DB it derives from
           # DATABASE_URL, so it must never point at lifeos_reallocal.
-          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/lifeos_reallocal_pytest
-          APP_DATABASE_URL: postgresql://mainai_app:mainai_app_reallocal_pw@localhost:5432/lifeos_reallocal_pytest
+          # Must match tests/conftest.py's DISPOSABLE_DATABASE_NAME pattern
+          # (^lifeos_test(?:_[A-Za-z0-9]+)*$) — pytest refuses to drop any database whose name
+          # doesn't match it, as a guard against ever dropping an installed application's real
+          # database. `lifeos_reallocal_pytest` predates that guard and no longer matches it.
+          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/lifeos_test_reallocal
+          APP_DATABASE_URL: postgresql://mainai_app:mainai_app_reallocal_pw@localhost:5432/lifeos_test_reallocal
         run: |
           set -eo pipefail
           python -m pytest tests/backend/rag/test_embedding_space.py -q \
```

`git diff --stat` for the whole commit: **1 file changed, 6 insertions(+), 2 deletions(-)** —
nothing else in the commit, no other file touched.

**`permissions:` block** (lines 39-40, unchanged by this commit):
```yaml
permissions:
  contents: read
```
Read-only, unmodified. No `write`, `id-token`, or `actions` permission is requested anywhere
in the file, before or after `eddddf53`.

**Trigger (`on:`, lines 27-37, unchanged):** `workflow_dispatch` only, with two optional text
inputs (`candidate_ref`, `candidate_sha`) — not `push`, not `pull_request`, not scheduled. It
cannot fire from an external contributor's PR.

**Secrets:** `grep -n "secrets\."` over the file returns zero matches, before or after this
commit. The workflow uses no repository secrets at all.

Conclusion for this section: the change touched one environment-variable pair's literal
database-name string (plus an explanatory comment), inside a `run:` step already scoped by
the unchanged `permissions: contents: read`. Nothing touching trust boundaries, permissions,
triggers, or secret handling was changed.

## 3. External network and artifact handling

The workflow (header comment, lines 3-25, unchanged by this commit) installs Ollama "from a
release archive authenticated against its published SHA-256 digest, then removed with the
runner," uses only throwaway Postgres/pgvector and Redis service containers, and fakes
outbound email. `CHAT_FALLBACK_ORDER` is empty and no paid-provider key exists, so a failed
local call cannot silently become a paid external call. None of this network/artifact
handling is touched by `eddddf53` — the change is entirely inside one `run:` step's
environment block, downstream of all of the above.

## 4. Secret and token exposure risk

None found, before or after. No `secrets.*` reference exists in this file. The changed lines
are a Postgres connection string using the hardcoded, non-secret `postgres`/`postgres` and
`mainai_app`/`mainai_app_reallocal_pw` credentials already present pre-change (unchanged
values — only the **database name** suffix changed, from `lifeos_reallocal_pytest` /
`lifeos_reallocal_pytest` to `lifeos_test_reallocal` / `lifeos_test_reallocal`). These are
throwaway service-container credentials for a disposable CI database, not production secrets.

## 5. Expected lock update

A correct re-signed entry replaces only this one line in `security/POLICY_LOCK.json`:

```diff
-    ".github/workflows/real-local-ai-verification.yml": "09076e19a88749e04a9d31f062441afe967d6e2dc0d36eadfedfb32636176750",
+    ".github/workflows/real-local-ai-verification.yml": "5febc65c8064d7b9eca3cfb3317ba0acaabd07ec88004eb8e996c57356706091",
```

A reviewer can verify this arithmetically without trusting this document:
`sha256sum .github/workflows/real-local-ai-verification.yml` at commit `192ce7b6` (or any
commit from `eddddf53` onward, since no later commit in this chain touched the file again)
must equal `5febc65c8064d7b9eca3cfb3317ba0acaabd07ec88004eb8e996c57356706091`. All 26 other
locked entries are unaffected and must not change.

The correct, reviewed command to produce that update is:
```
python backend/scripts/security/verify_policy_lock.py --update
```
run in a commit that a human reviews and signs off on — not run by this document or by any
automated process. This document does not run it.

## 6. Verification results (reproduced directly)

Direct script invocation, at the current candidate head:
```
$ python backend/scripts/security/verify_policy_lock.py --repo-root ..
policy lock verification FAILED:
  - changed control: .github/workflows/real-local-ai-verification.yml (expected 09076e19a887…, found 5febc65c8064…)

If the change is intended, regenerate with `python backend/scripts/security/verify_policy_lock.py --update` and include both the control change and the regenerated lock in the SAME reviewed commit.
```

The pytest wrapper, `test_the_repository_policy_lock_verifies`, reproduces the same single
finding:
```
>       assert policy_lock.verify_lock(REPO_ROOT, lock) == []
E       AssertionError: assert ['changed con...ebc65c8064…)'] == []
E         Left contains one more item: 'changed control: .github/workflows/real-local-ai-verification.yml (expected 09076e19a887…, found 5febc65c8064…)'
FAILED tests/security/test_security_controls.py::test_the_repository_policy_lock_verifies
```

Exactly one finding in both cases — no other control is reported as changed, missing, or
uncovered.

## Technical opinion (input to founder review, not a decision)

This change is a test-infrastructure-only string fix: it renames a disposable CI database so
it satisfies a safety pattern (`tests/conftest.py`'s `DISPOSABLE_DATABASE_NAME`) added after
this workflow was first written, with no change to the workflow's triggers, permissions,
secret usage, external network/artifact handling, or any other file. The lock is stale purely
because the one hash moved for a reason unrelated to what the lock exists to catch (a
workflow quietly gaining more privilege, a new unreviewed control, a weakened check). Nothing
found here suggests the underlying change needs anything beyond the single-line hash update
in section 5.

This is a technical read of the diff, offered as input — the actual re-signing decision and
action remain the founder's, per the project's own stated policy.
