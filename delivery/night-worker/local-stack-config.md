# Konfiguration som krävdes i den isolerade stacken

Ingen produktkod ändrades för att få nattkörningen att fungera — två konfigurationsfel i den
lokala installationen blockerade den.

## 1. Chatt-rollens primära provider saknades

`start-backend.sh` och `start-worker.sh` satte `DEFAULT_EMBEDDING_*` men inte `DEFAULT_LLM_*`.
Följden:

* `chat_with_fallback()` föll tillbaka genom `CHAT_FALLBACK_ORDER` till Ollama → samtalen
  fungerade.
* `get_capability_status()` frågar bara `resolve_active(db, role="chat")`, som returnerade
  default `openai`/`gpt-4o-mini` utan nyckel → `configured=False`.
* Workern vägrade därför dispatcha: `Capability 'task_execution' is implemented but not
  currently configured to run.` (verifierat i worker-loggen 2026-11-27 07:44).

Ändring (båda skripten):

```bash
export DEFAULT_LLM_PROVIDER=ollama
export DEFAULT_LLM_MODEL=llama3.1
```

Efter omstart: `corpus_review`, `message_sequence_backfill`, `message_source_backfill` och
`task_execution` rapporterar alla `available=True, configured=True`.

Alternativet hade varit att sätta en `provider_config`-rad för rollen `chat` via
`PUT /api/admin/providers/config` (produktens egen runtime-switch). Miljövariablerna valdes för
att de redan är mönstret i den här stacken (`DEFAULT_EMBEDDING_*`) och inte skapar DB-drift.

## 2. `approval_policy` måste vara `autonomous_development_work`

Safe Planner (`app/safe_planner/service.py`, `validate_candidate()`) vägrar planera lokala
skrivningar under `standard_repo_work`:

> autonomous safe planner requires goal.approval_policy == 'autonomous_development_work'

Det är korrekt fail-closed: ett mål skapat för mänsklig granskning får inte ärva den
autonoma vägen. Målet måste därför skapas med:

```json
{"approval_policy": "autonomous_development_work"}
```

Under den policyn kräver `repo_edit` (och `open_pr`) grundarens godkännande per task — vilket
är precis vad "redan godkänd uppgift" innebär i flödet. Workern självgodkänner aldrig: utan
godkännande står ticken kvar i `WAITING_APPROVAL` varannan sekund (verifierat, 46 ticks).

## Miljövariabler som användes vid testkörningen

```bash
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeai_dev
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeai_dev
REDIS_URL=redis://127.0.0.1:56381/0
STORAGE_ROOT=/home/lifeai-security/lifeai-local-stack/storage
ENVIRONMENT=development
LOCAL_ONLY_MODE=true
OLLAMA_BASE_URL=http://127.0.0.1:11434
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_LLM_MODEL=llama3.1
DEFAULT_EMBEDDING_PROVIDER=ollama
DEFAULT_EMBEDDING_MODEL=nomic-embed-text
```

Tester (disposable databas, skapas av `tests/conftest.py`):

```bash
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_local
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_local
REDIS_URL=redis://127.0.0.1:56381/1
ENVIRONMENT=test
```
