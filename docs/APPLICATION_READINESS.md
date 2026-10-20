# Application-readiness (website + MainAI Founder interface)

Isolated successor of frozen `#291` @ `0b1871bf63e4c9b6d0d32633af097b281e644679`.
Frozen itself @ `5e3cd01ac3a2cded69ef19bfa074d15196d3afaa`. Do not amend this package;
the Founder-workflow successor is `cursor/founder-workflow-readiness-91c1`.
Does **not** compose Codex transport/grants, enable Recall, merge, or claim first-cycle GO.

Machine-readable flags: `GET /api/mainai/founder-ops/snapshot` (`first_cycle_go: false`).

## What this workstream closed

- Founder HTTP for STOP, startup readiness, live spend, and canonical work status
  (`queued` / `running` / `waiting_on_dependency` / `failed` / `completed` /
  `independently_verified`).
- Dashboard no longer claims the system is operational when APIs fail, and no longer
  hard-codes “Konversationer idag” to `0`.
- Chat history keeps durable failed assistant turns (`status=failed`) and marks
  unconfirmed sends after a transport failure.
- Duplicate live workforce assignment for the same MainAI task, and a second paid
  agent-orchestration dispatch, are refused.
- Library ops bar reports a fetch failure instead of going silent.
- AuthGuard does not treat a backend outage as logout.

## Safe local startup (privileged remote effects and Recall off)

From the repository root. Do not set provider API keys, `GITHUB_TOKEN`, or
`PERSONAL_RECALL_SYSTEM_KEK_B64` for this profile.

```sh
cp .env.example .env
# Required: SECRET_KEY, FOUNDER_EMAIL, FOUNDER_PASSWORD, MAINAI_APP_PASSWORD
# Keep unset: OPENAI_API_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN, PERSONAL_RECALL_SYSTEM_KEK_B64
# Defaults already false: GITHUB_WRITE_ENABLED, MAINAI_ACTIVATION_MODE,
# MAINAI_LEGACY_PREPARE_PR_WRITES_ENABLED. Recall HTTP is not mounted in app.main.

docker compose up -d postgres redis qdrant
docker compose run --rm backend alembic upgrade head
docker compose up -d backend frontend
```

Without Docker, after creating the `lifeos` database and `mainai_app` role
(see `docs/OPERATIONS.md`):

```sh
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://lifeos@localhost/lifeos
export APP_DATABASE_URL=postgresql://mainai_app:<password>@localhost/lifeos
export GITHUB_WRITE_ENABLED=false
export MAINAI_ACTIVATION_MODE=false
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000

# other terminal
cd frontend
npm ci
INTERNAL_API_URL=http://localhost:8000 npm run dev
```

Verify:

```sh
curl -sS http://localhost:8000/api/health
# Founder cookie session, then:
# GET /api/mainai/founder-ops/snapshot
# Expect: first_cycle_go=false, safe_launch.github_write_enabled=false,
#         safe_launch.recall_http_mounted=false
```

Open `http://localhost:3000`, log in as `FOUNDER_EMAIL`, and open **Founder-status**.

## Remaining launch blockers

These stay visible FAIL/BLOCKED. This PR does not close them.

- Codex Git transport / worktree credentials: CRED-1/2/3, E2E-3
- Production grant interface: GRANT-PROD
- Production publication history/symlink/gitlink: TA-12/TA-13/PUB-2
- Production `evaluate_check_runs` skipped-as-pass: CI-6
- `#280` RETURN_TO_BUILDER dual `0089`
- First-cycle GO remains **false** until those compose with a PASS-reviewed
  activation-security replacement
- Chat/workbench can still invoke paid providers if API keys are set in the
  environment — leave keys empty for a safe test
- No Next.js middleware: shell HTML is client-guarded; APIs remain `require_founder`
- Independent exam of this PR is still required before treating it as launch-ready
