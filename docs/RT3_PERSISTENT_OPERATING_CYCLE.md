# RT3 persistent operating cycle and provider ingress

This candidate adds a production caller to the existing worker without activating it.
`AUTHORIZED_CYCLE_WORKER_ENABLED` defaults to `false`; silence, a provider message, or the
presence of a credential never creates authority.

The durable path is:

1. An authenticated Founder creates a cycle for an already-running goal and the exact
   current execution envelope.
2. The existing worker claims one cycle with `FOR UPDATE SKIP LOCKED`, a lease expiry and a
   monotonically increasing fencing generation.
3. The existing Delegation Broker selects a capability-matching workforce profile and mints
   its normal bounded assignment. The cycle does not grant additional capabilities.
4. A separately authorized provider dispatch is bound by the Founder. This component does
   not make paid provider calls and cannot infer dispatch from absence.
5. Cursor v0 status events enter through `POST /api/provider-events/cursor/{owner_id}`. The
   server verifies the HMAC over the exact raw body, exact provider User-Agent, timestamp
   window, immutable delivery identity, owner/cycle/assignment binding, current envelope,
   assignment lifetime and STOP epoch before recording an append-only receipt.
6. `FINISHED` is stored as an untrusted result and moves the cycle only to
   `awaiting_verification`. It never completes the task.
7. A later worker tick queries the existing independent verification registry for the exact
   workforce assignment and exact candidate SHA. Only that trusted PASS completes the task
   and exposes the next dependency-eligible task.

Crash before a transaction commit changes nothing. Crash after commit resumes from the
durable state. A stale worker generation cannot write, duplicate webhook deliveries are
idempotent, changed bytes under the same delivery ID are rejected, and STOP/envelope/epoch
changes fail closed. Provider terminal failure is terminal rather than retried after an
unknown external effect. Bounded retry applies only to pre-dispatch delegation failures.

This does not implement or authorize provider creation, spending, GitHub mutation, merge,
deployment, Recall, or unattended activation. `first_cycle_go` remains false.

The isolated branch has one Alembic head, `0100_rt3_cycle_webhooks`, directly after its
required #314 base head `0089_activation_branch_protect`. The non-colliding revision number
is intentional. A future composition that already contains migrations 0090-0099 must rebase
only this migration's `down_revision` onto the independently approved composed head; it must
not create a second Alembic head or silently combine authority schemas.

On the VPS topology the backend alone receives `CURSOR_CLOUD_WEBHOOK_SECRET`; the worker
explicitly overrides it to an empty value. The public Next.js proxy preserves the raw streamed
body and signature headers. The legacy single-container Render topology has no durable worker
process and therefore cannot run RT3 until its topology is changed; enabling the flag there
does not create a scheduler that does not exist.
