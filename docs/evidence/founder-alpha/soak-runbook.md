# F23 — fyra timmars aktiv soak (FORBEREDD, INTE KORD)

**Status:** **BLOCKED**. Ingen fyratimmarskorning har gjorts. En kort smoke ar
inte ekvivalent och far inte rapporteras som F23 PASS.

## Varfor den inte kors har

Den befintliga VPS:en har 1,8 GiB RAM. En fyra timmars aktiv soak med backend,
worker, PostgreSQL, Redis och aterkommande arbetslast ar inte en saker
resursanvandning dar. F23 kraver en separat, resursbegransad miljo dar
produktionsdata och produktionshemligheter inte ar atkomliga.

## Forutsatningar

- Dedikerad host/container med minst 4 GiB RAM och 20 GiB ledigt disk.
- Separat PostgreSQL och Redis, egna databaser/DB-nycklar, ingen produktions-URL.
- `DATABASE_URL` och `APP_DATABASE_URL` pekar pa samma isolerade databas.
- `REDIS_URL` pekar pa en dedikerad Redis-databas.
- `FIRST_CYCLE_GO=false`; inga provider-nycklar och ingen extern betalning.
- Alembic-head ar verifierad och `scripts/security/apply_runtime_privileges.py`
  har korts fore soak.
- En skrivbar `STORAGE_ROOT` utanfor produktionsvagen.

## Preflight

```bash
free -m
df -h
pg_isready -h 127.0.0.1 -p 55432
redis-cli -h 127.0.0.1 -p 56379 ping
python -m alembic current
```

Om minne ar under 1 GiB ledigt, disken under 10 GiB eller PostgreSQL/Redis inte
svarar: avbryt och rapportera **BLOCKED**. Ga inte vidare med resursbrist.

## Korning

Anvand den befintliga langa autonomy-soaken som aktiv arbetslast, i en
resursbegransad miljo, och registrera start/slut, cykler, health, RSS, CPU, DB-
och Redis-status minst var femte minut. Kommandot far inte ersattas av en kort
smoke.

```bash
export PYTHONPATH=.
export DATABASE_URL="postgresql://postgres@127.0.0.1:55432/lifeos_soak_f23"
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_soak_f23"
export REDIS_URL="redis://127.0.0.1:56379/16"
export STORAGE_ROOT="/tmp/lifeai-f23-storage"
export FIRST_CYCLE_GO=false
pytest tests/backend/mainai/test_composed_autonomy_soak_v4_long_autonomy.py -q
```

Repetera den aktiva arbetslasten under fyra timmar med samma isolerade
databas, och skriv lopande health-/resursevidens till en egen fil. En enda
testkornning ar inte fyra timmar.

## Evidens som kraver for F23 PASS

- Start- och sluttidpunkt med minst fyra timmar real tid.
- Antal cykler, misslyckade jobb, dubbeljobb och aterhamtningar.
- Health p50/p95, RSS/CPU, PostgreSQL- och Redis-anslutningar.
- Inga dubbelraknade jobb eller tappade jobb under hela fonstret.
- En tydlig sammanstallning av vad som faktiskt kordes, utan att kalla en kort
  smoke for en soak.

Tills dess ar F23 och F24 **BLOCKED**.
