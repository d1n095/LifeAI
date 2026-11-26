# MainAI – integrationsöverlämning till Codex

Uppdaterad 2026-11-26. Det här dokumentet är skrivet för att kunna läsas utan någon tidigare
kontext: vad som ska integreras, i vilken ordning, vilka beroenden som finns, hur det
verifieras och vad som fortfarande är ogjort.

## 1. Kandidaten

| | |
| --- | --- |
| Arbetsgren | `freebuff/mainai-context-atlas-decision-support-20261125` |
| Bas | `8c7b335e` |
| Kod- och testinnehåll | `a64b4a23` (`a8644b2a` plus den svit-säkrade assertionen) |
| Dokumentation ovanpå | `0e19e7a5`, `2b659294` och detta dokument |
| Antal commits över basen | 37 före detta dokument (38 med det) |
| Upstream | ingen |
| Remote-ref som innehåller HEAD | ingen (`git branch -r --contains HEAD` tom) |
| Arbetsyta | ren |
| Push / merge / deploy | inte gjort |

Kandidaten är alltså branchens tips, inte en enskild commit. `a64b4a23` är den sista commit
som ändrar kod eller tester; allt efter den är dokumentation.

## 2. Vad som ska integreras

**Integrera hela branchen som en kandidat.** Cherry-picka inte bara de sista commitsen:
`backend/app/routers/chat.py` importerar moduler som infördes tidigare på samma branch och som
saknas på båda integrationsgrenarna.

| Gren | Tip | Merge-base mot kandidaten | Saknar |
| --- | --- | --- | --- |
| `origin/integration/founder-alpha-20261121` | `0adb108f` | `dd69cb30` | `context_atlas/`, `providers/circuit_breaker.py`, `founder_memory/prompt.py`, `mainai_executive/reuse_gate.py` |
| `origin/claude/founder-alpha-pre-vps-consolidation` | `b313c9df` | `86671468` | samma moduler |

`chat.py` skiljer sig med 1017 respektive 843 rader mot kandidaten, så en konflikt i den filen
är väntad. Den ska lösas en gång mot den samlade kandidaten – inte genom att kopiera enskilda
funktioner från någon av grenarna.

Om en snävare port ändå krävs är den tekniska beroendeordningen:

| Ordning | Commit | Fil(er) | Varför |
| --- | --- | --- | --- |
| 1 | `6708c469` | `backend/app/providers/circuit_breaker.py`, `registry.py` | `registry.py` importerar `circuit_breaker` |
| 2 | `587156ea` | `backend/app/founder_memory/prompt.py` | `chat.py` importerar `format_founder_memory_instructions` |
| 3 | `76181071`, `3670d28a` | `backend/app/context_atlas/*`, `mainai_executive/reuse_gate.py`, `missing_piece.py` | reuse-blocket i chatten kräver atlas-rekommendationen |
| 4 | `624269e8` | `backend/app/work_management/service.py` | dubblettskydd för identiska öppna uppgifter |
| 5 | `6145a764` | `backend/app/providers/ollama_provider.py` | retry efter stoppad lokal modell |
| 6 | `90be8867`, `ee67cbe8` | `backend/tests/backend/chat/*` | felmatris och in-process-acceptans |
| 7 | `a8644b2a`, `a64b4a23` | `backend/tests/backend/chat/test_project_session_os_restart.py` | OS-processacceptans; endast test |

## 3. Migrationsberoenden

**Inga.**

```bash
git diff 8c7b335e..HEAD -- backend/alembic | wc -l   # 0
```

Alembic-head är oförändrad. Flödet använder befintliga tabeller: `projects`, `conversations`,
`messages`, `chat_request_claims`, projektminne/checkpoints samt
`knowledge_import_jobs` (worker-lease). Idempotens bygger på `client_message_id` plus
partiellt unikt index på `in_reply_to_id`; historiken läses i total ordning
`(created_at, id)`.

## 4. Miljövariabler

**Inga nya miljövariabler.** `backend/app/config.py` är oförändrad
(`git diff 8c7b335e..HEAD -- backend/app/config.py | wc -l` → 0). För lokal modell på staging:

```dotenv
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_LLM_MODEL=qwen2.5:0.5b        # större modell är möjlig på 16 GB
OLLAMA_BASE_URL=http://ollama:11434
CHAT_FALLBACK_ORDER=                  # tom = ingen betald fallback under acceptansen
```

`OLLAMA_BASE_URL` och `CHAT_FALLBACK_ORDER` finns i `config.py` men saknas i
`.env.vps.example`; lägg dem i staging-miljön, inte i någon annan agents fil. En aktiv
`provider_config`-rad i databasen vinner över miljövariablerna – kontrollera
`GET /api/admin/providers/status` innan en env-only ändring antas ha effekt.

**Embeddings är en separat roll.** `EMBEDDING_DIM=1536` är låst av pgvector-kolumnen. Byt inte
till en 768-dimensionell Ollama-embedding utan egen migration och om-embedding av alla
chunkar. Båda acceptanstesterna stubbat därför embedding-anropet; chattmodellen är riktig.

## 5. Testkommandon och förväntat utfall

```bash
cd backend
export DATABASE_URL="postgresql://postgres@127.0.0.1:55432/lifeos_test_p1"
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_p1"
export REDIS_URL="redis://127.0.0.1:56379/0"
export LIFEAI_TEST_DATABASE_NAME="lifeos_test_p1"

# 1. Verklig OS-processomstart: uvicorn- och worker-barnprocesser dödas med SIGKILL
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/chat/test_project_session_os_restart.py -q
#    förväntat: 1 passed (43–75 s beroende på modellens svarstid)

# 2. In-process-acceptans med riktig Ollama
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/chat/test_project_session_real_model.py -q
#    förväntat: 1 passed (~41 s)

# 3. Sju fel-/återhämtningsscenarier
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/chat/test_chat_failure_recovery.py -q
#    förväntat: 7 passed

# 4. Providerkontraktet efter retry-fixen
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/providers/test_ollama_provider_failclosed.py -q
#    förväntat: 19 passed

# 5. Hela chat- + provider-sviterna
/tmp/r15-venv/bin/python -m pytest tests/backend/chat tests/backend/providers -q
#    förväntat: 358 passed (chat 120 + providers 238)

/tmp/r15-venv/bin/python -m ruff check \
  app/providers/ollama_provider.py tests/backend/chat tests/backend/providers
```

Test 1–2 kräver `qwen2.5:0.5b` på `http://127.0.0.1:11434`. Om modellen saknas **skippar**
modulerna med ett miljöskäl – ett skip är inte PASS.

Test 1 startar riktiga barnprocesser och behöver därför: en ledig TCP-port på `127.0.0.1`, att
testdatabasen är migrerad till head (görs av `tests/conftest.py`), Redis enligt `REDIS_URL`,
och att ingen annan session håller en öppen transaktion mot testdatabasen (startens
`apply_rls()` tar tabellås). Testet sätter `COOKIE_SECURE=false` endast i barnprocessernas
miljö, eftersom httpx – till skillnad från en webbläsare – inte skickar Secure-kakor över
plain http till `127.0.0.1`. Det skriver också nyckeln `worker:heartbeat` i den Redis-databas
`REDIS_URL` pekar på.

## 6. Verifierat läge

| Moment | Resultat | Bevis |
| --- | --- | --- |
| Riktig backend-process dödad med SIGKILL, ny process läser samma konversation | **PASS** | `test_project_session_os_restart.py`: 1 passed, 75 s + 43 s |
| Idempotens över processgräns (samma `client_message_id`, inget nytt modellanrop) | **PASS** | Samma test |
| Fortsatt projektkontext efter omstart | **PASS** | Modellprompten i process B innehåller första frågan och första svaret |
| Ollama otillgänglig i en riktig process: `unreachable`/`retryable`, ingen fallback | **PASS** | Process C mot stängd port; användarraden bevarad, assistant-raden tom |
| Retry i en senare process återanvänder samma assistant-rad | **PASS** | Process D |
| Annan grundare nekas (403) | **PASS** | Samma test |
| Riktig worker-process dödad; ny worker tar över utgången lease | **PASS** | `locked_by = os-restart-worker-b`, framtida `lease_expires_at` |
| In-process-acceptans med riktig Ollama | **PASS** | 1 passed, 41 s |
| Felmatris, 7 scenarier | **PASS** | 7 passed, stabil i fem körningar |
| Hela chat- + provider-sviterna | **PASS** | 358 passed: chat 120 (5 m 01 s) + providers 238 (4 m 29 s); ruff rent |
| CI #511 (`29d09a78`, run `45562008534`) | **NOT PASS** | `in_progress` vid kontroll 2026-11-26 (startad 08:14) |
| CI #507–#509 | **NOT PASS** | `cancelled`; bevisar ingenting |
| CI #510 | **NOT RUN** | Run-id `45562002525` svarar 404 |
| 16 GB-VPS med produktionsmodell | **NOT RUN** | Servern finns inte i den här miljön |
| Webbläsare/frontend | **NOT RUN** | Chrome saknas; frontend ägs av Claude |
| `security/POLICY_LOCK.json` | **BLOCKED** | Divergensen kvarstår; filen är orörd (Z ansvarar) |
| Hela backend-sviten parallellt | **NOT RUN** | 2 GB VPS; endast fokuserade sviter |

## 7. Reproducerbar bundle

```bash
cd /tmp/p1
OUT="$(git rev-parse --git-common-dir)/freebuff-bundles/mainai-codex-<sha>"
bash scripts/ops/export_commit_stack.sh export . 8c7b335e "$OUT" \
  freebuff/mainai-context-atlas-decision-support-20261125
bash scripts/ops/export_commit_stack.sh verify "$OUT/commit-stack.bundle" "$OUT/manifest.json"
```

`export` vägrar en smutsig tracked arbetsyta, skannar de nya commitsen efter
hemlighetsmönster och skriver `commit-stack.bundle`, `manifest.json` och `SHA256SUMS`.
`verify` klonar bundlen i en temporär katalog, kontrollerar varje commit/tree och kör
`git fsck`. Den konverterade bundlen har restriktiva filrättigheter (`umask 077`) och
innehåller endast den egna branchen – inga andra agenters grenar och ingen historikomskrivning.

Hemlighetsskanningen hittar endast testfixturens lösenord (`TestFounderPassword123!`), som
redan finns i `tests/conftest.py` och inte är en produktionshemlighet.

## 8. Gränser för integrationen

- Ingen merge till huvudgrenen, ingen deployment, ingen force-push, ingen historikomskrivning.
- `security/POLICY_LOCK.json` får inte ändras; divergensen ägs av Z.
- Claudes staging-/frontendfiler och Z:s säkerhetskod är orörda i kandidaten och ska förbli det.
- `first_cycle_go=false` kvarstår.
- CI #511 får inte störas; den ska läsas när den är terminal, och `cancelled` är aldrig grönt.
- Ett skip i modultestet är inte PASS; det kräver att `qwen2.5:0.5b` faktiskt finns installerad.

## 9. Kvar att göra innan staging

1. Läs CI #511 när den är terminal och rapportera PASS/FAIL/CANCELLED.
2. Kör `test_project_session_os_restart.py` och `test_project_session_real_model.py` på
   16 GB-VPS:en med den modell som ska användas i staging.
3. Lös `chat.py`-konflikten en gång mot den samlade kandidaten och kör om sviterna i punkt 5.
4. Fastställ publiceringsordning för #507–#511 + denna branch – utan force-push eller merge.
