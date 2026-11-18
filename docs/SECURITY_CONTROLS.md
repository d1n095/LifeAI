# Security controls — Founder Alpha (Guardian foundation)

This is the control matrix for starting MainAI privately. It states, for each control, what it
actually proves, where it lives, and whether it is **enforced today** or **prepared and waiting on
a repository setting only an administrator can change**. Nothing in the "enforced" column is a
claim that the system is secure; see [Limits](#limits-stated-plainly).

Everything below is verified by `.github/workflows/security.yml`, whose last job (`gate`) fails when
any mandatory control did not succeed. There is no `continue-on-error` anywhere in the workflows;
`verify_policy_lock.py` fails the build if one is added.

## Control matrix

| # | Requirement | Control | Where | Enforced today |
|---|---|---|---|---|
| 1 | Automated static security analysis | Semgrep with a reviewed, project-specific rule set (command injection, `eval`/`exec`, SQL interpolation, unsafe YAML/pickle, JWT signature bypass, TLS verification off) | `.semgrep/rules.yaml`, job `semgrep` | **Yes** — findings at `ERROR` fail the gate |
| 2 | Secret detection / credential-leak prevention | gitleaks with the default ruleset plus value-based allowlists for test placeholders | `.gitleaks.toml`, job `gitleaks` | **Yes** — working tree, blocking. Git **history** is not scanned by default (see limits) |
| 3 | Dependency / supply-chain analysis | OSV-Scanner over the manifests; `npm audit` already in `ci.yml` | job `osv-scanner` | **Yes** |
| 4 | Security review of Actions, install scripts, migrations, privileged paths | Security-sensitive paths are integrity-locked and code-owned; privileged DB policy is a reviewed module applied by reviewed scripts | `security/POLICY_LOCK.json`, `.github/CODEOWNERS`, `backend/scripts/security/s1a_privilege_policy.py` | **Integrity: yes.** CODEOWNERS *review requirement*: prepared, needs branch protection (below) |
| 5 | Isolated execution of untrusted AI-generated code | Per-attempt git checkout for writes; per-attempt local-only base repository for proposals | `app/mainai_execution/worktree.py`, `app/mainai_execution/proposal_worktree.py` | **Yes** — proposal mode never writes to the live checkout; PI-1d rollback retained as defense in depth |
| 6 | Explicit agent capabilities, least privilege | `security/agent_capabilities.json` cross-checked against the real `CAPABILITY_MANIFEST` by AST (no import, no execution) | `backend/scripts/security/verify_agent_capabilities.py` | **Yes** — drift, contradiction, or a forbidden grant fails the build |
| 7 | Independent review and approval boundaries | Founder approval for push/PR recovery and for merge; agent may open drafts only; agent never approves its own change | `app/mainai_execution/approval.py`, `app/mainai_execution/recovery_approval.py`, `security/agent_capabilities.json` | **Runtime: yes.** GitHub-side review requirement: prepared (below) |
| 8 | Build provenance | SHA-256 manifest per file + one tree digest, created and re-verified in CI | `backend/scripts/security/build_provenance.py`, job `provenance` | **Yes** (integrity of the measured tree; not a signed attestation) |
| 9 | Tamper resistance for policies and CI | Every control file is hashed; changed / missing / **newly added** control fails the build | `security/POLICY_LOCK.json`, `verify_policy_lock.py` | **Yes** |
| 10 | Fail-closed on critical check failure | The `gate` job requires `success` from every mandatory control, including `cancelled`/`skipped` | `.github/workflows/security.yml` job `gate` | **Yes** |
| — | Deep data-flow analysis (CodeQL) | `github/codeql-action` for python + javascript-typescript | job `codeql` | **No — requires GitHub Advanced Security on a private repository.** See the administrator action |

## Administrator actions (prepared, not enforceable without repo admin)

1. **Require code-owner review.** Settings → Branches → protect the integration branch → *Require a
   review from Code Owners*. Without this, `.github/CODEOWNERS` is a request, not a gate.
2. **Make the Security workflow required.** Settings → Branches → *Require status checks to pass* →
   select `Security — gate (fail closed)`. Without this the gate runs but nothing blocks a merge on it.
3. **Enable CodeQL** (private repos need GitHub Advanced Security): enable GHAS, then set the
   repository variable `CODEQL_ENABLED=true`. The job then runs and fails closed. Until then the
   gate prints `codeql (optional, requires GHAS): NOT ENFORCED` — deliberately visible rather than
   silently green.
4. **Disallow force-pushes and direct pushes to the integration branch**, so every change to a
   control file passes through review (control 9's lock is only meaningful if history is not
   rewritten around it).

## Updating a control

Changing a security control is a normal, reviewable act — it is not meant to be impossible:

```bash
python backend/scripts/security/verify_policy_lock.py --repo-root . --update
```

The control change and the regenerated `security/POLICY_LOCK.json` belong in the **same** commit, so
a reviewer sees exactly which control changed. `--update` from CI would defeat the control; the
workflow only ever runs the verifying mode.

## Fail-closed semantics

* Any mandatory scanner failing, being cancelled, or being skipped fails `gate`.
* An unreadable or empty lock/capability manifest exits `2` (error), never `0`.
* `verify_policy_lock.py` treats a protected-pattern file that is **not in the lock** as a failure,
  so adding a workflow that disables a check cannot slip through.
* `verify_agent_capabilities.py` fails on drift in either direction: a capability in code that is not
  declared, or a declared capability that does not exist.

## Limits, stated plainly

* **Static analysis cannot prove the absence of malicious code.** Semgrep and CodeQL find known
  shapes; they do not judge intent. The controls that actually bound an AI-authored change are
  *runtime*: the isolated per-attempt checkout, the explicit capability manifest, founder approval
  before push/merge, and the fact that MainAI has no merge or deploy code path at all.
* **gitleaks in CI scans the working tree**, not full history. History scanning is a separate
  advisory run; making it blocking on a repository that already has history would produce an
  unpassable check, which in practice ends with the check removed.
* **Provenance here is integrity, not authenticity.** It detects unintended or unaudited change in
  the measured tree. It does not prove who built the artifact (that needs a signed attestation) and
  says nothing about review quality.
* **`security/POLICY_LOCK.json` cannot hash itself.** It is protected by review (CODEOWNERS) and by
  being visible in every diff that touches a control.
* **Semgrep's SQL-interpolation rule deliberately excludes** the Alembic migrations, `app/rls.py`,
  `backend/scripts/security/*` and `backend/scripts/ops/db_recovery.py`: those build DDL from fixed
  identifier lists over an admin connection, reviewed when the rule was added. Runtime query code
  (`app/**`) stays covered. A new DDL builder needs its own reviewed exclusion.
