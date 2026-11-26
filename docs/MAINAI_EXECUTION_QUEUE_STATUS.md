# MainAI – exekveringskö, checkpoint

Uppdaterad 2026-11-25. Arbetsgren:
`freebuff/mainai-context-atlas-decision-support-20261125`, bas `8c7b335e`.

## Steg 0 – skydda befintlig kod: PASS

- HEAD vid kontrollen: `45fa468c`; arbetsytan var ren (`git status --porcelain` tom).
- Branchen har **ingen upstream** och ingen remote-ref innehåller commitsen
  (`git branch -r --contains` tom) — det finns alltså ingen PR-head att rubba.
- Hela historiken är säkrad som bundle:
  `LifeAI/.git/freebuff-bundles/mainai-context-atlas-45fa468c.bundle` (8,5 MB).
- Ingen push, ingen merge, ingen force-push, ingen POLICY_LOCK-ändring.

## Steg 1 – G1 fault injection: PASS (`6708c469`)

Verkliga brister som rättades:

1. `circuit_breaker.is_open()` är fail-open och returnerar `False` både för "stängd" och
   "Redis nere". Ny read-only `circuit_breaker.state()` ger `open`/`closed`/`unknown`;
   `is_open()` är oförändrad för chattvägen.
2. `chat_route_truth` kraschade om `latest_check` kastade. Nu fångas felet, status förblir
   okänd och endast undantagsklassens namn rapporteras — aldrig `str(exc)`, som kan bära
   en DSN med lösenord.
3. En gammal eller malformad hälsorad räknades som bevis. Nu krävs rätt resultattyp och en
   tidsstämpel; färskhet mäts mot `provider_verification_cache_seconds` och en föråldrad rad
   räknas inte som aktuell.
4. `usable_now` är `True` endast med en positiv probe. Konfigurerad + stängd krets är en
   förutsättning, inte ett friskhetstecken; utan probe är svaret `None`.

Tester: `tests/backend/mainai/test_capability_truth.py` (17) + `tests/backend/providers/`
(236) = **253 passed**, ruff rent. Testerna täcker Redis nere, Redis-timeout, öppen krets,
stängd krets utan probe, kastande hälsoläsning med DSN-läcka, saknad rad, föråldrad rad,
färsk rad, malformad payload, okonfigurerad provider med probe och policy-nekad identitet.

## Steg 2 – D1 tool capability truth: PASS

Byggt på befintliga auktoriteter, ingen ny säkerhetsmotor:

- `CAPABILITY_MANIFEST` + `get_capability_status()` → `implemented`/`configured`/`available`.
- `require_founder()` (samma grind som de skyddade rutterna) → `permitted`.
- `CapabilityRecord` → `verified`; `verified_available` utan `last_verified_at` rapporteras
  som **inte verifierad**, och `unknown` förblir `None`.

Kod: `app/mainai_executive/capability_truth.py` (`ToolCapability`, `tool_capability_view`),
`SystemIntelligence.tool_capabilities(db, user=...)` i `app/context_atlas/runtime.py`.
Tester: `tests/backend/mainai/test_tool_capability_truth.py` (7) + capability-sviten (17) =
**24 passed**, ruff rent. Täcker icke-grundare, saknad identitet, saknad konfiguration,
kastande konfigurationsläsning, verifieringspåstående utan tidsstämpel och okänd status.

## Steg 3 – D2 provider routing: redan korrekt, dokumenterat

Granskad kod och befintliga tester; inga ändringar gjorda eftersom inga faktiska brister
hittades:

| Egenskap | Kod | Test |
| --- | --- | --- |
| Provider resolution / capability matching | `registry.select_model`, `evaluate_model` | `providers/test_model_capability_routing.py` |
| Fallback-ordning, ingen ny extern provider via policy | `registry.resolve_chat_chain` | `test_model_capability_routing.py::test_fallback_policy_cannot_add_external_provider` |
| Egress-nekande avbryter hela kedjan | `chat_with_fallback`/`stream_chat_with_fallback` (`except EgressDeniedError: raise`) | `security_r15_egress/test_egress_owner_authorization.py`, `test_openai_consult_mainai.py::test_egress_denied_never_reaches_openai` |
| Osäker effekt → ingen retry/fallback | `effect_guard` + `external_effect_unknown` | `providers/test_r15_integration_security.py::test_uncertain_chat_outcome_is_durable_and_retry_is_blocked` |
| Budget/okänt pris blockeras före provider | `billing` + `select_model` | `test_r15_integration_security.py::test_external_unknown_price_blocks_before_provider_when_enforcement_on` |
| Felsanering | `verification.classify_provider_exception` | `test_provider_verification.py`, `test_gemini_provider.py` |
| Krets | `circuit_breaker` + G1 tri-state | `providers/`-sviten |

## Steg 4 – E1 dokument och RAG: PASS med tillägg (`45fa468c`)

Luckan var att ingen test körde hela produktvägen. `tests/backend/chat/
test_upload_to_grounded_answer.py` gör nu: HTTP-upload → durabel `ImportJob(pending)` →
riktig `claim_next_job` → `process_claimed_job` → chunk med aktiv embedding-space-stämpel →
chattfråga med grundat svar, korrekt källa och dokumenttexten i prompten, samt att en annan
ägare inte kan ranka chunket. **2 passed**; hela chat-sviten **108 passed**.

Övriga E1-egenskaper är redan täckta: raderad källa (`test_chat_source_grounding.py:195`),
motsägelser (`:225`), chunking/embedding-space (`rag/test_chunking.py`,
`rag/test_embedding_space.py`), importens fail-closed-lägen
(`rag/test_library_import.py`, `rag/test_library_import_truth_status_fail_closed.py`),
väntande jobb (`test_documents_upload_durable_delivery.py`).

## Steg 5 – E2 evidence-aware svar: redan korrekt, dokumenterat

- Citat byggs från retrieval-rader, inte modelltext:
  `tests/backend/test_adversarial_reasoning.py::test_model_fabricated_citation_never_enters_the_response_sources`.
- Epistemiska kategorier (verified/hypothesis/assumption/conflicting/unknown):
  `rag/test_epistemics.py`, `rag/test_trust_engine.py`, `test_epistemics_integration.py`.
- Osäkerhet bevaras, aldrig uppgraderad: `test_conversation_acceptance_r12.py` (S6),
  `test_cognitive_gate.py`, `test_cognitive_ablation_r11.py`.

## Steg 6 – System Intelligence: redan implementerat

`SystemIntelligence` (Context Atlas) används av `investigation`, `missing_piece`,
`project_status` och `capability_truth`; evidens/ägarskap kommer från befintliga moduler.
Riktade tester: `mainai/test_evidence_intelligence_integration.py`,
`mainai/test_mainai_executive_ops_lanes.py`, `mainai/test_capability_truth.py`.
Ingen parallell recommendation engine har skapats.

## Steg 7 – Persistence och recovery: redan implementerat

Checkpoints: `mainai_execution/checkpoint.py`, `recovery_inspector.py`,
`workforce/failure.py` (`latest_checkpoint`), `project_memory.get_latest_checkpoint`.
Meddelandeidempotens utan dubbletter: `chat/test_chat_message_persistence.py` (D/F).
Projektminne/checkpoint-färskhet: `test_project_memory.py`, `context_atlas/
development_state.py`. Inga nya lager tillagda.

## Steg 8 – CI och publicering: BLOCKERAT

- Fyra körningar för #507–#511 var fortfarande `in_progress`; två körningar för
  `evidence-intelligence-integration` och `system-intelligence-runtime` är **`cancelled`**
  och bevisar alltså ingenting.
- Ingen push har gjorts, eftersom den skulle avbryta aktiva körningar och ändra PR-heads.
- Publiceringsordning får bestämmas först när körningarna är terminala; historiken skrivs
  inte om och `a178e11c` bevaras.

## Implementation run 2026-11-26 (etapp 1–6)

### Etapp 1 – läget: PASS

- Arbetsgren `freebuff/mainai-context-atlas-decision-support-20261125`, 25 lokala commits mot
  `8c7b335e`, ingen upstream, arbetsyta ren. Bundle från steg 0 finns kvar.
- CI #507–#511: fyra körningar `in_progress`, fyra `cancelled`. Ingenting har pushats, så inga
  körningar har avbrutits och inga PR-heads ändrats.

### Etapp 2 – konversationsflödet: PASS (befintlig kod, inga nya luckor)

Verifierat: autentiserad identitet (`require_founder`), durabel användarrad före provider,
 total ordning (`created_at`, `id`), context retrieval, routing/fallback, streaming med
avbrottshantering, retry utan dubbletter (claim + partiellt unikt index), sanerad felkontrakt,
återhämtning efter processdöd (inflight-claim blockerar blind retry) och RLS.
Bevis: 55 passed i `test_project_session_resume.py`, `test_messages_rls.py`,
`test_chat_request_idempotency.py`, `test_message_sequence.py`; tidigare 108 passed i hela
chat-sviten.

### Etapp 3 – projektminne och arbetskontinuitet: PASS + en riktig fix

Chatten injicerar projektkontext (mål, milstolpar, beslut, öppna uppgifter, källor),
projektstatus, grundarens minne och continuity-checkpoints. Checkpoint-staleness mäts mot ny
note, ny branch/PR-status och ny git-commit.

Fix (`624269e8`): `create_task` återanvänder en identisk öppen uppgift (samma ägare, projekt,
milstolpe, normaliserad titel och beskrivning) i stället för att skapa en andra rad. Testet
skickar samma uppgift två gånger och får samma id.

Bevis: 67 passed inkl. `test_project_memory.py` (checkpoint/staleness) och 55 inkl.
återupptagningsscenariot.

### Etapp 4 – systemförståelse i verklig användning: PASS (`3670d28a`)

Utredningsmotorn och atlas-rekommendationen fanns men anropades aldrig från en levande väg.
Nu: `app/mainai_executive/reuse_gate.py` känner igen bygg-intent (svenska/engelska), kör
`investigate_engineering_request` läs-only och returnerar moduler, explicita evidensnivåer,
verkliga käll-/testreferenser, tidigare projektbeslut, ägarskap och rekommendation.
Chatten får blocket i prompten och svaret får `reuse_check` med `execution_authorized=False`.
Svenska synonymer (`minne`, `dokument`, `chatt`, `projekt`, `sökning`) mappar till samma
granskade moduler som engelska. Bevis: 7 passed (gate + riktig HTTP-väg).

### Etapp 5 – felinjektion: PASS

| Felväg | Bevis |
| --- | --- |
| Redis otillgänglig | `test_v5_fault_injection.py::test_03`, G1-sviten |
| PostgreSQL tillfälligt otillgänglig | `test_04` |
| Modellleverantör timeout | `test_05`; `test_chat_message_persistence.py` |
| Fallback nekad av policy | `providers/test_routing_security_hardening.py` |
| Dokument utan originalfil | `rag/test_library_import.py` (blob kan inte återställas / återskapas) |
| Gamla checkpoints | `test_project_memory.py` (stale after note/PR/git) |
| Felaktigt användar-ID | `test_messages_rls.py`; atlas-ägarskap |
| Gammal agentlease | `test_operator_lease_effect_time_race.py`; `test_09` |
| Motsägande evidens | `test_trust_engine.py`; `test_adversarial_reasoning.py` |
| Misslyckad återhämtning | `test_mainai_execution_auto_recovery.py`; `test_01/02` |

Körningar: 67 passed (V5 + projektminne + lease + recovery) och 81 passed, 1 skipped
(biblioteksimport + routing-säkerhet + streaming).

### Etapp 6 – slutlig integrationsverifiering: PASS

`tests/backend/chat/test_project_session_resume.py` går genom riktig HTTP och riktig Postgres:
inloggning → projekt → uppgift → chatt med projektkontext i prompten → omladdning → ny session
som fortsätter historiken utan dubblett → andra konto nekas av founder-gaten. Endast
modelltransporten är fake; retrieval är isolerad till tomt resultat.

### NOT_RUN

- Live extern modell och webbläsar-UI (ingen Chrome i miljön; frontend ägs av Claude).
- Hela backend-sviten parallellt (2 GB VPS — endast fokuserade sviter).
- CI som grönt bevis: körningarna är `cancelled`/`in_progress`, inte PASS.
- `security/POLICY_LOCK.json`-divergensen kvarstår (Z).

## Kvar att göra

1. Steg 8 när CI är terminal: verifiera varje körning, säkra publiceringsordning, ingen merge.
2. Om en verklig E1-lucka dyker upp (t.ex. saknad originalfil vid omindexering) ska den
   hanteras i egen commit med eget test — nuvarande sviter täcker de listade fallen.

## Verklig modellacceptans och överlämning (2026-11-26)

Arbetsgren `freebuff/mainai-context-atlas-decision-support-20261125`, bas `8c7b335e`.
Koden är `ee67cbe8`; branchens exakta HEAD är `git rev-parse HEAD`. Ingen upstream. Full
överlämning:
`docs/MAINAI_REAL_MODEL_HANDOFF.md`.

### DEL 1 – bevara arbetet: PASS

- Arbetsytan ren; samtliga commits mot basen finns på branchen; ingen upstream och ingen
  PR-head.
- Bundle exporterad och verifierad i en isolerad klon med
  `scripts/ops/export_commit_stack.sh` (bundle + manifest + SHA256SUMS).
- Hemlighetsskanningen hittade endast testfixturens lösenord, inte någon
  produktionshemlighet. Inga untracked filer riskerar att försvinna.

### DEL 2 – verklig AI i projektflödet: PASS

`tests/backend/chat/test_project_session_real_model.py` (1 passed, 41 s) kör samma resa som
den godkända `test_project_session_resume.py`, men med riktig Ollama `qwen2.5:0.5b`:
inloggning → projekt → uppgift → chatt med projektkontext → sparat svar → två riktiga
worker-cykler → full backend-lifespan-omstart → omladdning → fortsatt chatt med historiken i
prompten → annan grundare nekas 403. Deterministiska assertioner är separerade från det enda
modellstyrda: svaret är icke-tomt och har `ollama`-proveniens.

### DEL 3 – fel och återhämtning: PASS

`tests/backend/chat/test_chat_failure_recovery.py` (7 passed; fem körningar i följd):
Ollama nere före sändning (och retry), avbrott under generering, backend-omstart, samma
meddelande igen efter omstart, projektkontext saknas, projektkontext otillgänglig, fallback
förbjuden av policy, samt annan användare. Varje test kontrollerar att historik, idempotens
och behörigheter överlever felet — inte bara att ett fel returnerades.

### DEL 4 – integrationsförberedelse: PASS (dokumenterad)

Jämfört med `integration/founder-alpha-20261121` (`0adb108f`, merge-base `dd69cb30`) och
`claude/founder-alpha-pre-vps-consolidation` (`b313c9df`, merge-base `86671468`). Båda saknar
`context_atlas/`, `providers/circuit_breaker.py`, `founder_memory/prompt.py` och
`reuse_gate.py`, så hela branchen måste integreras; tre commits kan inte cherry-pickas löst.
**Inga migrationer** och inga nya miljövariabler. Claudes staging-filer och Z:s säkerhetskod
är orörda.

### DEL 5 – tester och leverans: PASS för fokuserade sviter

- `6145a764` providerfix: en aldrig etablerad Ollama-anslutning är bevisat pre-egress och
  retrybar; read timeout förblir ambivalent. 15 providertester.
- `90be8867` fel-/återhämtningssvit: 7 tester.
- `ee67cbe8` real-model-acceptans: 1 test.
- Hela `tests/backend/chat` + `tests/backend/providers`: **357 passed**, 8 m 46 s; ruff rent.

### NOT_RUN / BLOCKED

- CI #511 `45562008534` är fortfarande `in_progress`; #507–#509 är `cancelled`; #510:s run-id
  svarar 404. Ingen körning är PASS, och ingenting har pushats.
- Webbläsare/frontend: Chrome saknas; frontend ägs av Claude.
- Hela backend-sviten parallellt: 2 GB VPS, endast fokuserade sviter.
- `security/POLICY_LOCK.json`: divergensen kvarstår (Z); filen är orörd.
- Staging på 16 GB med produktionsmodell: inte körd här.

### Första oavslutade moment

1. Invänta att CI #511 blir terminal och verifiera varje körning som PASS/FAIL/CANCELLED.
2. Kör real-model-acceptansen på 16 GB-VPS:en med den modell som ska användas i staging.
3. Fastställ publiceringsordning för #507–#511 + denna branch utan force-push eller merge.
