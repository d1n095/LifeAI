# Disaster-Recovery Decision Package (2026-11-08)

> **Purpose.** Turn the open DR gaps from PR #366 into **implementable options with tradeoffs**
> and mark which choices need **Founder authorization**. Follow-up to #366 — **its frozen
> candidate is not modified**. No paid provisioning, no credential creation, no production
> changes are performed here; this is a decision document. `first_cycle_go=false`.

## Starting point (what #366 already secured — do not re-litigate)

`scripts/vps/backup.sh` now publishes atomically (hidden `*.partial.*` → integrity-gate → SHA-256
→ atomic `mv` → `sync`), prunes only checksum-verified archives, serializes with `flock`, and
cleans up on crash. `scripts/vps/restore.sh` verifies the whole-archive checksum before extraction,
rejects non-tar/corrupt archives, and re-verifies per-blob content-addressing + the backup-time
manifest. Verified on Linux CI (30/30 failure-injection + real ENOSPC; run `42509439439`). **Scope
of #366 was single-host backup integrity** — everything below is the *remaining* DR surface.

## Open decisions and implementable options

### 1. Encrypted off-host backups — **the top gap**
Backups live only on the same VPS disk they protect (`/var/backups/lifeai`), so a host/disk loss
destroys both. Options (all reuse the existing `backup.sh` archive as the unit):

| Option | How it reuses existing architecture | Tradeoffs | Founder decision |
|---|---|---|---|
| **A. Push to object storage the Founder already controls** (e.g. the VPS provider's own S3-compatible bucket) | A post-publish step uploads the already-sealed `*.tar.gz` + `.sha256` | Simplest; but needs a bucket + credentials | **Yes** — choose/provision the target + who holds credentials |
| **B. `rsync`/`scp` to a second host the Founder owns** | Reuses SSH already used for deploy | No new platform; needs a second always-on host | **Yes** — provide the host |
| **C. Offline/cold media rotation** (operator copies to external disk) | Manual, zero new infra | Highest RPO; human-dependent | **Yes** — accept the manual cadence |

**Recommendation:** B or A, but **the target and credentials are a Founder decision** (no paid
provisioning or credential creation done here).

### 2. Encryption & key separation
Archives are plaintext today (secrets are *excluded* by policy, but Library originals are included).
Any off-host copy must be encrypted **with keys that do not live on the VPS**.

| Option | Reuse | Tradeoffs | Founder decision |
|---|---|---|---|
| **`age` (or `gpg`) encrypt the archive before upload** | A one-line filter on the sealed archive; recipients' public keys only on the VPS, **private keys held by the Founder off-host** | Strong key separation; key loss = backup loss (needs a key-custody plan) | **Yes** — generate/custody the keypair; decide recovery-key escrow |
| Server-side encryption at the object store | Offloads crypto to the store | Keys may sit with the provider — weaker separation | **Yes** |

**Key-separation principle (for review):** the VPS should hold only the **public** key; decryption keys never on the protected host. This also satisfies the §A "hardware-bound credentials must not be copied as archive data" constraint from the Founder requirements delta.

### 3. Automated backup scheduling (bounds RPO)
`backup.sh` is manual today → unbounded RPO.

| Option | Reuse | Tradeoffs |
|---|---|---|
| **systemd timer** on the VPS (e.g. hourly/daily) calling `backup.sh` | Native to the Ubuntu VPS; journald logging | Needs a unit file + enablement (a VPS change — not done here) |
| cron entry | Simplest | Less observable than systemd |

**Founder decision:** the cadence (which sets the RPO target). No scheduler is installed here (that is a production change).

### 4. Independent restore verification (periodic drill)
No automated proof that backups actually restore.

- **Option:** a scheduled **restore-drill** that pulls the latest off-host archive into a disposable target and runs `restore.sh` verification, reusing the exact pattern already proven in `.github/workflows/backup-integrity-verification.yml` (Scenario 9 / ENOSPC recovery). Alert on failure.
- Tradeoff: needs the off-host target (depends on #1) and a runner/host to execute it.

### 5. Immutable / protected backup retention
Archives are 0600 files deletable by the deploy user; no WORM.

| Option | Reuse | Tradeoffs | Founder decision |
|---|---|---|---|
| **Object-lock / WORM** on the off-host bucket | Rides on option #1A | Ransomware/compromise can't delete within the lock window | **Yes** — enable object-lock + retention window |
| Append-only offsite with a separate credential | Rides on #1B | Simpler; weaker than true WORM | **Yes** |

### 6. Measured RPO / RTO
Both are currently undefined.

- **RPO** is set by the schedule (#3): e.g. a daily timer ⇒ RPO ≤ 24 h. **Founder sets the target.**
- **RTO**: today restore is a manual drill (`docs/VPS_BACKUP_RESTORE.md`). Measure it by timing the restore-drill (#4) end to end; publish the number and iterate. **Founder sets the acceptable RTO.**

### 7. Complete host-loss recovery
Needs: (a) off-host encrypted archive (#1/#2), (b) a documented rebuild runbook (re-provision VPS → re-provision secrets from the secret store [names are in the backup manifest already] → restore volumes via `restore.sh` → bring up `docker-compose.vps.yml`), and (c) Postgres is **Supabase-hosted** (their DR — confirm the Supabase backup/retention policy separately). A rehearsed, timed host-loss drill is the only real proof; it depends on #1 and #4.

## Flagged for independent review — do NOT silently change

**Missing-checksum restore behavior.** `restore.sh` currently **warns and proceeds** when no
`.sha256` sidecar is present (`restore.sh:86–88`: *"proceeding without integrity verification"*).
With #366, every backup.sh-produced archive now always has a verified sidecar, so a missing sidecar
implies a hand-placed or legacy archive. **Question for review:** should restore instead **refuse**
a missing checksum unless an explicit `--i-understand-unverified` flag is passed (fail-closed), or
keep the current warn-and-proceed (fail-open with a loud warning)? This is a **trust-policy change**
and is deliberately **not made here** — it is raised for the Founder/independent reviewer to decide.

## Decisions requiring Founder authorization (summary)
1. Off-host backup **target** + who provisions/holds credentials (#1).
2. Encryption scheme + **key custody/escrow** (#2).
3. Backup **cadence** (sets RPO) (#3).
4. **Object-lock/WORM** retention window (#5).
5. Acceptable **RTO** target (#6).
6. The missing-checksum restore **trust policy** (fail-closed vs fail-open) — independent review.

## Boundaries
No paid provisioning, no credential creation, no scheduler/encryption/upload installed, no change
to #366 or to `restore.sh`. Each item above becomes its own isolated, independently-reviewed branch
**after** the corresponding Founder decision. `first_cycle_go=false`.

*Builder (Claude Code). Documentation-only; review-pending.*
