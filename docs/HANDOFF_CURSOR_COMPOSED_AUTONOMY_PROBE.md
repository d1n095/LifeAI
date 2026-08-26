# HANDOFF — Composed autonomy runtime probe (Cursor)

**SHA:** `ec1bdd03e841`
**PR:** (pending open)
**Base tip:** `27f8562`

## Done

Composed probe proves claim→…→production Supervisor tick is reachable and stops honestly at
`PROVIDER_SPEND_NOT_AUTHORIZED` with spend/remote_write false.

## Next real blocker (Cursor-owned)

`PROVIDER_SPEND_NOT_AUTHORIZED` defer still leaves task/job `running` (B7 only released
`WAITING_PROVIDER`). Process death → recovery contamination risk — same family as B7, but
releasing every tick would spam failed jobs until a durable park/wake is designed.

Do **not** flip `production_entry.provider_spend_authorized` without founder authorization.

```json
{
  "lane": "cursor_autonomy_night",
  "unit": "composed_autonomy_runtime_probe",
  "branch": "cursor/composed-autonomy-runtime-probe",
  "local_result": "1 passed",
  "honest_stop": "PROVIDER_SPEND_NOT_AUTHORIZED",
  "next_blocker": "PROVIDER_SPEND defer leaves running job (park/wake design)",
  "provider_spend_authorized": false
}
```
