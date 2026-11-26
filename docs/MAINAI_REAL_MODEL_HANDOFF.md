# MainAI – verklig modellacceptans och staging-överlämning

Uppdaterad 2026-11-26. Arbetsgren:
`freebuff/mainai-context-atlas-decision-support-20261125`, bas `8c7b335e`. Koden är
`ee67cbe8`; överlämningsdokumentet följer i commits efter koden, så branchens exakta HEAD är
`git rev-parse HEAD`. Ingen push, merge eller deployment har gjorts.

## 1. Beständighet och lokalt arbete

- Branchen innehåller samtliga commits i `git log 8c7b335e..HEAD`; arbetsytan var ren efter
  commit.
- Branchen har **ingen upstream** och ingår inte i någon av PR #507–#511:s heads.
- Reproducerbar bundle (skapas om efter sista commit):

  ```bash
  cd /tmp/p1
  OUT="$(git rev-parse --git-common-dir)/freebuff-bundles/mainai-real-model-<sha>"
  bash scripts/ops/export_commit_stack.sh export . 8c7b335e "$OUT" \
    freebuff/mainai-context-atlas-decision-support-20261125
  bash scripts/ops/export_commit_stack.sh verify "$OUT/commit-stack.bundle" "$OUT/manifest.json"
  ```

  `export` vägrar en smutsig tracked arbetsyta, skannar de nya commitsen efter
  hemlighetsmönster och skriver `commit-stack.bundle`, `manifest.json` och `SHA256SUMS`.
  `verify` klonar bundlen i en temporär katalog, kontrollerar varje commit/tree och kör
  `git fsck`.
- Hemlighetsskanningen i `export` och en manuell genomgång av diffen hittade endast
  testfixturens lösenord (`TestFounderPassword123!`), som redan finns i `tests/conftest.py`
  och inte är en produktionshemlighet.
- Inga filer är untracked eller ignorerade på ett sätt som riskerar att försvinna.

## 2. Vad som verifierades

| Moment | Resultat | Bevis |
| --- | --- | --- |
| Verklig lokal modell genom projektflödet | **PASS** | `test_project_session_real_model.py`: 1 passed, 41 s |
| Backend-omstart med sparad konversation | **PASS** | Samma test: stänger första lifespan, startar ny, historiken och nästa tur finns kvar |
| Worker-omstart | **PASS** | Två riktiga `Worker().run_once()`-cykler i två instanser; konversationen oförändrad |
| Fel och återhämtning, 7 scenarier | **PASS** | `test_chat_failure_recovery.py`: 7 passed, 5 körningar i följd utan flakiness |
| Ollama nere före sändning + retry | **PASS** | Användarraden bevaras, felet är `unreachable`/retryable, retry återanvänder samma rad |
| Avbrott under generering | **PASS** | Deltext visas men lagras aldrig som svar; blind retry och dubblett nekas 409 |
| Fallback förbjuden av policy | **PASS** | `CHAT_FALLBACK_ORDER=""`; extern provider anropas aldrig |
| Projektkontext saknas/otillgänglig | **PASS** | Saknat projekt → 404 före någon rad; kastande kontextblock → turen lyckas utan blocket |
| Annan användare | **PASS** | 403 för konversation, projekt och chatt |
| Providerkontrakt efter fixen | **PASS** | `test_ollama_provider_failclosed.py`: 15 passed |
| Hela chat- + provider-sviterna | **PASS** | 357 passed, 8 m 46 s |
| CI #511 | **NOT PASS** | Körningen `45562008534` är fortfarande `in_progress` |
| CI #507–#509 | **NOT PASS** | `cancelled`; bevisar ingenting |
| CI #510 | **NOT RUN** | Run-id `45562002525` svarar 404; ingen terminal körning kunde verifieras |
| Webbläsare/frontend | **NOT RUN** | Chrome saknas i miljön; frontend ägs av Claude |
| Hela backend-sviten parallellt | **NOT RUN** | 2 GB VPS; endast fokuserade sviter |
| `security/POLICY_LOCK.json` | **BLOCKED** | Divergensen kvarstår; filen är orörd (Z ansvarar) |

Den verkliga modellen var `qwen2.5:0.5b` på `http://127.0.0.1:11434`. Modellens svar är
medvetet inte innehållsligt assertat: testet kräver ett icke-tomt svar med
`provider="ollama"` och rätt modellnamn. Allt annat – projektkontext i prompten, persistens,
omstart, historik, behörigheter – är deterministiska assertioner mot HTTP och Postgres.

## 3. Produktionsfixen i denna omgång

`6145a764` – en anslutning som aldrig etablerades är bevisat pre-egress.

- **Rotorsak:** `OllamaProvider.chat()` lämnade `provider_request_may_have_left` som `None`
  vid `httpx.ConnectError`. Registret gjorde då om felet till `external_effect_unknown`, som
  ligger i `_UNRESOLVED_EFFECT_CATEGORIES` och permanent blockerar retry. En stoppad lokal
  modell låste alltså grundarens eget meddelande trots att ingenting skickats.
- **Minsta fix:** `ConnectError`/`ConnectTimeout` sätter `False` (bevisat ingen förfrågan
  lämnade processen). `ReadTimeout`/`ReadError` och HTTP-statusfel förblir `True`/ambivalenta,
  så ett anrop som kan ha accepterats fortfarande är fail-closed.
- **Regressionstester:** connection refused → retryable; read timeout → inte retryable; samma
  distinktion i streaming.

## 4. Vad Codex behöver integrera

Den nya acceptansen är test- och fixlager; själva projektflödet byggdes i de tidigare
commitsen på samma branch. **Cherry-picka inte bara de tre sista commitsen** – chatten
importerar moduler som infördes tidigare på branchen och som saknas på båda
integrationsgrenarna.

Rekommenderad väg: integrera **hela branchen** (samtliga commits mot `8c7b335e`) som en
kandidat och lös
`backend/app/routers/chat.py` en gång. Om en snävare port krävs är beroendeordningen:

| Ordning | Commit | Fil(er) | Varför |
| --- | --- | --- | --- |
| 1 | `6708c469` | `backend/app/providers/circuit_breaker.py`, `registry.py` | `registry.py` importerar `circuit_breaker`; filen saknas på båda integrationsgrenarna |
| 2 | `587156ea` | `backend/app/founder_memory/prompt.py` | `chat.py` importerar `format_founder_memory_instructions` |
| 3 | `76181071`, `3670d28a` | `backend/app/context_atlas/*`, `backend/app/mainai_executive/reuse_gate.py`, `missing_piece.py` | reuse-blocket i chatten kräver atlas-rekommendationen |
| 4 | `624269e8` | `backend/app/work_management/service.py` | dubblettskydd för identiska öppna uppgifter |
| 5 | `6145a764` | `backend/app/providers/ollama_provider.py` | retry efter stoppad lokal modell |
| 6 | `90be8867`, `ee67cbe8` | `backend/tests/backend/chat/*` | acceptans- och felmatris |

Övriga commits på branchen (`aca37661`, `1bc5e318`, `089de13c`, dokumentation och
atlas-/system intelligence-arbete) är inte importkritiska för det nya chattflödet men ingår i
kandidaten och bör följa med för att undvika en halv branch.

### Migrationsberoenden

**Inga.** `git diff 8c7b335e..HEAD -- backend/alembic` är tom; alembic-head är oförändrad.
Staging behöver alltså ingen ny migration för detta flöde.

### Provider- och miljöinställningar

Inga nya miljövariabler infördes. För lokal modell på staging:

```dotenv
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_LLM_MODEL=qwen2.5:0.5b        # större modell är möjlig på 16 GB
OLLAMA_BASE_URL=http://ollama:11434
CHAT_FALLBACK_ORDER=                  # tom = ingen betald fallback under acceptansen
```

`OLLAMA_BASE_URL` och `CHAT_FALLBACK_ORDER` finns i `backend/app/config.py` men saknas i
`.env.vps.example`; lägg till dem i staging-miljön, inte i någon annan agents fil.

**Embeddings är en separat roll.** `EMBEDDING_DIM=1536` är låst av pgvector-kolumnen. Byt
inte till en 768-dimensionell Ollama-embedding (t.ex. `nomic-embed-text`) utan en egen
migration och om-embedding av alla chunkar. Acceptansen fejkar därför embedding-anropet: bara
chattmodellen är installerad här. På staging ska embeddings fortsätta på en
1536-dimensionell provider (OpenAI enligt mallen) tills en sådan migration finns.

En aktiv `provider_config`-rad i databasen vinner över miljövariablerna. Kontrollera
`GET /api/admin/providers/status` innan en env-only ändring antas ha effekt.

### Projektminne och konversationshistorik

Inga schemaändringar. Flödet använder befintliga tabeller (`projects`, `conversations`,
`messages`, `chat_request_claims`, projektminne/checkpoints). Idempotens bygger på
`client_message_id` + partiellt unikt index på `in_reply_to_id`; historiken läses i total
ordning `(created_at, id)`.

## 5. Jämförelse med integrationsgrenarna

| Gren | Tip | Merge-base mot HEAD | Unika commits (gren / HEAD) | Saknar |
| --- | --- | --- | --- | --- |
| `origin/integration/founder-alpha-20261121` | `0adb108f` | `dd69cb30` | 32 / 164 | `context_atlas/`, `providers/circuit_breaker.py`, `founder_memory/prompt.py`, `mainai_executive/reuse_gate.py` |
| `origin/claude/founder-alpha-pre-vps-consolidation` | `b313c9df` | `86671468` | 74 / 92 | samma moduler; `registry.py` importerar inte `circuit_breaker` |

Båda grenarna är alltså äldre kandidater med egna 32 respektive 74 commits. `chat.py` skiljer
sig med 1017 respektive 843 rader mot HEAD, så en konflikt i den filen är väntad och ska lösas
en gång mot den samlade kandidaten – inte genom att kopiera enskilda funktioner.

Claudes staging-filer och Z:s säkerhetskod är orörda. `security/POLICY_LOCK.json` är inte
ändrad och `first_cycle_go=false` kvarstår.

## 6. Reproducerbara verifieringskommandon

```bash
cd /tmp/p1/backend
export DATABASE_URL="postgresql://postgres@127.0.0.1:55432/lifeos_test_p1"
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_p1"
export REDIS_URL="redis://127.0.0.1:56379/0"
export LIFEAI_TEST_DATABASE_NAME="lifeos_test_p1"

# Verklig modell + backend- och worker-omstart
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/chat/test_project_session_real_model.py -q

# Sju fel-/återhämtningsscenarier
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/chat/test_chat_failure_recovery.py -q

# Providerkontraktet
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/providers/test_ollama_provider_failclosed.py -q

# Hela chat- + provider-sviterna (357 tester)
/tmp/r15-venv/bin/python -m pytest tests/backend/chat tests/backend/providers -q

/tmp/r15-venv/bin/python -m ruff check \
  app/providers/ollama_provider.py tests/backend/chat tests/backend/providers
```

Modultestet hoppar med ett miljöskäl om `qwen2.5:0.5b` inte finns på
`http://127.0.0.1:11434`; ett skip är inte PASS.

## 7. Kvarvarande grindar

1. **CI:** #511 (`29d09a78`) var `in_progress` vid sista kontrollen. #507–#509 är
   `cancelled`, #510:s run-id svarar 404. Ingen av dem är grönt bevis. Ingen push har gjorts,
   så inga körningar har avbrutits och inga PR-heads ändrats.
2. **Staging på 16 GB:** kör om real-model-acceptansen med den modell som ska användas där.
   2 GB-värden klarar `qwen2.5:0.5b` men är inte representativ för produktionsmodellen.
3. **`security/POLICY_LOCK.json`:** divergensen är en blockerare som ägs av Z.
4. **Frontend/webbläsare:** inte verifierad i den här miljön (ingen Chrome; frontend ägs av
   Claude). Backend-kontraktet är verifierat, UI-flödet är det inte.
