# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-08-02, mot GitHubs PR-/check-runs-API
direkt (`mcp__github__pull_request_read`/`get_check_runs`/`get_job_logs`, inte memorerat).
**PR #31** står nu på head (Pass 29, pushas i denna omgång) — se Pass 29-avsnittet nedan för
den FJÄRDE granskningsrundans cross-domain blobretention-blockerare (åtgärdad). Föregående
head `9851c0b` (Pass 28) var grön på ALLA obligatoriska kontroller UTOM
`Frontend — npm audit`, som fortsatt är ett bekräftat orelaterat, förklarat fynd (se Pass 26
nedan och **PR #32**, `claude/frontend-npm-audit-ghsa-mh99-source-ids` — öppen, egen branch
grenad från `claude/det-kommer-mer-879lcm`, verifierad helt grön, väntar på grundarens
uttryckliga godkännande innan merge). Tidigare rad,
oförändrad: **PR #29 mergad** som `0bdf03d`, verifierad grön (18/18 checkar) på exakt head-SHA
`df9e9c8`
innan merge, inte en äldre commit. **PR #30 mergad** som `9b15840` in i
`claude/det-kommer-mer-879lcm` — verifierad grön (18/18 checkar, "All required checks passed")
på exakt head-SHA `b2347e4` (PR-branchens sista commit) direkt innan merge, samma disciplin
som PR #29. `claude/memory-source-unit-design` är nu mergad och kan städas bort (branchen har
inga oavslutade delar kvar — hela dess innehåll är designdokumentation som nu lever i
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`s §4.8 på huvudgrenen). §4.8 är den kanoniska,
GODKÄNDA arkitekturen för `MemorySourceUnit`/S1A.

**PR #31** (`claude/s1a-memory-source-implementation`, grenad från `claude/det-kommer-mer-879lcm`
efter PR #30:s merge) — draft, öppen, INTE mergad, INGEN deploy/produktionsmigration körd.
Implementerar §4.8:s design: migration `0019_memory_source_units` (tabeller, CHECKs,
triggers, `transition_own_memory_source`/`transition_memory_source_admin`/
`erase_owner_memory`/`erase_owner_memory_admin`), SQLAlchemy-modeller,
`app/rag/memory_source.py`s race-säkra find-or-create, den delade `backend/scripts/
s1a_privilege_policy.py` (använd atomiskt av både `ensure_app_role.py` och
`apply_runtime_privileges.py`), grundlagret verifierat genom FYRA granskningsrundor (Pass
14–17). Pass 18 lade till deterministisk backfill (`app/rag/memory_source_backfill.py`) och
dual-write (`app/rag/claims.py`) ovanpå det godkända grundlagret. Pass 19 åtgärdade fyra
integrationsproblem grundaren hittade i den granskningen (`library_import.py`s saknade
rollback, backfillens `batch_size<=0`-oändlig-loop-risk, dual-writes ouverifierade
`version_id`, produktionsrapportering dokumenterad men inte byggd) och rättade en felaktig
"96 tester"-siffra i PR-beskrivningen. Pass 20 (nedan) lade till den delade
`app/rag/source_purge.py::purge_source()`-tjänsten, nu använd av BÅDA `library.py`s
`delete_source` och den tidigare separat implementerade `DELETE /api/documents/{id}`. Pass 21
(nedan) rättade en verklig bugg Pass 20:s egen "atomisk"-beskrivning inte höll för: bloben
raderades fysiskt FÖRE DB-commit, så ett commitfel efter en lyckad filradering skulle
återuppliva ett levande dokument vars originalfil redan var permanent borta. Pass 22 (nedan)
åtgärdade två ytterligare integrationsluckor grundaren hittade i blob-/audit-hanteringen:
`maybe_purge_blob()` kände bara till levande `Document.storage_key`-rader, aldrig
`ImportJob.source_storage_key` (en väntande/körande/återupptagningsbar importjobb-blob kunde
raderas av en orelaterad källradering), plus ett TOCTOU-race mellan uppladdning och
blob-purge; och `source_purged`-revisionsposten skrevs i en SEPARAT commit i routern efter att
`purge_source()` redan committat, vilket kunde ge ett 500-svar för en radering som redan
lyckats. Pass 23 (nedan) täppte till en blockerande cross-owner RLS-lucka: den globala,
innehållsadresserade blobreferenskontrollen kördes som vanliga ORM-frågor mot `documents`/
`knowledge_import_jobs` inuti anropande ägarens egen RLS-scopade session — strukturellt
oförmögen att se en ANNAN ägares levande dokument eller väntande importjobb som delade samma
`storage_key`. Löst med en ny, smal `SECURITY DEFINER`-funktion
(`storage_key_still_referenced_global`, migration `0020`), inte en RLS-avstängning. Pass 24
(nedan) täppte till två kvarstående privilegieblockerare grundaren hittade i den granskningen:
`s1a_privilege_policy.py` verifierade aldrig `pg_proc.prosecdef` (en `ALTER FUNCTION ...
SECURITY INVOKER` hade passerat alla andra kontroller tyst), och `ensure_app_role.py`s
S1A-omsmalning var gated på att ALLA S1A-objekt existerar — vilket lämnade ett "mixed-version
boot window" öppet mellan migration 0019 och 0020 där en bred `GRANT ALL` kunde committas
oomsmalnad. Löst med en `require_complete`-flagga genom `apply_privilege_policy()`. Pass 25
stängde en kvarstående verifieringslucka: funktionssignaturer matchades bara på namn,
inte exakta argumenttyper, plus två test-/dokumentationsfel (mixed-version-testets
`to_regclass`-bugg, en duplicerad ImportJobStatus-lista i statusdrifttestet, och en felräknad
testsumma). Pass 26 levererade grundarens sista begärda funktionella S1A-skiva:
konto-export/erasure-integrationen — `erase_account_data()`/`export_account_data()` som delade
domäntjänster, `app/routers/account.py` omskrivet till en tunn wrapper, en durabel
`storage_deletion_tasks`-köfor fysisk blob-radering (migration `0021`), och stängning av ett
upload/erasure-race. Verifieringen hittade och åtgärdade också en E2E-privilegielucka i CI
(åtgärdad direkt i PR #31) och en npm audit-ID-churn (åtgärdad på egen branch, **PR #32**).
Pass 27 (nedan) — en andra granskningsrunda av kontoslicen — stängde ett blockerande
privilegiehål (`storage_deletion_tasks` gav `mainai_app` SELECT+UPDATE på en tabell utan
owner_id/RLS), rättade exportauditens transaktionsmodell, synkade modellens enum-typer mot
migrationens verkliga varchar+CHECK-schema, gjorde taskclaiming atomisk och
flerworker-säker, samt granskade och dokumenterade alla blob-skrivande vägar. 171 dedikerade
S1A-/konto-tester totalt över 9 filer (154 tidigare + Pass 27:s 17 nya — `test_account_
erasure.py`: 14 nya, `test_worker.py`: 1 ny, `test_memory_source_units.py`: 1 ny, `test_
account_deletion.py`: 1 ny). Hela backend-/security-/account-sviten: **727 passed, 1
skipped**, verifierat direkt (upp från Pass 26:s 710 med exakt Pass 27:s 17 nya tester). CI
grön på PR #31:s head `5f4f2fd`, alla obligatoriska kontroller UTOM det fortsatt spårade
`npm audit`-fyndet (PR #32).

**Kvarstår innan PR #31 kan gå från draft till granskningsklar/mergbar** (se PR-beskrivningen
och §4.8:s "Status"-avsnitt för den fullständiga listan): produktionsdataprofilen (krävs före
MERGE, inte före draft), den beständiga run-/felrapporteringen Pass 19 dokumenterar men
medvetet inte bygger än (krävs före en RIKTIG produktionsbackfill-körning, inte bara denna
PR:s merge), samt att **PR #32** mergas till huvudgrenen (varefter PR #31 uppdateras DÄREFTER,
inte i förväg — se Merge-regeln nedan) innan `npm audit`-kontrollen kan bli grön på PR #31
själv. Det tidigare dokumenterade racet mellan kontoradering och en redan köad (`pending`)
importkörning — som grundaren i Pass 28 uttryckligen underkände som "acceptabel follow-up" —
är STÄNGT (Pass 28, `claim_next_job()`s tvåfas-ägarlåsta claim). Den cross-domain
blobretention-blockeraren grundaren hittade i Pass 29 (global blobkontroll som saknade
Project Memory) är också STÄNGD (Pass 29, nedan). Kontoexport/erasure-integrationen är KLAR
(Pass 26), den andra granskningsrundans fynd är åtgärdade (Pass 27), den tredje
granskningsrundans tre blockerare är åtgärdade (Pass 28) — inklusive en verklig
Postgres-deadlock Pass 28:s egen fulla testsviteskörning avslöjade — och den FJÄRDE
granskningsrundans cross-domain-fynd är åtgärdat (Pass 29). Nästa kontrollpunkt enligt
grundarens instruktion: vänta på FÄRSK granskning av Pass 29:s ändringar innan arbetet
fortsätter längre — grundaren var explicit att detta INTE är ett godkännande att gå vidare
till produktionsprofil/merge/deploy/produktionsbackfill/P4/P6/Admin reboot-knapp, och att
PR #32 INTE ska mergas utan uttryckligt godkännande.

## Pass 29 (2026-08-02): PR #31 — fjärde granskningsrundan: global blobkontroll saknade Project Memory (cross-domain orphan-blob-risk)

Grundarens bedömning: "Pass 28:s tre huvudfixar är godkända i sak. Men den fysiska
blobpolicyn är fortfarande global endast mellan användarkonton, inte global mellan systemets
datadomäner." Content-addressed lagring är global — samma bytes i två olika domäner får samma
`storage_key`, exakt samma egenskap som redan tvingade fram migration 0020:s cross-owner-fix
(Pass 23), bara inte ännu stängd mellan OLIKA DATADOMÄNER (per-konto Life Library-data vs.
founder-brett projektminne).

**Det konkreta felet:** `storage_key_still_referenced_global()` (migration 0020) kontrollerade
bara `documents.storage_key`/`knowledge_import_jobs.source_storage_key`. `app/project_
memory.py`s founder-breda Project Memory skriver genom EXAKT samma `get_storage()`/
`write_stream()`-backend till `project_sources.storage_key`/`project_checkpoints.
brief_storage_key` — helt osynligt för funktionen. Scenario: Project Memory lagrar innehåll X
→ en användare laddar upp byte-identiskt X → båda delar `storage_key` → kontot raderas →
`enqueue_account_erasure_storage_task()` (migration 0022) godkänner nyckeln korrekt (användaren
äger verkligen ett Document/ImportJob) → Document/ImportJob-raderna försvinner → den globala
referenskontrollen ser ingen Document/ImportJob kvar, känner inte till Project Memory,
returnerar false → maintenance-workern raderar bloben fysiskt → Project Memory pekar nu på en
fil som inte längre finns. `project_checkpoints.brief_storage_key` är dessutom `NOT NULL` — en
checkpoint vars brief-blob försvinner är en permanent trasig rad, inte återställningsbar.

**Fixat:**

1. **Migration `0023`** — `CREATE OR REPLACE` av `storage_key_still_referenced_global()`
   (samma exakta signatur, `SECURITY DEFINER`, `search_path`, `REVOKE PUBLIC` — ingen ny
   funktion, ingen ändring av den befintliga `documents`/`knowledge_import_jobs`-logiken,
   kopierad ordagrant från migration 0020) med två nya OR-grenar: `project_sources.storage_key`
   och `project_checkpoints.brief_storage_key`. `downgrade()` återställer migration 0020:s
   EXAKTA ursprungliga funktionskropp (inte en `DROP`), så `test_migration_roundtrip.py`s
   schema-fingeravtryck (som hashar `pg_get_functiondef()`) ser en verklig, annorlunda kropp
   efter nedgradering och exakt samma kropp igen efter omgradering. Verifierat direkt: kropp
   innehåller `project_sources`/`project_checkpoints` efter upgrade, INTE efter downgrade,
   INNEHÅLLER dem igen efter re-upgrade.
2. **Fullständig lagringsdomän-inventering** (grundarens uttryckliga krav — "gissa inte att
   Project Memory är den enda ytterligare domänen"), utförd med en dedikerad genomsökning av
   hela backend/ efter `storage_key`-liknande kolumner, `get_storage()`-anrop,
   `.write_stream()`/`.delete()`-anrop:
   - **`documents.storage_key`** — redan skyddad (migration 0020). Klass A.
   - **`knowledge_import_jobs.source_storage_key`** — redan skyddad (migration 0020). Klass A.
   - **`project_sources.storage_key`** — SAKNADES, nu skyddad (migration 0023). Klass A.
   - **`project_checkpoints.brief_storage_key`** — SAKNADES, nu skyddad (migration 0023). Klass A.
   - **`storage_deletion_tasks.storage_key`** — konsumerar kontrollen (kön för fysisk radering),
     är inte själv en levande referens. Klass B, korrekt exkluderad.
   - `documents.media_blob` (LargeBinary, migration 0010) — separat in-DB-kolumn, inte
     content-addressed lagring. Klass C.
   - `memory_source_unit.py`s `source_identity_key` — orelaterad identitetssträng, inte en
     blobnyckel. Klass D.
   - **Ytterligare fynd, UTANFÖR scope för denna omgång, dokumenterat men INTE åtgärdat här**
     (per samma "isolera orelaterade ändringar"-princip som `CLAUDE.md` etablerar):
     `app/routers/library.py:159` gör ett OGRINDAT `storage.delete(blob.storage_key)` för en
     tom (0 byte) uppladdning, utan någon `storage_key_still_referenced_global()`-kontroll
     alls — eftersom lagringen är innehållsadresserad kunde hash-nyckeln för tomt innehåll i
     teorin redan vara refererad av en annan rad (inklusive de två domänerna som fixades här).
     Blast radius idag är litet (bara tomma filer), men det är en verklig, separat
     TOCTOU-lucka som INTE är del av detta fynd och bör hanteras i en egen, senare branch/PR
     om grundaren vill prioritera den.
3. **Cross-domain regressionstester** (grundarens exakta bokstavsordning A–E):
   - **Test A** (`test_account_erasure.py`): en `ProjectSource` som delar `storage_key` med en
     raderad ägares `Document` → task blir `retained_shared`, bloben finns kvar, `ProjectSource`
     orörd.
   - **Test B** (`test_account_erasure.py`): samma för `ProjectCheckpoint.brief_storage_key`.
   - **Test C** (`test_source_purge.py`): endast en `ProjectSource` refererar nyckeln efter att
     Document/ImportJob-raderna är borta → `storage_key_still_referenced_global()` returnerar
     `true`.
   - **Test D** (`test_source_purge.py`): ingen domän alls refererar nyckeln → funktionen
     returnerar `false` (bevisar att den inte bara blir permanent `true` — varje domäns
     referens kontrolleras levande).
   - **Test E** (cross-owner-skyddet, Pass 23): redan täckt av den befintliga svit av tester i
     `test_source_purge.py` — körda på nytt, oförändrat gröna.
4. **Drift-förhindrande register + test.** Ny kanonisk konstant, `app.rag.blob_references.
   KNOWN_STORAGE_KEY_COLUMNS` — den hand-underhållna listan över varje `table.column` som kan
   hålla en levande referens till den delade content-addressed lagringen, med en dokumenterad
   process för att lägga till en ny kolumn (registret + en ny migration + ett retentiontest,
   allt i samma ändring). Ny test,
   `test_known_storage_key_columns_registry_matches_the_sql_functions_real_behavior`
   (`test_source_purge.py`), itererar registret och bevisar att SQL-funktionen faktiskt
   skyddar VARJE post — en framtida kolumn som läggs till i registret utan matchande
   SQL-täckning misslyckas omedelbart, istället för att tyst återöppna exakt samma
   cross-domain-lucka.

**Tester:** 6 nya (`test_account_erasure.py`: 2 — Test A/B; `test_source_purge.py`: 4 — Test
C/D, en extra SQL-nivåtest för `ProjectCheckpoint`, samt drift-registertestet). Hela
backend-/security-/account-sviten: **750 passed** (upp från Pass 28:s 744, exakt Pass 29:s 6
nya), verifierat direkt TVÅ gånger i följd. Bare-DB-migrationsrundtripp
(`0022→0023→0022→0023`) verifierad mot en databas UTAN `mainai_app`-rollen alls —
funktionskroppen innehåller `project_sources`/`project_checkpoints` efter upgrade, inte efter
downgrade, igen efter re-upgrade. `apply_runtime_privileges.py` omkörd mot testdatabasen —
`storage_key_still_referenced_global`s signatur/EXECUTE-grant är oförändrad (samma signatur
som migration 0020, ingenting att ändra i privilegiepolicyn).

**Grundarens explicita avslutande instruktion (Pass 29), oförändrad från tidigare omgångar:**
ingen produktionsdataprofil, ingen produktionsbackfill, ingen merge av PR #31, ingen merge av
**PR #32** utan uttryckligt godkännande, ingen deploy — vänta på färsk granskning innan arbetet
fortsätter längre.

## Pass 28 (2026-08-02): PR #31 — tredje granskningsrundan av kontoslicen: oändlig retry-loop, INSERT fortfarande farligt, det avvisade pending-job-racet stängt

Grundarens bedömning: "Pass 27 förbättrar outboxen tydligt, men Code har själv lämnat den
viktigaste pending-job-racen öppen, och den nya immediate-retryloopen återintroducerar en
redan känd infinite-loop-felklass." Tre blockerare, alla åtgärdade, plus en verifieringspunkt
och en verklig deadlock som denna omgångs egen fulla re-verifiering (inte grundarens egen
granskning) avslöjade:

1. **Oändlig retry-loop vid permanent fel (stängd).** `attempt_pending_storage_deletions_for_
   operation()`s `while True`-loop, kombinerad med Pass 27:s `claim_storage_deletion_tasks()`
   som behandlade `pending`/`failed` som lika omedelbart claimbara, återintroducerade en redan
   känd felklass (samma som en tidigare, redan fixad backfill-bugg): en task som misslyckas med
   ett PERMANENT `StorageError` blev `failed`, och nästa loop-iteration claimade och
   återförsökte SAMMA task igen — för evigt. Fixat via en ny `include_failed`-parameter på
   `claim_storage_deletion_tasks()`: det omedelbara försöket claimar nu med
   `include_failed=False` (varje task denna operation skapade försöks högst en gång här);
   allt som blir `failed` lämnas helt åt workerns egen retry-loop
   (`include_failed=True`, default), som nu respekterar en begränsad, exponentiell, jittrad
   backoff (`next_attempt_at`, migration `0022`, satt av `attempt_storage_deletion_task()` via
   `app.jobs.retry.compute_backoff_seconds` — samma rena policyfunktion STEG 11:s
   importjobb-retries redan använder).
2. **INSERT-only fortfarande farligt (stängd).** Pass 27:s `mainai_app`-policy (INSERT-only på
   `storage_deletion_tasks`) var fortfarande fel: INSERT i just den tabellen är INDIREKT ÅTKOMST
   TILL EN PRIVILEGIERAD FYSISK RADERINGSOPERATION, eftersom ingenting i databasen verifierade
   att en infogad `storage_key` faktiskt tillhörde den infogande ägaren, eller ens refererade
   något verkligt alls — `app/project_memory.py`s founder-breda blobbar (utanför workerns
   referenskontroll, `storage_key_still_referenced_global()`) är exakt den typ av data en
   felaktigt köad godtycklig nyckel kunde förstöra spårlöst. Fixat: `mainai_app` får NOLL
   direkta privilegier på `storage_deletion_tasks` (migration `0022` +
   `s1a_privilege_policy.py`), och en ny `SECURITY DEFINER`-funktion,
   `enqueue_account_erasure_storage_task(operation_id, storage_key)`, är den ENDA vägen en
   vanlig session kan skapa en task-rad: den härleder anroparen från `app.current_user_id`,
   verifierar explicit att nyckeln tillhör just den ägaren via `Document.storage_key`/
   `ImportJob.source_storage_key` (litar aldrig på Python-kodens egen inventeringsfråga som
   auktorisering), sätter `reason`/`status` själv, och är idempotent på
   `(operation_id, storage_key)`. `erase_account_data()`s lagernyckel-inventering anropar nu
   denna funktion via `db.execute(sa_text("SELECT enqueue_account_erasure_storage_task(...)"))`
   istället för en ORM-`INSERT`.
3. **Det avvisade pending-job-racet (stängd, INTE dokumenterad som follow-up).** Grundaren
   avvisade uttryckligen mitt eget Pass 27-omdöme att lämna racet mellan kontoradering och en
   redan köad (`pending`) importkörning som dokumenterad follow-up: "Det räcker inte att
   dokumentera racet som follow-up. Det är precis den race account-slicen skulle stänga."
   Stängt genom att göra om `app/jobs/lease.py`s `claim_next_job()` till en tvåfas,
   ägarlåst claim (se den modulens egen docstring för hela mekanismen): en låsfri
   kandidat-SELECT, DÄREFTER `acquire_owner_erasure_lock()` för den kandidatens ägare INNAN
   någon radlåsning tas alls (aldrig efter — samma ordning `erase_account_data()` redan
   följer, så de två kan aldrig deadlocka mot varandra), DÄREFTER en atomisk omvaliderad claim
   av exakt den kandidaten, med omförsök på en färsk kandidat vid förlorad kapplöpning.
   Vinnaren av ägarlåset (en workers claim, eller själva erasure-transaktionen) committar eller
   rullar tillbaka helt innan den andra sidans radnivå-arbete ens kan börja — en väntande job
   sveps antingen säkert in i erasure-transaktionens egen lagernyckel-inventering innan en
   worker hinner börja skriva nya blobbar mot den, eller så claimar workern jobbet säkert innan
   erasure hinner se det som blockerande (via den befintliga `AccountErasureBlockedError`-
   spärren, oförändrad). Verifierat med RIKTIGA två-trådars-tvåsessions-tester för BÅDA
   race-ordningarna (`test_claim_next_job_winning_the_owner_lock_race_blocks_a_concurrent_
   erasure`, `test_erasure_winning_the_owner_lock_race_leaves_nothing_for_claim_next_job_to_
   claim`), båda med bundna `join(timeout=5)` som dubblerar som deadlock-timeout-bevis, och
   ett explicit orphan-bevis (den väntande jobbens `source_storage_key` MÅSTE finnas i
   `storage_deletion_tasks` efter att erasure vunnit racet).
4. **Claim-tillståndsövergångar (verifierade, inga kodändringar behövdes utöver punkt 1).**
   `completed_at`/`next_attempt_at` nollställs explicit i början av varje nytt
   `attempt_storage_deletion_task()`-anrop (defensivt — i praktiken var de redan alltid `NULL`
   för en icke-terminal task, men detta gör invarianten explicit snarare än implicit).
   `last_error` sätts konsekvent (`None` vid framgång, felmeddelandet vid `failed`).
   `attempt_count` inkrementeras ENDAST av ett verkligt I/O-försök, aldrig av en claim (bevisat
   av design: `claim_storage_deletion_tasks()` rör aldrig den kolumnen). Terminal-tasks
   (`purged`/`retained_shared`) är aldrig claimbara, bevisat direkt med en ny test
   (`test_claim_storage_deletion_tasks_never_reclaims_a_terminal_purged_or_retained_shared_
   task`) som sätter en artificiellt gammal `updated_at` på en terminal task och verifierar den
   ändå inte claimas.

**En verklig Postgres-deadlock upptäckt under denna omgångs egen fulla testsviteskörning** (inte
en teoretisk oro, inte grundarens fynd — upptäckt av mig själv genom att faktiskt köra hela
sviten, inte bara den nya filen isolerat): `erase_account_data()` tog `FOR UPDATE`-lås på
`users`-raden FÖRE den förvärvade `acquire_owner_erasure_lock()` — omvänd ordning mot varje
annan plats i kodbasen (uppladdning, `claim_next_job()`) som redan tar ägarlåset FÖRST. En
konkurrerande uppladdning som redan höll ägarlåset och väntade på ett `FOR KEY SHARE`-lås på
samma `users`-rad (Postgres FK-validering för `ImportJob.owner_id`) kunde deadlocka mot en
erasure-transaktion som höll radlåset och väntade på ägarlåset — klassisk cirkulär
låsordning. Postgres egen deadlock-detektor fångade det (`DeadlockDetected`), men bara i den
fulla sviten, inte i den isolerade testfilen — ren timing. Fixat genom att flytta
`acquire_owner_erasure_lock()`-anropet FÖRE `with_for_update()`-frågan, vilket samtidigt
bevarar den befintliga "serialisera en andra samtidig erasure"-garantin (ägarlåset serialiserar
redan det fallet) och tar bort låsordningscykeln helt. Verifierat: den tidigare deadlockande
testen (`test_owner_erasure_lock_serializes_erasure_against_a_concurrent_upload_for_the_same_
owner`) och de två nya race-testerna körda 8x i rad utan en enda deadlock, plus hela sviten
grön två gånger i följd.

**Tester:** 17 nya (16 i `test_account_erasure.py` — inklusive de två riktiga
tvåtrådars-race-testerna för `claim_next_job()`, den permanent-fel-utan-loop-regressionen, sex
`enqueue_account_erasure_storage_task()`-tester för ägarskap/cross-owner/godtycklig
nyckel/project_memory-nyckel/idempotens/oautentiserad anropare, samt privilegiegräns- och
backoff-tester; 1 i `test_memory_source_units.py` — `test_mixed_version_boot_window_0021_to_
0022`, plus omskrivning av `test_mixed_version_boot_window_0020_to_0021`s scenario C och
privilegiekatalogens `expectations`-dict för Pass 28:s nollprivilegiepolicy). Hela
backend-/security-/account-sviten: **744 passed** (upp från Pass 27:s 727 passed + 1 skipped),
verifierat direkt, inklusive en bar-DB-migrationsrundtripp (`0021→0022→0021→0022`) mot en
databas UTAN `mainai_app`-rollen alls. `docker-entrypoint.sh`s riktiga boot-ordning
(`ensure_app_role` → `alembic upgrade head` → `apply_runtime_privileges`) opåverkad — inga
ändringar i den filen denna omgång.

**Grundarens explicita avslutande instruktion (Pass 28), oförändrad från tidigare omgångar:**
ingen produktionsdataprofil, ingen produktionsbackfill, ingen merge av PR #31, ingen merge av
**PR #32** utan uttryckligt godkännande, ingen deploy — vänta på färsk granskning innan arbetet
fortsätter längre.

## Pass 27 (2026-08-02): PR #31 — andra granskningsrundan av kontoslicen: privilegiehål, audittransaktion, schema-drift, atomisk claiming

Grundaren bekräftade att Pass 26:s huvudsakliga erasureflöde (radlåsning, storage-inventering,
owner-lås) var korrekt genomtänkt, men fann två blockerande problem och en schema-drift innan
produktionsprofilen.

**1. `storage_deletion_tasks` var för brett privilegierad.** Tabellen saknar avsiktligt
`owner_id`/RLS (se migration 0021), men bootpolicyn gav ändå `mainai_app` — den vanliga,
request-scopade applikationsrollen — SELECT+INSERT+UPDATE på HELA tabellen. Det innebar att
VARJE vanlig requestsession tekniskt kunde läsa alla kontoraderingars storage-nycklar och
operation-ID:n, eller skriva om vilken tasks status som helst — inte en säkerhetsgräns bara
för att ingen router råkade göra det idag. Löst genom att smalna av `mainai_app`s grant till
ENDAST INSERT (`s1a_privilege_policy.py`). All läsning/claiming/uppdatering flyttades till en
egen, privilegierad maintenance-session (`app/rag/account_erasure.py`s nya
`_MaintenanceSession`, samma mönster som `app/worker.py`s befintliga `_ClaimSession` för
`knowledge_import_jobs`) — den vanliga requestsessionen `erase_account_data()` kör på rör
aldrig tabellen efter sina egna INSERT-satser. En verklig, inte-uppenbar bieffekt upptäcktes
under implementationen: SQLAlchemy 2.0 hämtar som standard servergenererade kolumner
(`created_at`/`updated_at`) tillbaka via en `INSERT ... RETURNING`-sats, vilket kräver
SELECT-privilegium utöver INSERT — utan att stänga av det (`__mapper_args__ = {"eager_
defaults": False}`) hade även den legitima kontoraderingens egna INSERT av tasks börjat
misslyckas med "permission denied" så fort omsmalningen tillämpades.

**2. Exportauditens transaktion följde inte den beslutade modellen.** `export_account_data()`
anropade `record_audit(...)` utan `commit=False`, vilket gjorde att auditfunktionens egen
separata `db.commit()` kördes istället för en kontrollerad transaktion — motsäger kravet och
gjorde det omöjligt att skilja "export byggd OCH audit committad" från "export byggd men
audit-commit misslyckades", med ingen rollback-punkt för det senare fallet. Rättat: `record_
audit(..., commit=False)` följt av ett explicit `db.commit()`, med `db.rollback()` +
återkastning vid fel. Nytt test tvingar ett commitfel EFTER auditinsert och bevisar att
auditposten rullas tillbaka och att exporten aldrig returneras som lyckad.

**3. Modell och migration beskrev olika databastyper.** Migration 0021 skapar `reason`/
`status` som `varchar(N) + CHECK`, men SQLAlchemy-modellens `Enum(...)` implicerade (om än
harmlöst i praktiken, eftersom bindprocessorn bara skickar strängvärden) en NATIV Postgres
ENUM TYPE som aldrig faktiskt skapades. Rättat med `native_enum=False, create_constraint=
False` och exakt matchande längder — migrationens CHECK förblir den enda databassanningen.

**4. Taskclaiming var en oskyddad, olåst scan.** Både den omedelbara best-effort-attempten och
workerns återförsöksscan gjorde `.all()`-frågor utan `FOR UPDATE SKIP LOCKED`, vilket kunde
låta två samtidiga claimers (varandra, eller två workerprocesser) plocka upp och dubbelbehandla
SAMMA rad. Löst med en ny `claim_storage_deletion_tasks()` — exakt samma atomiska `UPDATE ...
WHERE id = ANY(SELECT ... FOR UPDATE SKIP LOCKED ...) RETURNING id`-mönster
`app/jobs/lease.py`s `claim_next_job()` redan använder för `knowledge_import_jobs` — med
bunden batchstorlek och en lease (`updated_at` + `lease_seconds`) som gör en `processing`-rad
vars claimer kraschat återclaimbar. Verifierat med ett riktigt tvåtrådars/tvåsessions-test som
bevisar att ingen rad någonsin claimas av båda samtidigt, plus ett dedikerat lease-utgångstest.

**5. Genomgång av alla blob-skrivande vägar** (`storage.write`/`storage.write_stream`):
`app/routers/library.py`s uppladdningsändpunkt var redan täckt (Pass 26). `app/rag/library_
import.py`s `_store_bytes()` (workerns per-fil-skrivningar under bearbetning av ett REDAN
accepterat importjobb) var det INTE — och kan inte stängas med samma transaktionsbundna lås,
eftersom `run_import_job` committar efter varje fil för att förbli återupptagningsbar. Löst
genom att `erase_account_data()` nu VÄGRAR fortsätta medan en `running`-importkörning med
ogången lease pågår för kontot (`AccountErasureBlockedError`, av routern mappad till HTTP 409)
— stänger det realistiska, långvariga fallet (en worker som aktivt extraherar/embeddar ett
flerfilsimport) men INTE ett smalare kvarstående race mot en redan köad (`pending`) körning
som hinner claimas mellan kontrollen och denna transaktions commit (skulle kräva att `claim_
next_job()` självt tar ett per-ägarlås, vilket motverkar dess syfte att se alla ägares jobb i
en enda fråga) — medvetet dokumenterat som kvarstående, inte stängt i denna omgång, hellre än
en forcerad, overifierad låsomdesign under tidspress. `app/project_memory.py`s tre
blob-skrivningar är INTE kontobundna data — de är MainAI Cores egna, founder-breda
projektminnesobjekt (`ProjectSource`/`ProjectCheckpoint`, uttryckligen dokumenterade som
"Not RLS-protected... founder-wide project state, not per-user data") och korrekt utanför
kontoraderingens scope.

**Tester:** 17 nya — `test_account_erasure.py` (14: privilegiegräns i realtid (`SELECT`/
`UPDATE` nekas, `INSERT` fungerar), `claim_storage_deletion_tasks` (operation-scopning,
gräns, lease-ej-utgången, lease-utgången-återclaim, verkligt tvåtrådarsrace), `erase_account_
data` vägrar/fortsätter kring `running`/`pending`/utgången-lease-importjobb, exportauditens
tvingade commitfel, samt modell/schema-testerna för varchar/CHECK); `test_worker.py` (1: `_
retry_storage_deletion_tasks()` end-to-end genom den riktiga metoden); `test_memory_source_
units.py` (1: `test_mixed_version_boot_window_0020_to_0021`, samma mekanism som 0019→0020-
testet, nu för migration 0021; plus `storage_deletion_tasks` tillagd i de befintliga
least-privilege/reboot-persistence-testerna); `test_account_deletion.py` (1: HTTP-nivå-409 när
en importkörning aktivt pågår).

Omverifiering: riktat regressionssvep (`test_account_erasure.py`+`test_memory_source_units.py`
+`test_ensure_app_role.py`+`test_source_purge.py`+`test_worker.py`+`test_migration_roundtrip.py`
+`test_library_import.py`+`test_library_routes.py`+`test_claims.py`+`test_memory_source_
backfill.py`+`test_storage_local_fs.py`+`test_provider_verification.py`+`test_account_
deletion.py`) 315/315. Bare-DB-migrations-round-trip (`upgrade head` → `downgrade -1` →
`upgrade head` → `downgrade base` → `upgrade head`) mot en färsk `postgres`-superuser-databas
(`lifeos_bare_check_p27`, ingen `mainai_app`-roll) ren. Hela backend-/security-/account-sviten:
**727 passed, 1 skipped** — exakt Pass 26:s 710 + Pass 27:s 17 nya. CI grön på PR #31:s exakta
head `5f4f2fd`, alla obligatoriska kontroller UTOM det fortsatt spårade, orelaterade `npm
audit`-fyndet (PR #32, väntar på grundarens uttryckliga godkännande innan merge).

Grundarens instruktion var explicit: stanna nu för ny granskning — ingen produktionsprofil,
produktionsbackfill, merge eller deploy ännu, och PR #32 ska INTE mergas utan grundarens
uttryckliga godkännande.

## Pass 26 (2026-08-02): PR #31 — kontoexport/kontoradering-integration med S1A + två CI-fixar upptäckta under verifiering

Grundaren bekräftade att Pass 25 var godkänd och gav den fullständiga, 8-punkts specen för
nästa godkända skiva: **kontoexport och kontoradering**, med explicit instruktion att stanna
för fräsch granskning efteråt — ingen produktionsprofil, produktionsbackfill, merge eller
deploy.

**1. Delade domäntjänster.** `app/routers/account.py` skrevs om till en tunn wrapper — routern
gör ENDAST autentisering, lösenordsverifiering (vid radering), neutral request-metadata-
extraktion, anrop till tjänsten, cookie-clear EFTER lyckad commit, och fel→HTTP-mappning. All
affärslogik flyttades till två nya moduler:
- `app/rag/account_export.py::export_account_data()` — bygger hela exporten.
- `app/rag/account_erasure.py::erase_account_data()` — hela raderingssekvensen.

**2. Komplett kontoexport.** Behöll alla befintliga sektioner och lade till fyra nya,
ägarscopade och deterministiskt sorterade: `knowledge_claims` (inkl. `memory_source_id`),
`memory_source_units` (inkl. rensade/återkallade källor — `content_text`/`content_hash` är
korrekt `None` för en `purged`-rad, aldrig fabricerat), `document_source_units`,
`memory_source_lifecycle_events`. Inkluderar mjukraderade dokument. `export_schema_version=2`
+ `generated_at` tillagda. Den föråldrade kommentaren ("claims har ingen backande tabell än")
rättad. Revisionsposten `account_data_exported` skrivs EXAKT en gång, bara efter att hela
exportobjektet redan byggts klart — ett fel mitt i insamlingen kan aldrig ge en falsk
revisionspost för data som aldrig faktiskt returnerades.

**3. Atomisk DB-fas för radering.** `erase_account_data()`: låser `User`-raden (`FOR UPDATE`)
→ tar en ägarscopad Postgres-advisory-lock (`acquire_owner_erasure_lock`, seed `1`, skild
namnrymd från `acquire_storage_key_lock`s seed `0`) → inventerar alla unika storage-nycklar
från BÅDA `Document.storage_key` OCH `ImportJob.source_storage_key` → skapar durabla
`StorageDeletionTask`-rader FÖRE någon radrensning → anropar
`SELECT public.erase_owner_memory(:owner_id)` FÖRE dokumentradering (samma arkitekturlärdom
som `source_purge.py`: `document_source_units.document_id`s RESTRICT-FK skulle annars blockera
dokumentraderingen) → befintlig städordning (konversationer/tokens raderas, projekt/uppgifter
nollas, dokument/chunks/versioner/relationer/importjobb raderas, usage/audit anonymiseras) →
`account_deleted`-revisionsposten skrivs MED `user_id=NULL` INUTI SAMMA transaktion, med ett
neutralt `erasure_operation_id` som `entity_id` — den gamla separata post-commit
`record_audit`-anropet borttaget. Hela sekvensen är EN databastransaktion; ingen fysisk
`storage.delete()` sker före DB-commit.

**4. Durabel fysisk blob-radering.** Ny liten, allmän tabell `storage_deletion_tasks`
(migration `0021`) — INGEN FK till `users.id` (måste överleva kontot vars radering skapade
den), INGEN PII. Status: `pending`/`processing`/`purged`/`retained_shared`/`failed`. Ett
omedelbart best-effort-försök körs direkt efter DB-fasens commit, PLUS en worker-återförsöks-
mekanism (`Worker._retry_storage_deletion_tasks`, körs varje `run_once()`-cykel via
superuser-sessionen) för rader som överlever en krasch. Varje nyckel tar samma
storage-key-lock som upload/purge, kontrollerar `storage_key_still_referenced_global` — delad
med en ANNAN ägare ⇒ `retained_shared` (raderas aldrig), annars raderas ⇒ `purged`;
`StorageError` ⇒ `failed` (återförsökbar); redan borttagen fil ⇒ idempotent framgång.

**5. Race mot samtidig uppladdning stängt.** En `User`-radlås ensam räcker inte — en samtidig
uppladdning kan skriva bytes innan dess `ImportJob`-rad (med FK) ens finns. Samma
`acquire_owner_erasure_lock` tas nu även i `POST /api/library/import`, FÖRE
`storage.write_stream`, plus en explicit kontroll att ägaren fortfarande finns direkt efter
låset (annars skulle en påbörjad begäran innan en samtidig radering committat ändå kunna
fortsätta skriva en föräldralös blob, som bara skulle upptäckas som ett fult 500-fel EFTER att
bytes redan skrivits). Ett verkligt tvåtrådars/tvåsessions-samtidighetstest bevisar att ingen
ordning ger en föräldralös blob.

**Tester:** 20 nya i `tests/backend/test_account_erasure.py` (radering: alla källtyper,
legacy-konto utan MSU, rollback vid fel efter `erase_owner_memory`, rollback vid
task-insert-fel, dedup av Document/ImportJob-nycklar, båda nyckelkällorna, omedelbar
purge/retained_shared, aldrig `storage.delete()` före commit, verklig `StorageError`→`failed`
→lyckad retry, idempotens på redan borttagen fil, no-op för redan terminal task; export: aktiv/
återkallad/rensad källa med korrekt innehåll, DSU+lifecycle-events, claims länkade till
`memory_source_id`, cross-owner-isolering, deterministisk ordning, exakt en audit-rad, ingen
audit vid exportfel; lås-race: verklig tvåsessionstest). 4 nya i
`tests/account/test_account_deletion.py` (mjukraderade dokument i export, exakt en
`account_deleted`-audit, cookies rörs inte vid fel lösenord, usage-log överlever anonymiserad).
**24 nya S1A/konto-tester totalt**, ovanpå de 8 redan existerande i `test_account_deletion.py`
— 130+24 = **154 dedikerade S1A/konto-tester totalt över 8 filer** (se tidigare register-poster
för de övriga filernas nedbrytning; `test_account_erasure.py` kräver samma
`_narrow_privileges_before_this_module`-modulfixtur som `test_source_purge.py`/
`test_memory_source_units.py`, eftersom `erase_account_data()` nu anropar `erase_owner_memory()`
— tillagd även i `test_account_deletion.py` av samma skäl).

**Två CI-problem upptäcktes under verifieringen — hanterade enligt olika regler:**

- **E2E-privilegielucka (åtgärdad DIREKT i PR #31).** `E2E — Playwright (full stack)` föll
  rött på head `c0586d0` med `permission denied for function erase_owner_memory`. Grundorsak:
  `.github/workflows/ci.yml`s `e2e-tests`-jobb byggde sin egen roll/databas-setup för hand
  (`GRANT ALL PRIVILEGES ON ALL TABLES ...`) men körde ALDRIG
  `scripts/apply_runtime_privileges.py` — till skillnad från `docker-entrypoint.sh`s riktiga
  bootsekvens (`ensure_app_role` → `alembic upgrade head` → `apply_runtime_privileges` →
  starta appen), som redan gjorde detta korrekt. Utan det EXECUTE-grantet (S1A-funktionerna
  REVOKE:ar EXECUTE FROM PUBLIC i sina egna migrationer) kunde `mainai_app` aldrig anropa
  `erase_owner_memory` i E2E-miljön. Detta var en LATENT lucka sedan S1A:s första funktioner
  (Pass 14+) — den upptäcktes bara nu eftersom Pass 26:s `e2e/account.spec.ts`-raderingstest är
  den FÖRSTA Playwright-specen någonsin som når en S1A SECURITY DEFINER-funktion. Fixat direkt
  i PR #31 (inte en egen branch) eftersom detta är PR #31:s EGEN nya E2E-täckning som
  exponerade luckan, inte ett orelaterat fynd. Commit `ef54588`.
- **npm audit-ID-churn (åtgärdad på EGEN branch/PR, per `CLAUDE.md`s etablerade mönster).**
  `Frontend — npm audit` föll rött på samma head — men PR #31:s diff rör INTE `frontend/`
  alls (bekräftat med `git diff --stat` mellan bas och head: noll filer). Grundorsak: GitHubs
  advisory-databas bytte bara sitt interna `via.source`-ID för SAMMA redan dokumenterade/
  accepterade `brace-expansion`-fynd (GHSA-mh99-v99m-4gvg, `docs/SECURITY_BLOCKERS.md` punkt 3)
  från `1124334` till `1130588`/`1130591` — ingen ny sårbarhet, ingen ändrad
  `package-lock.json`. Exakt samma mönster som PR #8/#9-fallet `CLAUDE.md` dokumenterar. Fixat
  på en egen branch `claude/frontend-npm-audit-ghsa-mh99-source-ids` (grenad från
  `claude/det-kommer-mer-879lcm`, INTE från PR #31:s branch) → **PR #32**, verifierad grön
  (`node scripts/check-npm-audit.js` lokalt + full CI, "All required checks passed"). PR #31
  kommer fortsätta visa `npm audit` som rött tills PR #32 mergas till huvudgrenen och PR #31
  uppdateras DÄREFTER (inte i förväg — se `CLAUDE.md`s Merge-regel).

Omverifiering: `tests/backend/test_account_erasure.py` (20) + `tests/account/
test_account_deletion.py` (12, varav 4 nya) körda direkt, samt hela `tests/backend`+
`tests/security`+`tests/account`-sviten (se resultat nedan/i PR #31:s beskrivning). Bare-DB-
migrations-round-trip (`upgrade head` → `downgrade -1` → `upgrade head` → `downgrade base` →
`upgrade head`, hela kedjan inkl. migration 0021) mot en färsk `postgres`-superuser-databas
utan `mainai_app`-roll, ren. CI grön på PR #31:s exakta slutliga head `ef54588` — ALLA
obligatoriska kontroller `success` UTOM det redan förklarade/spårade `npm audit`-fyndet
(PR #32). PR #32 helt grön, "All required checks passed".

Grundarens instruktion var explicit: detta var den sista stora funktionella
S1A-integrationsskivan innan produktionsprofil och slutgranskning. STANNA nu för fräsch
granskning — ingen produktionsdataprofil, ingen produktionsbackfill, ingen merge, ingen
deploy, ingen P4/P6, ingen Admin reboot-knapp i denna PR.

## Pass 25 (2026-07-30): PR #31 — exakt funktionssignaturverifiering + test-/dokumentationsfixar

Grundaren bekräftade att Pass 24:s SECURITY DEFINER-verifiering och mixed-version-omsmalning
var korrekta i sak, men hittade en kvarstående verklig verifieringslucka plus två test-/
dokumentfel — INGEN account integration ännu.

**1. Exakt funktionssignatur verifierades fortfarande inte.** `_FUNCTIONS` innehöll
funktionsnamn och förväntad returtyp, men inte förväntade ARGUMENTTYPER —
`_function_signature()` sökte bara på namn och accepterade den enda overload som råkade
finnas. En funktion med FEL argumenttyper (t.ex. `storage_key_still_referenced_global
(integer)` istället för applikationens faktiska `(text)`-anrop) hade kunnat passera alla
andra kontroller — `SECURITY DEFINER`, boolean-retur, rätt ägare, rätt grants — medan den i
praktiken var en helt annan funktion än den `blob_references.py` faktiskt anropar, vilket
bara skulle upptäckas som ett runtime-fel.

Löst genom att döpa om `_function_signature` till `_resolve_function(cur, name,
expected_arg_types)`, som nu löser funktionen via `to_regprocedure` med den EXAKTA förväntade
argumentlistan (inte bara namn). `_FUNCTIONS` utökades till 5-tupler med varje funktions
riktiga identity-argumenttyper, hämtade direkt från migration 0019/0020:s `CREATE FUNCTION`-
satser. Om mer än en overload av samma namn existerar i `public`, eller om den enda
overloaden som finns har fel argumenttyper, returneras ett fel och INGET grantas/revokas för
det namnet — en oväntad overload behandlas som en policyöverträdelse, inte tyst ignorerad.

**Fyra nya tester** (`test_source_purge.py`, mot en isolerad engångsfunktion
`s1a_test_sig_check_p25` inuti en psycopg2-transaktion som alltid rullas tillbaka — CREATE
FUNCTION är transaktionell DDL, så inget explicit DROP behövs): A) korrekt funktion med
korrekt signatur resolverar rent utan fel; B) TVÅ overloads av samma namn (en korrekt `(text)`,
en oväntad `(integer)`) gör verifieringen röd, och ett medvetet förinlagt `PUBLIC`-grant på
den oväntade overloaden bevisar att INGET rördes; C) ENDAST fel-signaturen finns (`(integer)`
när `(text)` förväntas) — behandlas som saknad/fel funktion, accepteras aldrig tyst; D) rätt
namn OCH rätt argumenttyper resolverar, men funktionen är `SECURITY INVOKER` — fortfarande
fångad av den befintliga `prosecdef`-kontrollen, vilket bevisar att den nya
signaturupplösningen inte stör den kedjan.

**2. Mixed-version-testets `to_regclass`-bugg.** `test_mixed_version_boot_window_0019_to_0020`
kontrollerade om 0020:s funktion existerade med `to_regclass('public.storage_key_still_
referenced_global')` — `to_regclass` löser RELATIONER (tabeller/vyer), ALDRIG funktioner, så
den returnerar NULL oavsett om funktionen finns eller inte. Assertionen `is False` passerade
alltså garanterat, oavsett databasens verkliga tillstånd — testet bevisade ingenting om sin
egen premiss. Rättat till `to_regprocedure('public.storage_key_still_referenced_global
(text)') IS NOT NULL`, med en ny explicit `True`-kontroll efter uppgraderingen till 0020 också
(tidigare bevisades detta bara indirekt via `has_function_privilege`, som visserligen skulle
kasta ett SQL-fel om funktionen saknades, men aldrig kontrollerades explicit).

**3. Duplicerad ImportJobStatus-lista i statusdrifttestet.** `test_import_job_status_policy_
matches_the_documented_contract_for_every_status` skrev sin egen `pending/running/blocked/
partial`-if/elif-kedja som förväntanslogik — strukturellt okopplad från den verkliga policyn,
så en framtida status som läggs till i workerns faktiska återupptagningsvägar utan en
motsvarande uppdatering HÄR hade fortfarande kunnat passera. Löst genom nya kanoniska
konstanter/predikat i `app/models/import_job.py`: `CLAIMABLE_IMPORT_JOB_STATUSES` (vad
`claim_next_job`, app/jobs/lease.py, plockar upp ovillkorligt), `PROVIDER_REQUEUE_STATUSES`
(vad `_requeue_blocked_jobs`, app/worker.py, kör tillbaka till `pending`),
`import_job_requeue_eligible()` och `import_job_still_needs_raw_blob()` (den senare är den
policy migration 0020:s SQL implementerar i lås). Både `claim_next_job`s och
`_requeue_blocked_jobs`s SQL bygger nu sina `WHERE`-villkor från dessa konstanters faktiska
strängvärden (`ANY(:claimable_statuses)`/`ANY(:requeue_statuses)`) istället för hårdkodade
literaler — inga dubbla statussträngar kvar i worker/lease-lagret. Testet importerar nu
`import_job_still_needs_raw_blob()` direkt istället för att skriva om policyn för hand.

**4. Felräknad testsumma.** Pass 24:s registerinlägg skrev "127 dedikerade S1A-tester totalt
över 6 filer + 1 routertest" — men den egna nedbrytningen summerade till 40+9+17+12+2+46=126,
inte 127; "127" var egentligen totalsumman INKLUSIVE routertestet, inte antalet dedikerade
tester. Rättat i detta pass tillsammans med de nya testerna: se sammanfattningsblocket ovan
för den korrekta 130+1=131-summan.

Omverifiering: riktat regressionssvep (`test_memory_source_units.py`+`test_ensure_app_role.py`
+`test_source_purge.py`+`test_migration_roundtrip.py`+`test_worker.py`+
`test_provider_verification.py`+`test_media_import.py`+`test_library_routes.py`+
`test_library_import.py`+`test_memory_source_backfill.py`+`test_claims.py`+
`test_storage_local_fs.py`) 287/287, bare-DB-migrations-round-trip (`upgrade head` →
`downgrade -1` → `upgrade head`, genom migration 0020) mot en färsk `postgres`-superuser-
databas (`lifeos_bare_check_p25`, ingen `mainai_app`-roll) ren. Hela backend-/security-/
account-sviten och CI-verifiering på exakt slutlig head-SHA: se nästa uppdatering av detta
register eller PR #31:s beskrivning för det slutgiltiga resultatet. Grundaren var explicit:
INGEN konto-integration, produktionsprofil, produktionsbackfill, merge eller deploy förrän en
fräsch granskning godkänner detta.

## Pass 24 (2026-07-30): PR #31 — SECURITY DEFINER-verifiering + mixed-version boot window stängt

Grundaren bekräftade att Pass 23:s cross-owner-lösning var korrekt i sak, men hittade två
kvarstående privilegieblockerare och begärde en policy-driftkontroll innan konto-integration
ens skulle övervägas.

**1. `s1a_privilege_policy.py` verifierade aldrig `pg_proc.prosecdef`.** Den kontrollerade
ägare, `BYPASSRLS`, `search_path` och grants för varje S1A-funktion, men läste aldrig om
funktionen faktiskt ÄR `SECURITY DEFINER`. En `ALTER FUNCTION ... SECURITY INVOKER` hade
passerat ALLA andra kontroller tyst — och funktionen skulle då köra med ANROPARENS
(`mainai_app`s) privilegier/RLS-scope istället för den ägande rollens, vilket tyst
återinförde exakt den cross-owner-bugg Pass 23 stängde. `_FUNCTIONS`-listan utökades till
4-tupler med en `expected_return_type`; policyn kräver nu `prosecdef = true`, rätt
returtyp och `plpgsql` som språk för varje hanterad funktion, och boot-verifieringen
misslyckas högljutt om något av detta inte stämmer. Verifierat med en RIKTIG
`ALTER FUNCTION public.storage_key_still_referenced_global(text) SECURITY INVOKER` mot
databasen — `apply_and_verify` misslyckas, återställdes till `SECURITY DEFINER`, passerar
igen. Ett separat test verifierar att en felmonkeypatchad förväntad returtyp (text istället
för boolean) upptäcks, inte tyst accepteras.

**2. Ett "mixed-version boot window" mellan migration 0019 och 0020.** `s1a_objects_exist()`
kräver nu ALLA S1A-objekt, inklusive migration 0020:s funktion. `ensure_app_role.py` gjorde
`GRANT ALL ON ALL TABLES` → kontrollerade `s1a_objects_exist()` → smalnade av ENDAST om sant
→ commitade. Vid en rullande driftsättning där databasen fortfarande är på 0019 men en
`RUN_MIGRATIONS=false`-worker kör kod som redan känner till 0020, är grinden False ENBART
för att 0020:s funktion saknas — så `ensure_app_role` hoppade över omsmalningen HELT
(inklusive de 0019-objekt som FANNS och redan var smala), och den breda `GRANT ALL`
committades som bestående tillstånd. En äldre backend-instans kunde då fortsätta betjäna
trafik genom den nu breddade delade `mainai_app`-rollen.

Löst genom en `require_complete`-flagga genom `apply_privilege_policy()`:
`ensure_app_role.py` (varje boot) anropar den nu med `require_complete=False` —
omsmalnar OVILLKORLIGT vilken delmängd av skyddade tabeller/funktioner som än existerar just
nu, i SAMMA transaktion som sin egen `GRANT ALL`, medan ett legitimt saknat FRAMTIDA objekt
(före sin egen migration) inte längre blockerar omsmalningen av det som redan finns.
`apply_runtime_privileges.py` (körs efter `alembic upgrade head`) behåller
`require_complete=True` och vägrar committa NÅGOT om det aktuella head-objektsettet är
ofullständigt — om databasen redan påstår sig vara på revision 0020 men funktionen saknas,
misslyckas den utan att committa breda privilegier.

**Tre nya testscenarier (en kombinerad testfunktion mot den delade sessions-scopade
testdatabasen, med samma `try/finally: återställ till head`-disciplin som
`test_migration_roundtrip.py`):** A — migrera databasen till 0019, kör den nya
`ensure_app_role`-logiken (som redan känner till 0020), verifiera att
`memory_source_units`/`document_source_units`/`lifecycle_events` fortfarande har exakt minsta
privilegium och att INGEN bred grant committerades trots den saknade 0020-funktionen; B —
simulera en `RUN_MIGRATIONS=false`-worker på 0019, `apply_runtime_privileges` MÅSTE
misslyckas eftersom aktuellt head saknas, men privilegietillståndet på 0019-tabellerna
förblir smalt; C — uppgradera till 0020, `apply_runtime_privileges` passerar och beviljar
`EXECUTE` på exakt `public.storage_key_still_referenced_global(text)`.

**Schema-kvalificering överallt:** `CREATE OR REPLACE FUNCTION
public.storage_key_still_referenced_global(...)`, motsvarande `REVOKE`/`DROP FUNCTION` i
migration 0020, och `blob_references.py`s anropande SQL — ingen funktionsupplösning ska
någonsin bero på anropande sessions `search_path`.

**Policy-driftkontroll:** migration 0020:s SQL hårdkodar samma statussträngar som Pythons
`RESUMABLE_INDEX_STATUSES` (`app.models.document`) — SQL kan inte importera en Python-mängd,
så det enda skyddet mot att de glider isär är ett uttömmande test som jämför OBSERVERAT
SQL-beteende mot Python-kontraktet för varje nuvarande enum-värde. Två nya tester i
`test_source_purge.py` itererar över varje `IndexStatus`- respektive `ImportJobStatus`-värde
och jämför `storage_key_still_referenced_global()`s faktiska purge-blockeringsbeslut mot
kontraktet — dessa misslyckas automatiskt nästa gång Python-listan ändras utan en
motsvarande migrations-/SQL-uppdatering.

**`test_migration_roundtrip.py`s schemasnapshot fördjupades ytterligare** (Pass 23 lade bara
till namn+signatur): varje funktions fingeravtryck inkluderar nu också returtyp,
`prosecdef`, `proconfig` (search_path), språk, och en md5 av `pg_get_functiondef()` (hela den
kanoniska CREATE-satsen, kroppen inkluderad) — så "schemat återställdes exakt" faktiskt
betyder att SECURITY-egenskaperna kom tillbaka också, inte bara att en likadant namngiven
funktion dök upp igen.

Omverifiering: riktat regressionssvep (`test_source_purge.py`+`test_ensure_app_role.py`+
`test_memory_source_units.py`+`test_migration_roundtrip.py`+`test_library_routes.py`+
`test_library_import.py`+`test_memory_source_backfill.py`+`test_claims.py`+
`test_storage_local_fs.py`) 227/227, hela backend-/security-/account-sviten 682 passed/1
avsiktligt överhoppad/0 failed (210.04s, exakt +5 över Pass 23:s 677), bare-DB-migrations-
round-trip (`upgrade head` → `downgrade -1` → `upgrade head`, genom migration 0020) mot en
färsk `postgres`-superuser-databas (`lifeos_bare_check_p24`, ingen `mainai_app`-roll) ren.
Tre separata, avgränsade commits (privilegiepolicy/mixed-version-boot-window-fix +
schema-kvalificering; tester; detta registerinlägg), pushade — kod-/testhead `6746da3`,
docs-head `794aea7`. **CI verifierad grön ("All required checks passed", `conclusion:
success`) på exakt head-SHA `794aea7` direkt via GitHubs check-runs-API** — alla obligatoriska
jobb (backend unit/integration, konto-livscykel, RLS/session-security, E2E×2,
migrationskontroll, frontend) `success`. PR #31:s beskrivning uppdaterad till att matcha
(Round 11/Pass 24, korrigerade testantal 127+1=128, ny "Verified, not assumed"-sektion).
Grundaren var explicit: INGEN konto-integration, produktionsprofil, produktionsbackfill,
merge eller deploy förrän en fräsch granskning godkänner detta.

## Pass 23 (2026-07-30): PR #31 — cross-owner RLS-lucka i blobreferenskontrollen stängd

Grundaren bekräftade att Pass 22:s advisory-lock, audit-transaktion och statuspolicy var
korrekta, men hittade en BLOCKERANDE lucka: `storage_key_still_referenced()` körde vanliga
ORM-frågor mot `documents`/`knowledge_import_jobs` — båda tabellerna har `FORCE ROW LEVEL
SECURITY` med ägar-scopade policies (`uploaded_by`/`owner_id = app.current_user_id`). Men
bloblagringen är GLOBAL och innehållsadresserad: två olika ägares byte-identiska
uppladdningar delar exakt samma `storage_key`. En källradering i ägare A:s session kunde
därför strukturellt inte se ägare B:s levande dokument eller väntande/körande/blockerade
importjobb som delade samma nyckel — A:s purge kunde radera en blob B fortfarande behövde,
med RLS själv som anledningen till att faran var osynlig för just den kontroll som skulle
förhindra den.

**Lösningen är INTE `SET row_security = off`** i anropande session — enligt Postgres egen
dokumentation (och enligt projektets egen tidigare etablerade precedens, migration 0019:s
`transition_memory_source_admin`/`erase_owner_memory_admin`) ger den inställningen INTE en
icke-undantagen roll någon åtkomst RLS annars skulle neka; den gör bara ett annars tyst
filtrerat resultat till ett fel istället. Den enda riktiga vägen att se över alla ägare
trots FORCE RLS är en roll som genuint har `BYPASSRLS` (eller är superuser) — exakt vad
migrations-/adminrollen redan har, redan verifierad av `apply_runtime_privileges.py` för de
två befintliga `*_admin`-funktionerna.

**`migration 0020_storage_key_reference_check.py`** lägger till
`storage_key_still_referenced_global(text) RETURNS boolean`:
- `SECURITY DEFINER`, ägd av migrations-/adminrollen (verifierad `BYPASSRLS`, samma mönster
  som de befintliga admin-funktionerna),
- `SET search_path = pg_catalog`, alla relationer `public.`-kvalificerade,
- kontrollerar över ALLA ägare: levande `documents.storage_key`, samt
  `knowledge_import_jobs.source_storage_key` enligt EXAKT samma runnable/resumable-policy
  som Pass 22 redan implementerade (pending/running/blocked, partial+blocked_count>0, eller
  ett terminalt jobb med en levande resumable syskondokument — matchat mot
  `app/worker.py`s faktiska `_reconcile_orphaned_documents`-logik, inte gissat),
- returnerar ENDAST en boolean — inget ägar-, dokument- eller jobb-ID läcker någonsin
  tillbaka till anroparen,
- `REVOKE ALL FROM PUBLIC` i migrationen; `EXECUTE` till `mainai_app` ges ENDAST via
  `backend/scripts/s1a_privilege_policy.py` (samma mönster som övriga S1A-funktioner —
  aldrig en bokstavlig `GRANT ... TO mainai_app` i själva migrationen, eftersom det skulle
  slå sönder "Backend — Alembic migration check"-jobbet i CI, vars databas aldrig skapar den
  rollen).

`s1a_privilege_policy.py`s `_FUNCTIONS`-lista fick en ny post — den ENDA posten som är BÅDE
beviljad till `mainai_app` OCH kräver `BYPASSRLS`, medvetet: till skillnad från de
ägar-scopade funktionerna behöver den se ALLA ägares rader (inget eget ägarskapstest); till
skillnad från de rena admin-funktionerna MÅSTE `mainai_app` kunna anropa den (den körs från
en vanlig ägar-scopad request, inte en admin-väg) — säkert eftersom den bara returnerar en
boolean.

`app/rag/blob_references.py::storage_key_still_referenced()` delegerar nu helt till denna
SQL-funktion istället för att fråga de RLS-scopade tabellerna direkt.
`acquire_storage_key_lock()` schema-kvalificerades (`pg_catalog.pg_advisory_xact_lock`/
`pg_catalog.hashtextextended`) för konsekvens.

**11 nya cross-owner-tester** (alla genom den RIKTIGA `mainai_app`-bundna sessionen, RLS
inkluderat, INTE avstängt för testet): en annan ägares levande dokument, väntande/körande/
blockerade/partial+blocked_count-importjobb, terminalt jobb med kontra utan resumable
syskondokument, sista globala referensen försvinner och tillåter purge, `mainai_app` kan få
en boolean över ägargränser men kan fortfarande inte läsa en annan ägares rader via en vanlig
fråga i samma session, `PUBLIC` saknar `EXECUTE`, och en felkonfigurerad ägare utan
`BYPASSRLS` upptäcks av `apply_runtime_privileges.py` (samma mönster som
`test_memory_source_units.py`s befintliga `transition_memory_source_admin`-test).

**En verklig bugg i testinfrastrukturen upptäcktes och åtgärdades under omverifieringen**:
`tests/backend/test_migration_roundtrip.py`s schemasnapshot jämförde bara tabellkolumner och
enum-etiketter — migration 0020 är rent funktions-additiv (ingen ny/ändrad tabell eller
enum), så snapshotet var fullständigt blint för den. `downgrade -1` tog faktiskt bort
funktionen, men "före"- och "efter downgrade"-snapshoten jämfördes identiska, vilket tyst
slog ut testets egen `"downgrade -1 must actually change the schema, not silently no-op"`-
assertion. Fixat genom att även fingeravtrycka `public`-schemats funktioner (namn +
argumentsignatur), med undantag för funktioner ägda av en installerad EXTENSION
(`pg_depend.deptype='e'` — pgvectors egna funktioner som `array_to_vector`/`avg(vector)`
installeras i `public` men rörs aldrig av någon migrations upp/ner och ska inte räknas som
kvarvarande applikationsschema efter en fullständig `downgrade base`).

Omverifiering: `test_source_purge.py` 42/42, `test_migration_roundtrip.py` 2/2 (båda testerna,
inklusive den striktare `downgrade base`-varianten), regressionssvep 92/92
(`test_source_purge.py`+`test_migration_roundtrip.py`+`test_memory_source_units.py`+
`test_ensure_app_role.py`), hela backend-/security-/account-sviten 677 passed/1 avsiktligt
överhoppad/0 failed (208.21s, exakt +11 över Pass 22:s 666), bare-DB-migrations-round-trip
(`upgrade head` → `downgrade -1` → `upgrade head`, inklusive migration 0020) mot en färsk
`postgres`-superuser-databas ren. Tre separata, avgränsade commits (cross-owner-fix,
cross-owner-tester, test-infrastruktur-fix), pushade. **CI verifierad grön ("All required
checks passed", `conclusion: success`) på exakt head-SHA `ac92b36` direkt via GitHubs
check-runs-API** — alla obligatoriska jobb (backend unit/integration, konto-livscykel,
RLS/session-security, E2E×2, migrationskontroll — som kör exakt den fixade
`test_migration_roundtrip.py` — frontend) `success`. PR-beskrivningen uppdaterad till att
matcha.

## Pass 22 (2026-07-30): PR #31 — ImportJob som blob-referens, upload/purge-race, audit-atomicitet

Grundaren bekräftade att Pass 21:s tvåfasfix var korrekt och att testantalet 97 nu gick ihop
(39+9+17+12+2+18=97), men fann två kvarstående, verkliga integrationsluckor innan
konto-integration kunde påbörjas:

**1. `ImportJob.source_storage_key` var inte en känd blob-referens.** `maybe_purge_blob()`
(anropad av `retry_source_blob_purge()`) kontrollerade bara levande `Document.storage_key`-
rader. Men den råa uppladdningen ett `ImportJob` håller kvar durabelt (`app/worker.py`s
pollningsloop öppnar den själv, inte requesten som skrev den) delar samma content-adresserade
`storage_key` som en identisk enskild fil. Scenario: en ny import lagrar sin råfil och väntar
på workern; ett äldre, innehållsidentiskt dokument raderas; blobpurgen ser inget levande
`Document` och raderar filen — trots att den väntande importjobbets `source_storage_key`
fortfarande pekar på den.

**Löst genom `app/rag/blob_references.py`** (ny, kanonisk, delad av både uppladdningsvägen och
fas B): `storage_key_still_referenced()` kontrollerar nu även `ImportJob`-status mot de
faktiska återupptagningsvägarna i `app/worker.py` — inte gissat:
- `pending`/`running`/`blocked` blockerar alltid,
- `partial` med `blocked_count > 0` blockerar (samma fråga som `_requeue_blocked_jobs`
  använder, inklusive 2026-07-28-incidenten den dokumenterar),
- ett terminalt jobb (`completed`/`partial`/`failed`) blockerar OCKÅ om någon av dess EGNA
  levande `Document`-rader fortfarande sitter fast i `RESUMABLE_INDEX_STATUSES` — exakt samma
  villkor `_reconcile_orphaned_documents` använder för att återställa jobbet till `pending`,
  eftersom en enda ZIP-import kan producera flera dokument och radering av ett redan färdigt
  syskon inte får förstöra bloben ett annat, fortfarande fastkört syskon behöver.
- ett `cancelled`-jobb, eller ett terminalt jobb utan något fastkört dokument, blockerar inte.

**2. TOCTOU-race mellan uppladdning och purge.** `POST /api/library/import` skriver bloben
fysiskt till disk INNAN någon databasrad refererar den (content-addressing gör att nyckeln
inte ens är känd förrän bytes är hashade) — ett samtidigt `retry_source_blob_purge()`-anrop
kunde köra sin referenskontroll och radera filen i exakt det fönstret, innan `ImportJob`-raden
committats.

**Löst genom `acquire_storage_key_lock()`** (samma modul): ett transaktionsbundet Postgres
advisory lock (`pg_advisory_xact_lock`, inte Redis/threading — fungerar mellan processer,
frigörs automatiskt vid commit/rollback). Både uppladdningsvägen (efter `write_stream()`, före
`ImportJob`-skapandet) och `retry_source_blob_purge()` tar samma lås före sin egen
kontrollera-sedan-agera-sekvens — den som kommer först hinner committa eller rulla tillbaka
helt innan den andra sidans kontroll ens körs. Uppladdningsvägen verifierar att bloben
fortfarande finns EFTER låset tagits; om den försvunnit misslyckas uppladdningen med 409 utan
att skapa någon `ImportJob`-referens till en saknad fil (det finns ingen säker väg att skriva
om originalbytes i efterhand — strömmen är redan fullständigt läst och kastad).

**3. Revisionsposten skrevs i en separat, senare commit.** Båda routrarna körde
`purge_source()` (redan committad) och anropade DÄREFTER `record_audit()`, som gör sin EGEN
commit. Ett fel i den andra committen kunde ge klienten ett 500-svar trots att dokumentet
redan var permanent raderat — ett omförsök gav sedan 404 ("redan raderat").

**Löst genom att flytta revisionsskrivningen in i fas A:s egen transaktion:**
`app/audit.py::record_audit()` fick en `commit: bool = True`-parameter (`False` lägger bara
till raden i sessionen, utan egen commit) och en `ip_address: str | None`-parameter separat
från `request: Request | None`, så att domänlagret (`purge_source()`) kan ta emot ett neutralt
IP-strängvärde routern extraherat, istället för att importera `fastapi` självt.
`purge_source()` skriver nu `source_purged`-revisionen med `commit=False` precis innan sin
egen `db.commit()` — ett fel där rullar tillbaka HELA fas A, inte bara revisionsraden.

**14 nya tester**: varje relevant `ImportJob`-status som blockerar/inte blockerar blobpurge
(pending/running/blocked/partial±blocked_count), det icke-uppenbara fallet med ett terminalt
jobb + ett fastkört syskondokument (kontra ett terminalt jobb utan något fastkört), att en
orelaterad nyckel aldrig blockerar en annan, ett bevis på att `maybe_purge_blob()` delegerar
till den delade policyn istället för att duplicera den, en RIKTIG tvåtrådars/tvåkopplings-
reproduktion av upload/purge-racet via det faktiska Postgres-advisory-låset (inte en mockad
timer), ett HTTP-nivå-409-bevis i `test_library_routes.py`, ett tvingat revisionsfel som
bevisligen rullar tillbaka HELA fas A (dokument/chunks/MSU oförändrade, `storage.delete()`
aldrig anropad), och exakt en revisionsrad per HTTP-rutt vid en lyckad radering.

Omverifiering: `test_source_purge.py` 31/31, regressionssvep över `test_library_routes.py` +
`test_library_import.py` + `test_memory_source_units.py` + `test_memory_source_backfill.py` +
`test_claims.py` + `test_storage_local_fs.py` = 200/200, hela backend-/security-/account-
sviten 666 passed/1 avsiktligt överhoppad/0 failed (221.49s, exakt +14 över Pass 21:s 652),
bare-DB-migrations-round-trip mot en färsk `postgres`-superuser-databas ren (ingen ny migration
— ren applikationskod). Tre separata, avgränsade commits (`94fb325` blob-referens/lås,
`c76af35` tester, `56e74e3` registerdokumentation), pushade. **CI verifierad grön ("All
required checks passed", `conclusion: success`) på exakt head-SHA `56e74e3` direkt via GitHubs
check-runs-API** — alla obligatoriska jobb (backend unit/integration, konto-livscykel,
RLS/session-security, E2E×2, migrationskontroll, frontend) `success`. PR-beskrivningen
uppdaterad till att matcha.

## Pass 21 (2026-07-30): PR #31 — purge_source() delad i atomisk DB-fas + återförsökbar blob-fas

Grundaren bekräftade att den gemensamma raderingsvägen och lifecycle-ordningen i Pass 20 var
korrekt implementerad, men hittade en verklig blockerare: `purge_source()`s egen docstring
påstod att HELA operationen (databas + filsystem) var atomisk, vilket aldrig stämde.
`LocalFilesystemStorage.delete()` gör en riktig, omedelbar `unlink()` UTAN ångra-möjlighet,
men kördes FÖRE `purge_source()`s `db.commit()`. Felscenario: (1) filen tas bort från disk,
(2) `db.commit()` misslyckas, (3) `db.rollback()` återställer dokumentet/chunks/aktiva MSU-
rader, (4) dokumentet är åter levande i databasen men originalfilen är permanent borta.
Grundaren påpekade även att statusen `failed` beskrevs som återförsökbar men att
`purge_source()` bara accepterade dokument med `deleted_at IS NULL` — ett nytt anrop på ett
redan (misslyckat) raderat dokument gav bara `SourcePurgeNotFoundError`/404, ingen verklig
återförsöksväg fanns.

**Löst genom att dela operationen i två tydligt separata faser:**
- **Fas A — `purge_source()`, verkligen atomisk, endast databas.** Låser dokumentraden,
  purgar varje `MemorySourceUnit`, hårdraderar `DocumentChunk`-raderna, soft-deletar
  dokumentet, sätter `deletion_status='pending'` (eller `'purged'` direkt om dokumentet
  saknar `storage_key` — inget att purga) — committar, eller vid fel: rullar tillbaka till ett
  läge där INGENTING ändrats och originalbloben fortfarande ligger kvar exakt där den var.
  `storage.delete()` anropas ALDRIG någonstans i den här fasen.
- **Fas B — `retry_source_blob_purge()`, ny, idempotent, oberoende återförsökbar funktion.**
  Körs bara mot ett dokument fas A REDAN committat som soft-deletat. Kontrollerar på nytt om
  någon annan levande dokumentrad delar samma innehållsadresserade `storage_key` (samma
  `maybe_purge_blob`-logik som tidigare kördes inline i fas A), och antingen lämnar
  `pending` (fortfarande delad) eller anropar `storage.delete()` och committar
  `purged`/`failed` i en egen, separat transaktion. Säker att anropa hur många gånger som
  helst: `LocalFilesystemStorage.delete()` använder `Path.unlink(missing_ok=True)`, så en
  omradering av en redan borttagen fil är ett no-op, inte ett fel.

`purge_source()` gör fortfarande ETT direkt bästa-försök på fas B omedelbart efter att fas A
committat (den vanliga vägen purgar alltså fortfarande bloben i samma request) — men ett
fas B-fel fångas, loggas, och rullar ALDRIG tillbaka den redan beständiga fas A-purgen.
`retry_source_blob_purge()` är inte kopplad till någon ny HTTP-rutt i den här PR:n
(medvetet avgränsat, en framtida ops/admin-trigger).

**3 nya tester** bevisar det exakta felscenariot grundaren beskrev: (1) ett DB-commitfel under
fas A lämnar bloben orörd OCH bevisar att `storage.delete()` aldrig ens anropades (spårat via
en anropsräknande patch, inte bara "filen finns kvar"), (2) en lyckad fas A + ett lagringsfel
lämnar `deletion_status='failed'` med DB-purgen intakt, och en efterföljande
`retry_source_blob_purge()` lyckas, (3) den exakta racen — fysisk radering lyckas men
statuscommitten misslyckas — reproducerad direkt: filen är bevisligen borta innan den
simulerade commitfelet, ett nytt återförsök felar inte på den redan saknade filen och når
`purged`. Det befintliga delad-blob-testet uppgraderades till att använda en riktig fil på
disk och verifiera både överlevnad (fortfarande refererad) och faktisk radering (via
`get_storage().exists()`) efter att den sista levande referensen försvunnit.

Omverifiering: `test_source_purge.py` 18/18, ingen regression i övriga S1A-filer eller
`test_storage_local_fs.py` (186 tester tillsammans), hela backend-/security-/account-sviten
652 passed/1 avsiktligt överhoppad/0 failed (211.68s), bare-DB migrations-round-trip mot en
färsk `postgres`-superuser-databas ren (ingen ny migration — ren applikationskod). Tre
separata, avgränsade commits (`985da3b` tjänst, `027aa37` tester, `a388507`
registerdokumentation), pushade. **CI verifierad grön ("All required checks passed",
`conclusion: success`) på exakt head-SHA `a388507` direkt via GitHubs check-runs-API** — alla
obligatoriska jobb (backend unit/integration, konto-livscykel, RLS/session-security, E2E,
migrationskontroll, frontend) `success`. PR-beskrivningen uppdaterad till att matcha (se PR
#31 direkt, inte denna sammanfattning, för den fullständiga aktuella texten).

## Pass 20 (2026-07-30): PR #31 — delad purge_source()-tjänst för library.py och documents.py

Grundaren godkände Pass 19:s tre kodfixar (rollback, argumentvakter, version-integritet) som
korrekta, påpekade att den fjärde punkten (produktionsrapportering) skulle beskrivas som
DESIGNAD, inte implementerad, och att PR-beskrivningens "96 tests" inte gick ihop med sin egen
uppräkning. Efter att PR-beskrivningen rättats (se ovan) beställde grundaren nästa isolerade
S1A-slice: en gemensam `purge_source()`-tjänst enligt §4.8:s "En gemensam purge-tjänst",
använd av BÅDA raderingsvägarna.

**`app/rag/source_purge.py::purge_source(db, document_id, owner_id)`** — en domänservice, inte
routerlogik:
- Verifierar dokumentägarskap explicit (`Document.uploaded_by == owner_id`), utöver RLS —
  stänger en tidigare odokumenterad lucka i `documents.py`s gamla implementation, som aldrig
  kontrollerade ägarskap alls.
- Låser `Document`-raden (`FOR UPDATE`) — den verkliga serialiseringspunkten mot samtidiga
  raderingsförsök av samma källa.
- För varje `document_source_units`-rad som hör till dokumentet: en `active`/`revoked`
  `memory_source_units`-rad övergår till `purged` via `transition_own_memory_source()` (aldrig
  en direkt `UPDATE`), en redan-`purged` rad hoppas över (idempotent no-op — ett andra
  `purged -> purged`-anrop skulle annars få funktionen att resa "illegal transition").
- FÖRST DÄREFTER hårdraderas `DocumentChunk`-raderna — ordningen är inte godtycklig:
  `trg_dsu_guard_update` (migration 0019) tillåter bara att `document_source_units.chunk_id`
  nollas (vilket `DocumentChunk`s `ON DELETE SET NULL` utlöser) när förälderns
  `lifecycle_status` INTE längre är `active`. Ett direkt bevis på detta lades till som ett eget
  test: att radera chunken FÖRE purge av en `active` förälder avvisas av triggern med "chunk_id
  cannot be cleared while parent is active".
- `KnowledgeClaim`/`memory_source_units`/`document_source_units`/
  `memory_source_lifecycle_events`-rader raderas ALDRIG av den här funktionen — en
  medveten, grundarbekräftad avvikelse från §4.8:s ursprungliga per-dokument-purge-steg (som
  skulle ha raderat claims), specifikt för källradering (till skillnad från full
  kontoradering, som förblir `erase_owner_memory()`s ansvar, orört här).
- Ett dokument utan några `document_source_units`-rader alls (aldrig backfillat/dual-writat)
  hanteras via `legacy_without_memory_source` i resultatet — ingen source unit fabriceras.
- Atomisk: explicit `try/except/rollback` (samma disciplin som `account.py`s `delete_account`),
  återanvänder befintlig `maybe_purge_blob()`/referensräkningslogik oförändrad.

**Router-omskrivning**: `library.py`s `delete_source` är nu en tunn wrapper (validerar
`confirm`, anropar tjänsten, loggar audit). `documents.py`s `delete_document` byter från ett
hårt `db.delete(document)` till samma tjänst — en AVSIKTLIG beteendeförändring: migration
0019:s `document_source_units.document_id`-FK (ingen `ON DELETE`-åtgärd) skulle annars
RESTRICT-blockera den gamla hårda raderingen så fort ett memory_source_units-objekt finns för
dokumentet. Ingen dubblerad cleanup-kod kvar i någon router.

**15 nya tester** (`test_source_purge.py`) täcker exakt grundarens 15-punktslista: aktiv/
revoked/redan-purgad källa, flera claims som delar en källa, flera chunks+en document_record-
källa i samma dokument, claims+lifecycle-events överlever, content/hash/version nollas,
chunk_id nollas först efter purge (plus den omvända regressionen: fel ordning avvisas av
triggern), cross-owner nekas, dokument utan memory source (legacy), ett simulerat DB-fel som
rullar tillbaka allt, en blob som delas mellan två dokument som INTE raderas, en
lagringsfel-simulering som lämnar en återförsökbar `deletion_status`, och ett HTTP-nivå-test
som bevisar att båda `/api/library`- och `/api/documents`-rutterna producerar identiska utfall
via samma tjänst.

Omverifiering: `test_source_purge.py` 15/15, ingen regression i `test_library_routes.py`
(31/31) eller övriga S1A-filer (173/173 tillsammans), hela backend-/security-/account-sviten
649 passed/1 avsiktligt överhoppad/0 failed (258.73s), bare-DB migrations-round-trip mot en
färsk `postgres`-superuser-databas ren (ingen ny migration i detta pass — ren applikationskod).
Två separata, avgränsade commits (`007a136` tjänst+routrar, `8352fcf` tester), pushade. CI-
kontroll mot exakt ny head — se PR-beskrivningen för slutstatus.

## Pass 19 (2026-07-29): PR #31 — fyra integrationsproblem i backfill/dual-write, alla åtgärdade

Grundaren bekräftade att Pass 18:s backfill-/dual-write-kärna (fail-closed-exkludering,
atomisk commit, `FOR UPDATE SKIP LOCKED`, MSU-skapande efter första parsade claimet,
providerfel/tom-output skapar ingen MSU) i stort sett var korrekt, men hittade tre verkliga
integrationsproblem plus ett fjärde krav innan produktionskörning:

1. **`library_import.py` svalde dual-write-fel utan rollback.** Båda anropen till
   `extract_claims_for_document` fångade `Exception` och gjorde bara `pass` — nu när
   extraction även flushar MSU/DSU-rader kunde ett fel efter flush men före commit lämna
   ocommittade writes i sessionen, som ett SENARE `db.commit()` i samma worker-session kunde
   råka committa, eller lämna sessionen i `PendingRollback`-läge. Åtgärdat: båda call sites
   (`_import_one_file` och `_resume_incomplete_document`) kör nu `db.rollback()` + loggar
   innan de fortsätter — indexeringen (redan committad) påverkas inte, claim-extraktion
   förblir best-effort. Två nya integrationstester driver HELA `run_import_job`-vägen (första
   importen respektive återupptagen import via en dokumentrad fastnad i en
   `RESUMABLE_INDEX_STATUSES`-status) med en fejkad `extract_claims_for_document` som
   verkligen flushar en MSU/DSU och SEDAN kraschar — bevisar att inga MSU/DSU/claims
   committas, att indexeringen består, att sessionen kan göra en ny fråga/commit efteråt, och
   att importen ändå rapporteras `indexed`.
2. **Backfillen kunde fortfarande loopa oändligt.** `_apply()`s `for _ in range(batch_size)`
   blir en tom loop om `batch_size <= 0` — `exhausted` förblir `False` för evigt och den yttre
   `while`-loopen (med `max_batches=None`, det gamla standardvärdet) fortsätter i all
   oändlighet. Exakt samma felklass som redan kostat timmar en gång. Åtgärdat: `batch_size`
   och `max_batches` valideras nu explicit (`ValueError` vid `<= 0`), och standardvärdet för
   `max_batches` är nu ett ändligt `DEFAULT_MAX_BATCHES = 10` istället för `None` — en
   anropare som verkligen vill köra klart måste uttryckligen skicka `max_batches=None`. Nya
   tester bevisar både valideringen och att standardkörningen faktiskt är begränsad.
3. **Dual-write verifierade aldrig `version_id`.** `extract_claims_for_document` skrev en
   anropar-given `version_id` direkt på varje claim utan att kontrollera att versionen
   strukturellt hör till samma dokument/ägare — `knowledge_versions` har bara en enkel FK mot
   `documents.id`, ingen kompositkoppling som `memory_source_units` har. Åtgärdat: en ny
   `ClaimExtractionIntegrityError` reser sig, INNAN något providersanrop eller någon skrivning,
   om `document.uploaded_by != owner_id` eller om en given `version_id` inte har
   `source_id == document.id` och `owner_id == owner_id`. Fyra nya tester: version från ett
   annat dokument, version från en annan ägare, dokument som inte ägs av den givna
   `owner_id`, och den positiva motsvarigheten (en verkligt matchande version accepteras) —
   alla med en providermock som reser ett `AssertionError` om den någonsin anropas.
4. **Beständig produktionsrapportering — dokumenterad, INTE byggd.** Grundaren krävde att en
   riktig produktionskörning ska ha ett beständigt run-ID, status, räknare och
   claim-specifika fel/retries innan den körs — vanliga processloggar räcker inte. Detta är nu
   skrivet som ett explicit designavsnitt i `app/rag/memory_source_backfill.py`s
   moduldocstring, som pekar mot den redan tidigare identifierade `memory_processing_jobs`-
   planen (`app/routers/admin.py`s `trigger_claim_type_backfill`-docstring) istället för en ny
   fristående mekanism. Medvetet INTE byggd i den här PR:n — en egen, separat avgränsad PR
   krävs innan någon RIKTIG produktionsbackfill-körning, per isoleringsprincipen.

Omverifiering: `test_memory_source_backfill.py` 17/17, `test_claims.py` 51/51,
`test_library_import.py`s nya tester 2/2, hela backend-/security-/account-sviten 634 passed/1
avsiktligt överhoppad/0 failed (264.86s), bare-DB migrations-round-trip mot en färsk
`postgres`-superuser-databas ren. Tre separata, avgränsade commits (`64b7a39` library_import-
rollback, `a8e6f11` backfill-guards+designnot, `2bd3bcf` dual-write version-integritet),
pushade. CI-kontroll mot exakt ny head — se PR-beskrivningen för slutstatus.

## Pass 18 (2026-07-29): PR #31 — deterministisk backfill + dual-write, en verklig oändlig-loop-bugg hittad och fixad under egen testning

Grundaren godkände Pass 17:s grundlager ("provenance-grundlagret... tillräckligt strikt för
att gå vidare med backfill och dual-write") och beställde de två nästa S1A-slicerna:

1. **`app/rag/memory_source_backfill.py`** (ny modul): owner-scopad, batchad, idempotent,
   restart-säker backfill av `knowledge_claims.memory_source_id IS NULL`. Resolveringsordning
   exakt enligt §4.8: giltig `chunk_id` → `document_chunk`/`exact` (text läst från
   `DocumentChunk.text`), annars giltig `version_id` → `document_version`/`degraded`, annars
   `document_record`/`missing`. En `chunk_id`/`version_id` som är SATT men strukturellt
   ogiltig (fel ägare/dokument) failar closed — faller ALDRIG vidare till nästa nivå (det vore
   precis den gissning §4.8 förbjuder). Konkurrenssäkert via `SELECT ... FOR UPDATE SKIP
   LOCKED`, en claim i taget, committad atomiskt med sin source unit.
2. **Dual-write i `app/rag/claims.py`s enda claim-skrivväg**: för varje chunk som producerar
   minst en claim, en `document_chunk`-MSU med chunkens verkliga text, samma
   `memory_source_id` på alla claims från den chunken, atomiskt med claim-inserten. En chunk
   utan claims (providerfel eller tom extraktion) får ingen source unit — ingen orphan MSU
   möjlig.

**Verklig bugg hittad under egen testning, inte i granskning:** den första versionen av
`_apply()`s huvudloop exkluderade aldrig en permanent misslyckad claim (fail-closed-mismatch
eller `MemorySourceIdentityConflict`) från omval — med standardvärdet `max_batches=None`
valde loopen om SAMMA claim för evigt. Upptäcktes konkret: en bakgrundskörning av det nya
testet för denna exakta situation (en enda alltid-mismatchande claim, inget `max_batches`)
gick från att verka "hänga tyst" till att efter ~2h46m visa sig faktiskt köra oändligt (CPU-
bunden, INTE en I/O-väntan eller en förlorad process) — verifierat konkret med `ps -eo
pid,etime,stat,cmd`, inte antaget. Rättat genom att exkludera en misslyckad claims id från
återval för RESTEN av det anropet (samma mönster som `backfill_claim_types`s `failed_ids`) —
claimen är fortfarande en giltig kandidat vid nästa SEPARATA anrop. Efter fixen: alla 14
backfill-tester gröna på 8.92s, inklusive exakt den tidigare hängande situationen.

**Full omverifiering (i förgrunden, inte bakgrunden, efter incidenten ovan):**
- `test_memory_source_backfill.py`: 14/14 gröna.
- `test_claims.py` (S1A dual-write-tester + befintliga): 47/47 gröna.
- `test_memory_source_units.py` (ingen regression): 39/39 gröna.
- Hela `tests/backend` + `tests/security` + `tests/account`: **625 passed, 1 avsiktligt
  överhoppad**, 0 failed, 240.51s.
- `alembic upgrade head` / `downgrade -1` / `upgrade head` mot en färsk `postgres`-
  superuser-databas (migrationsfilen refererar aldrig `mainai_app` vid namn, verifierat med
  `grep`) — rent round-trip.

Två separata, avgränsade commits (`1fa7619` backfill, `7c60102` dual-write), pushade till
`claude/s1a-memory-source-implementation`. CI-kontroll mot exakt head `7c60102` pågick vid
tidpunkten detta pass skrevs — se PR-beskrivningen för slutstatus.

## Pass 17 (2026-07-29): PR #31 — fjärde granskningsrundan hittade 2 kvarstående problem, alla åtgärdade

Grundaren bekräftade Pass 16:s tre fixar (source_role='unknown', document_version aldrig
exact, document_chunk exact bunden till verklig chunktext, actor_kind borttaget) som korrekta,
men granskade en gång till och hittade 2 sista problem — samma explicita instruktion att
INTE fortsätta till backfill/dual-write:

1. **Hashen var fortfarande självdeklarerad vid rå DB-insert**: triggern verifierade att
   `content_text` matchade `document_chunks.text`, men läste aldrig `content_hash` och
   räknade aldrig ut SHA-256 själv — en rå insert (mainai_app har direkt `INSERT`) kunde
   alltså använda korrekt chunktext men lagra t.ex. 64 nollor som hash, vilket format-/
   versions-CHECK:arna fortfarande accepterade. Åtgärdat: `trg_dsu_validate_fields` beräknar
   nu själv `encode(sha256(convert_to(<verklig chunktext>, 'UTF8')), 'hex')` med Postgres
   egen inbyggda `sha256(bytea)` (pg_catalog, PG16+, inget pgcrypto-beroende) och kräver att
   `content_hash` matchar exakt, samt att `content_hash_version = 'sha256-utf8-v1'`. Ny test
   bevisar att korrekt chunk_id + korrekt text + en felaktig men formatgiltig 64-hex-hash
   avvisas vid commit.
2. **Actor-loggningen saknade en verklig founder-kontroll**: `transition_own_memory_source`
   loggade alltid `actor_type='founder'`, men verifierade bara att anroparen ÄGER raden —
   `users`-tabellen har även `admin`/`member` (för närvarande oåtkomliga via appens
   registreringsflöde, men kvar i schemat för den framtida UserAI-fasen). Åtgärdat:
   funktionen slår nu upp `users.role` för anroparen och NEKAR anropet om det inte är exakt
   `'founder'`, istället för att felmärka en member/admins handling som founder-utförd. Ny
   test bevisar att en `member` som äger en source ändå nekas.

Omverifiering: migrations-round-trip mot en färsk `postgres`-superuser-databas UTAN
`mainai_app`-roll; 603/604 gröna i hela backend-/security-/account-sviten (1 avsiktligt
överhoppad kapacitetstest); grön CI (18/18, "All required checks passed") på exakt head-SHA
`32a2c65`, verifierat direkt mot GitHubs check-runs-API. Två separata, avgränsade commits.
PR-beskrivningen på GitHub uppdaterad med "Review Round 3/4" och aktuella testsiffror.

## Pass 16 (2026-07-29): PR #31 — tredje granskningsrundan hittade 3 provenance-problem, alla åtgärdade

Grundaren bekräftade Pass 15:s fem fixar som korrekta, men granskade en gång till (medan
migrationen fortfarande är odriftsatt) och hittade 3 nya problem:

1. **En `exact`-snapshot var inte bunden till verklig källtext**: Python-hjälparen beräknade
   SHA-256 av caller-supplied `content_text`, vilket bara bevisar att hashen matchar den
   inskickade texten — inte att texten faktiskt kommer från den länkade `chunk_id`.
   Dessutom tillät DSU-triggern `document_version + exact` trots att `KnowledgeVersion`
   saknar en kanonisk textkolumn (bara checksum/metadata). Åtgärdat: `trg_dsu_validate_fields`
   verifierar nu, för `document_chunk + exact`, att förälderns `content_text` matchar
   `document_chunks.text` för den länkade chunk_id:n. `document_version` får aldrig längre
   vara `exact` — begränsad till `degraded`/`missing`, precis som `document_record` redan var.
2. **`content_hash_version` var fri och oskyddad av update-guarden**: ny CHECK
   `ck_msu_content_hash_version_matches_hash` (NULL endast tillsammans med `content_hash`,
   annars exakt `'sha256-utf8-v1'`), och fältet ingår nu i `trg_msu_guard_update`s
   immutabilitetsjämförelse.
3. **Owner-funktionen kunde märka användaråtgärder som `system`**: `transition_own_memory_
   source` tog emot ett fritt `p_actor_kind`. Åtgärdat: parametern helt borttagen — funktionen
   loggar nu ovillkorligt `actor_type='founder'` (härlett från att den enda vägen in är den
   egna ägarkontrollen). `downgrade()`s `DROP FUNCTION`-signatur uppdaterad i samma commit.

Omverifiering: migrations-round-trip; 601/602 gröna i hela backend-/security-/account-sviten;
grön CI (18/18) på exakt head-SHA `6b3820a`. Under körningen upptäcktes och fixades även en
riktig bugg i en BEFINTLIG test (`test_get_or_create_memory_source_unit_rejects_mismatched_
locator`) som det nya content_text-kravet avslöjade: testet skapade en chunk med text "Text A"
men byggde sin locator med hjälpfunktionens orelaterade default-text, så de aldrig matchade —
harmlöst innan denna runda (inget kontrollerade det), ett riktigt fel nu.

## Pass 15 (2026-07-29): PR #31 — andra granskningsrundan hittade 5 kvarstående problem, alla åtgärdade

Grundaren bekräftade att Pass 14:s 8 fynd var korrekt åtgärdade, men granskade koden en gång
till (medan migrationen fortfarande är odriftsatt och lätt att ändra) och hittade 5 nya
problem — med samma explicita instruktion att INTE fortsätta till backfill/dual-write ännu:

1. **`source_role` kunde bli en falsk auktoritetsclaim**: `mainai_app` har direkt `INSERT` på
   både MSU och DSU, och databasen hindrade inte att ett dokument skapades med
   `source_role='founder'` — permanent, eftersom fältet är immutable. Åtgärdat: DSU-
   valideringstriggern kräver nu att förälderns `source_role` är exakt `'unknown'` för alla
   `document_source_units`-rader. Ny test bevisar att `source_role='founder'` avvisas.
2. **Downgrade lämnade kvar en global säkerhetsändring**: migrationen körde
   `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, men `downgrade()` kunde inte återställa det
   säkert. Åtgärdat: den raden är helt borttagen ur migrationen — den levde redan dubbelt i
   `apply_runtime_privileges.py`, som är den enda platsen den nu körs.
3. **Privilegiehärdningen var varken atomisk eller ägarverifierad**: skriptet använde
   `autocommit=True` och kontrollerade bara "inte `mainai_app`", inte exakt vilken roll som
   faktiskt ägde tabellerna/funktionerna. Åtgärdat: hela REVOKE/GRANT/verifiering extraherad
   till en delad `backend/scripts/s1a_privilege_policy.py`, körd i EN transaktion, commit
   endast om verifieringen är helt grön. `ensure_app_role.py` applicerar samma policy i SAMMA
   transaktion som sin egen breda `GRANT ALL`, närhelst S1A-objekten redan finns — stänger
   fönstret mellan `ensure_app_role` och `apply_runtime_privileges` där breda rättigheter
   annars kunde bli det committade sluttillståndet vid en krasch. Ny test tvingar fram ett
   fel i omsmalningen och bevisar att HELA transaktionen (inklusive den breda GRANT-satsen)
   rullas tillbaka, inte bara det misslyckade steget.
4. **Lifecycle-CHECK:arna var inte fullständigt koherenta**: en `active`-rad kunde t.ex. bära
   ett kvarglömt `revocation_reason`. Åtgärdat: `ck_msu_lifecycle_coherence` skärpt så alla
   fyra revoke/purge-fält verifieras tillsammans per status, och `purged`-rader tillåts bevara
   `revoked_at`/`revocation_reason` bara som par (aldrig ett utan det andra).
   `memory_source_lifecycle_events.reason` är nu `NOT NULL`.
5. **Hash kunde deklareras av anroparen**: `content_hash` accepterades direkt från
   `DocumentSourceLocator`, vilket lät en anropare hävda ett overifierat hash-värde för en
   `exact`-snapshot. Åtgärdat: `content_hash`/`content_hash_version` beräknas nu internt
   (SHA-256 över exakt UTF-8-innehåll, `app/rag/memory_source.py`s `compute_content_hash`),
   med en ny DB-CHECK för 64 gemena hex-tecken. `created_at`-defaults bytta från naiv
   `datetime.utcnow()` till `server_default=func.now()`.

PR #31:s beskrivning på GitHub uppdaterad till att matcha den aktuella koden (tog bort det
felaktiga `row_security=off`-påståendet och föråldrade testsiffror).

Omverifiering: migrations-round-trip mot en färsk `postgres`-superuser-databas UTAN
`mainai_app`-roll (samma villkor som "Alembic migration check"-jobbet); 598/599 gröna i hela
backend-/security-/account-sviten (1 avsiktligt överhoppad kapacitetstest); grön CI (18/18,
"All required checks passed") på exakt head-SHA `637576c`, verifierat direkt mot GitHubs
check-runs-API. Fyra separata, avgränsade commits enligt `CLAUDE.md`s arbetsdisciplin.

## Pass 14 (2026-07-29): PR #31 — kodgranskning hittade 8 blockerande fel, alla åtgärdade

Grundaren granskade PR #31:s faktiska kod (inte bara design) och hittade 8 konkreta problem,
med explicit instruktion att stanna innan backfill/dual-write fick fortsätta:

1. **Cross-owner-FK-lucka**: `knowledge_claims.memory_source_id` hade en enkel-kolumn-FK, som
   bara bevisar att raden finns — inte att den tillhör samma ägare, eftersom FK-kontroller
   körs oberoende av RLS. Åtgärdat: sammansatt FK `(memory_source_id, owner_id)` mot
   `memory_source_units(id, owner_id)`, både i migrationen och i SQLAlchemy-modellen
   (`ForeignKeyConstraint` i `__table_args__`). Ny test bevisar att en cross-owner-referens nu
   avvisas av databasen, inte bara döljs av RLS efteråt.
2. **`mainai_app` behöll onödiga rättigheter**: `apply_runtime_privileges.py` REVOKEade bara
   UPDATE/DELETE — TRUNCATE (som INTE alls omfattas av RLS), REFERENCES och TRIGGER lämnades
   kvar. Åtgärdat: deny-by-default (REVOKE ALL, sedan explicit GRANT exakt SELECT+INSERT på
   MSU/DSU, SELECT-only på lifecycle_events), verifierat mot alla sju tabellrättigheter.
3. **Worker-omstart-bugg**: `apply_runtime_privileges.py` kördes bara inuti
   `RUN_MIGRATIONS=true`-grenen i `docker-entrypoint.sh` — worker-containern sätter
   `RUN_MIGRATIONS=false` men kör ändå `ensure_app_role.py`s ovillkorliga fullrättighets-
   återgivning. Åtgärdat: körs nu ovillkorligt på varje boot. Ny test kör det RIKTIGA
   entrypoint-skriptet via subprocess med `RUN_MIGRATIONS=false` och bevisar att rättigheterna
   ändå smalnas av.
4. **`row_security = off` gav ingen faktisk RLS-bypass**: enligt Postgres egen dokumentation
   ger flaggan bara ett fel istället för tyst filtrerat resultat — den beviljar aldrig åtkomst
   RLS annars skulle neka. Åtgärdat: borttagen från alla fyra SECURITY DEFINER-funktioner; de
   två ägar-scopade behöver ingen bypass alls (egen explicit ägarkontroll räcker), de två
   admin-funktionerna kräver nu explicit, EXTERNT verifierad BYPASSRLS/superuser på den ägande
   rollen (`apply_runtime_privileges.py`). Ny test byter faktiskt ägare på
   `transition_memory_source_admin` till en riktig `NOSUPERUSER NOBYPASSRLS`-roll och bevisar
   att verifieringen slår larm istället för att tyst lita på det gamla antagandet.
5. **Fel undantag fångades i find-or-create**: `get_or_create_memory_source_unit()` fångade
   ALLA `IntegrityError` och antog att det var `uq_msu_owner_identity`-racet. Åtgärdat:
   inspekterar `exc.orig.diag.constraint_name`, återkastar allt annat oförändrat. Utökat att
   även jämföra `content_hash` (inte bara `snapshot_status`) och att ALDRIG tyst återanvända en
   `revoked`/`purged` källa.
6. **"Samtidighets"-testet var inte samtidigt**: session A committade helt innan session B ens
   startade. Åtgärdat: nytt test håller session A:s INSERT öppet (ej committat) på huvudtråden
   medan session B kör på en bakgrundstråd, verifierat via `pg_stat_activity` att session B
   faktiskt går in i ett riktigt lock-wait innan session A committar och släpper den.
7. **Trigger-funktioner använde okvalificerade tabellnamn**: sårbart för `pg_temp`-skuggning
   eftersom Postgres alltid kollar sessionens temp-schema först, oavsett `search_path` — och
   `mainai_app` kan skapa temp-tabeller som standard. Åtgärdat: alla triggerfunktioner
   schema-kvalificerade (`public.<tabell>`) och `search_path` låst till enbart `pg_catalog`.
8. **`apply_runtime_privileges.py` behövde egen härdning**: verifierar nu funktionens
   ÄGARROLL (inte bara tabellägare), dess `rolsuper`/`rolbypassrls`, dess `search_path`/
   `proconfig`, och FALLERAR HÖGLJUTT (istället för att tyst hoppa över) om en förväntad S1A-
   tabell/funktion saknas vid den här punkten i bootsekvensen.

Under omverifieringen upptäcktes och åtgärdades även en kvarglömd `$$ LANGUAGE plpgsql;`-rad
från en tidigare redigeringsomgång i `trg_msu_guard_update`, som bröt migrationens rena
körning mot en bar `postgres`-superuser-databas (exakt "Alembic migration check"-jobbets
villkor) — fångad genom att faktiskt köra om den testen, inte anta att den fortfarande
fungerade efter de andra ändringarna.

Omverifiering: migrations-round-trip (`upgrade head` → `downgrade -1` → `upgrade head`) mot en
färsk `postgres`-superuser-databas UTAN `mainai_app`-roll; `apply_runtime_privileges.py` kört
mot samma databas efter att `mainai_app` skapats; 591/592 gröna i hela backend-/security-/
account-sviten (1 avsiktligt överhoppad kapacitetstest); grön CI (17/17) på exakt head-SHA
`7041c2c`, verifierat direkt mot GitHubs check-runs-API. Fem separata, avgränsade commits
(en per fix-område) enligt `CLAUDE.md`s arbetsdisciplin.

## Pass 13 (2026-07-29): PR #30 — SECURITY DEFINER-funktionen fick eget ägarskydd

Grundaren hittade att `transition_memory_source()` (SECURITY DEFINER, kör med admin-rollens
rättigheter) inte själv verifierade att källan den skulle övergå faktiskt tillhör
anroparen — RLS gäller inte inuti en `SECURITY DEFINER`-funktion, så `mainai_app` kunde i
princip ha övergått en ANNAN ägares `memory_source_units`-rad, och `actor_type`/`actor_id`
var fria parametrar som kunde sättas till `'admin'`/en godtycklig användare. Löst genom att
dela funktionen i två: `transition_own_memory_source()` (beviljad `mainai_app`, verifierar
`owner_id = current_user_id` FÖRST, `actor_kind` begränsad till `'founder'|'system'`,
`actor_id` härlett internt — aldrig ett parametervärde) och `transition_memory_source_admin()`
(full flexibilitet, `EXECUTE` ALDRIG beviljad `mainai_app`). `search_path` skärpt till enbart
`pg_catalog` + schema-kvalificerade objektnamn istället för `pg_catalog, public`.
`apply_runtime_privileges` utökad att verifiera hela uppdelningen (inte bara UPDATE/DELETE).
CI verifierad grön direkt via GitHub API på PR #30:s exakta head vid varje steg i den här
granskningen, inte antagen från en tidigare commit.

## Pass 12 (2026-07-29): PR #30 — reboot-persistent privilegiehärdning, CI verifierad grön

Grundaren hittade ett verkligt driftfel i privilegieplanen (Pass 11): `backend/docker-
entrypoint.sh` kör `ensure_app_role.py` (som ovillkorligt beviljar `ALL PRIVILEGES` till
`mainai_app` på VARJE boot, inte bara vid rollskapande) FÖRE `alembic upgrade head`. En
`REVOKE UPDATE, DELETE` inskriven bara i S1A:s migration skulle alltså fungera vid första
deployen men bli tyst återställd vid nästa vanliga omstart, eftersom Alembic då inte har
något nytt att köra och `REVOKE` aldrig körs om. Löst i §4.8 genom att lägga till ett fjärde
boot-steg, `apply_runtime_privileges` (idempotent, körs EFTER Alembic, FÖRE appstart, på
VARJE boot — verifierar med `has_table_privilege`/`has_function_privilege` istället för att
anta att `REVOKE`/`GRANT` lyckades, stoppar uppstarten vid avvikelse). Skrivs in i designen
nu, implementeras i S1A-implementations-PR:n tillsammans med migrationen.

Även löst: "Vad som återstår"-listan delad i två explicita trösklar (vad som krävs för att
merga PR #30 självt, kontra vad som krävs för att merga den separata, senare
S1A-implementations-PR:n — produktionsdataprofilen blockerar den senare, inte PR #30).

**CI-status verifierad direkt mot GitHub API** (`pull_request_read` med `get_check_runs`/
`get_status`) på PR #30:s exakta head vid tidpunkten (`a4f4591...`): "All required checks
passed" = success, 18/18 checks completed (VPS-specifika jobb `skipped` som väntat för en
docs-only-PR, resten `success`). Grundarens observation om avsaknad av synlig Actions-körning
var alltså en timing-fråga (körningen hann inte synas/slutföras än) — inte ett kvarstående
CI-problem.

## Pass 11 (2026-07-29): PR #30 — konsoliderad kanonisk design, tre kvarstående blockerare

`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`s §4.8 skrevs om från en punktlista över fem
sekventiella granskningsrundor (delvis motsägande — äldre `document_tombstone`/subtyp-regler
levde kvar bredvid sina ersättningar) till EN sammanhängande, aktuell design. PR #30:s
beskrivning uppdaterad på GitHub för att matcha (tog bort "Third correction round"-språk och
felaktig `knowledge_claim_evidence`-i-S1A-referens).

Under konsolideringen hittades och löstes tre ytterligare verkliga fel: (1) `document_source_
units` saknade en egen, komposit-FK-buren `source_kind`, vilket skulle fått flera purgade
`document_chunk`-rader från samma dokumentversion att kollidera i `document_version`s
partiella unika index när deras `chunk_id` nollas; (2) en `SET LOCAL`-sessionsflagga
(`memory.transition_active`/`erasure_in_progress`) hävdades vara den enda skrivvägen till
livscykelfält/radering, vilket är strukturellt falskt — vilken kod som helst på samma
DB-anslutning kan sätta samma flagga. Löst med repots REDAN EXISTERANDE rolluppdelning
(`mainai_app`, en icke-ägande runtime-roll, skild från migrationsrollen som äger tabellerna —
se `ensure_app_role.py`): `REVOKE UPDATE, DELETE` från `mainai_app` på proveniens-tabellerna,
`SECURITY DEFINER`-funktioner (`transition_memory_source`/`erase_owner_memory`) med fixerad
`search_path` och `EXECUTE` beviljad enbart till `mainai_app` — en gräns Postgres själv
upprätthåller, inte en flagga en session kan sätta. (3) Purge var ofullständig
(`MemorySourceUnit.content_text` nollades, men `KnowledgeClaim.claim_text`/`Document.
content_preview`/`media_blob`/diskblobben kunde fortfarande innehålla samma material) och det
finns en andra, fortfarande LIVE dokumentraderingsväg (`DELETE /api/documents/{id}`,
`app/routers/documents.py`, anropad från `frontend/lib/api.ts`) som skulle blockeras rakt av
S1A:s nya FK:er — båda måste konsolideras till EN delad `purge_source()`-tjänst.

**Kvarstår innan en S1A-implementations-PR (migration + kod) får öppnas:**
1. Produktionsdataprofilen (`chunk_id`/`version_id`-nullkombinationer på `knowledge_claims`)
   är fortfarande inte körd — ingen databasåtkomst från den här sessionen.
2. Den delade `purge_source()`-tjänsten, `app/rls.py`-uppdateringen, och `delete_account`/
   `export_account`-ändringarna är beskrivna i §4.8 men inte implementerade.
3. Testmatrisen (migration/dual-write/delete/kontoradering/RLS/behörighet/konkurrens) är
   specificerad men inte skriven som kod.

## Pass 10 (2026-07-28): PR #30 — fjärde granskningsrundan av MemorySourceUnit-modellen

Ytterligare åtta korrigeringar innan någon S1-migration skrivs (source_role utökad med
`system`/`unknown`, backfill defaultar till `unknown` inte `external`; `lifecycle_status`
(`active|revoked|purged`) på `memory_source_units` istället för att förlita sig på
`ON DELETE SET NULL` som enda livscykelmodell — nulägesbilden av Library-radering (soft
delete: `Document` behålls, `deleted_at` sätts, chunks raderas) korrigerad; oföränderlig
`content_text`-snapshot krävs eftersom varken `KnowledgeVersion` eller `DocumentChunk`
garanterat bevarar källtexten; deferrable constraint-triggers för en verkligt
databasupprätthållen exclusive arc; komposit-FK `(memory_source_id, owner_id)` för
ägarintegritet; `Message.sequence_number` för deterministisk ordning; `document_chunk`
kontra `document_version`/`document_tombstone` som explicit granularitet istället för ett
odifferentierat `source_kind=document`; `knowledge_claim_evidence`s roller ändrade till
`context|supports|contradicts|corroborates`, `direct` borttaget eftersom
`KnowledgeClaim.memory_source_id` redan ÄR den direkta primärkällan). Reviderad DDL
presenterad i konversationen, ännu inte skriven som Alembic-fil. Se PR #30 för den
uppdaterade designdokumentationen.

Grundaren granskade PR #29 och hittade en verklig, blockerande lucka: migration 0018 satte
alla BEFINTLIGA `knowledge_claims`-rader till `claim_type=uncategorized`, och
`_import_one_file`s dublett-kontroll kör aldrig om `extract_claims_for_document` för ett
redan-`indexed` dokument — så material importerat före P3 skulle aldrig få riktiga
`claim_type`-värden organiskt. Löst med `backfill_claim_types()` (`app/rag/claims.py`):
idempotent, omstartssäker, uppdaterar `claim_type`/`extraction_version` in place på
BEFINTLIGA rader, skapar aldrig nya — se den fullständiga kandidat-/avgränsningslogiken i
funktionens docstring. Manuellt triggerbar via `POST /api/admin/claims/backfill-types`.

**Egen bugg hittad och fixad under implementationen** (inte grundarens fynd): den första
versionen av backfill-loopen requeryade samma misslyckade batch om och om igen inom samma
anrop (ingen exkludering av redan-försökta rader vid providerfel/längdmissmatch,
`max_batches=None` som standard) — en verklig oändlig loop, bekräftad genom att testprocessen
körde 10+ minuter med växande minnesförbrukning innan den dödades manuellt och buggen
identifierades. Fixad med en `failed_ids`-uteslutning per anrop; om testet hade fått köra
längre hade det aldrig terminerat. Kvarstående lärdom: kör alltid nya loopar med en hård
timeout lokalt, aldrig obegränsat, innan de committas.

Samtidigt en större, ännu INTE implementerad arkitekturkorrigering: grundaren pekade ut att
konversationer/meddelanden (`Conversation`/`Message`) ska vara en förstklassig källa till
SAMMA minneskärna som filer — inte bara turer som Context Resolver flaggar som explicit
minne/idé (dagens P6-plan), utan hela historiken, analyserad asynkront i bakgrunden.
Grundaren beordrade uttryckligen: **skriv ingen P4/P6-migration förrän en delad,
additiv proveniensmodell (Document ELLER Message som källa, utan polymorfa FK:er) är låst** —
se konversationen för den fullständiga arkitekturanalysen (exklusiv-arc-mönster via
`num_nonnulls()`-CHECK-constraint, `extract_claims_for_message` som syskonfunktion till
`extract_claims_for_document`, ny bakgrundsworker för konversationsklassificering eftersom
chat.py:s svarsväg är synkron och inte kan bära extra AI-anrop per tur, säkerhetsregler för
att aldrig behandla assistent-genererad text som grundarfakta). **P4/P6-migrationer är därför
INTE påbörjade** — väntar på grundarens bekräftelse av den föreslagna proveniensmodellen.

## Pass 8 (2026-07-28): MainAI Memory Core — P3 (claim-typning), första skivan

Grundaren korrigerade en felaktig uppdelning: "Connected Memory & Project Context v1" (ett
tidigare, för brett formulerat uppdrag) ska INTE byggas som ett separat minnessystem parallellt
med Life Library/Founder Knowledge Studio. Repots egen `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
specificerar redan EN gemensam minneskärna (§4): Kunskap/Projekt/Idéer/Beslut/Uppgifter/
Grundarminne/Konstitution är typer, relationer och vyer ovanpå SAMMA underliggande kedja
(`ImportJob → Document → KnowledgeVersion → DocumentChunk → KnowledgeClaim`), inte separata
lagringsplatser. Se konversationen för den fullständiga arkitekturgenomgången (befintliga
tabeller som återanvänds, additiva tabeller/kolumner per P3/P4/P6/P7, och en uttrycklig
varning om att INTE förväxla den nya `project_entities`-familjen (P4) med det redan
existerande `app/models/project_memory.py` — ett annat, orelaterat system för LifeAI-repots
EGEN utvecklingsstatus, inte grundarens liv/affärsprojekt).

Byggordning låst till repots egen §8: **P3 (denna branch) → P6 (parallellt, ingen ny PR än) →
P4 (kräver P3) → P5 (kräver P4) → P7B (sist, kräver P4:s godkännandeinfrastruktur)**. Ingen
`MainAICoreContext`, ingen ny retrieval-ordning, ingen systemprompt-ändring i denna PR — de
kräver P4:s `project_entities`-tabell för att ha något att läsa, och byggs i en separat,
senare PR.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/p3-claim-typing` | [#29](https://github.com/d1n095/LifeAI/pull/29) | **Öppen, inte mergad, `mergeable_state: clean`, CI grönt (verifierat)** — väntar på grundarens slutgranskning | P3: `KnowledgeClaim.claim_type` (migration 0018, additiv), utökat STEG 10-extraktionsanrop, `KnowledgeClaimOut`-schema + Library-UI-badge, PLUS (Pass 9) `backfill_claim_types()` för retroaktiv klassificering av befintliga claims + `POST /api/admin/claims/backfill-types`. 18 tester totalt i `test_claims.py` + full lokal svit 562 passed/1 skipped. | `claude/det-kommer-mer-879lcm` @ `dace6c8` (efter PR #28) |

**Verifierat lokalt (Pass 9, klart):** Full backend-svit mot riktig Postgres+Redis: 562 passed,
1 medvetet skippad (P2-kapacitetstestet), 0 regressioner. `tsc --noEmit`/`eslint`: rena.
GitHub Actions-CI verifierad grön direkt mot PR #29:s head-SHA via `pull_request_read` (18/18
checkar, "All required checks passed" = success) — inte bara Vercel.

Grundaren granskade PR #28 kod-för-kod (inte bara CI-status) i två separata rundor efter att
Pass 6:s ursprungliga vertikala kedja redan var grön, och hittade båda gångerna en verklig,
kvarvarande bugg i krasch/återupptagnings-logiken — se `MAINAI_CONTEXT_BUNDLE.md`s produktions-
incident (dokumentet fastnade permanent i `embedding`-status trots att dess ImportJob visade
"Klar"):

- **Runda 1** (commit `5bbc979`): `_import_one_file` behandlade ETT befintligt dokument i
  `RESUMABLE_INDEX_STATUSES` (t.ex. `embedding`, `extracting`) som en vanlig "duplicate" istället
  för att återuppta det — bara `awaiting_provider`/`blocked_provider` återupptogs innan detta.
  Ny `RESUMABLE_INDEX_STATUSES`-konstant (`app/models/document.py`), utökad
  `_resume_incomplete_document` (omdöpt från `_resume_blocked_document`), ny
  `_run_once`-spärr mot att markera ett jobb `completed`/`partial`/`failed` medan ett kopplat
  dokument fortfarande sitter fast, samt ny `app/worker.py`s `_reconcile_orphaned_documents` som
  reparerar ett REDAN terminalt jobb (den mekanism som faktiskt löser det befintliga
  `MAINAI_CONTEXT_BUNDLE.md`-fallet).
- **Runda 2** (commit `8d61f96`): samma återupptagningsfunktion anropade ovillkorligt
  textpipelinen (`extract_text`/`index_document`) — en MP3/MP4 som kraschat i
  `extracting`/`embedding` hade blivit felaktigt skickad dit istället för mediepipelinen
  (`media_import.validate_media_bytes`/`index_media_document`), med risk att bli
  `extraction_failed`. Fixat med dispatch på `media_import.media_kind_for(filename)`. Samtidigt
  fixades att `_requeue_blocked_jobs` lämnade `completed_at`/`failure_reason` kvar vid
  `partial → pending`-återställning (dataintegritetsfel, inte en funktionell bugg).

Båda rundorna fullt lokalt testade (545 passerade, 1 medvetet skippad), `tsc`/`eslint` rena, och
verifierade grönt på riktig GitHub Actions-CI (18/18 kontroller, "All required checks passed")
INNAN merge — se PR #28:s commit-historik för fullständiga detaljer per runda.

**Mergad av grundaren efter explicit, villkorat godkännande** ("När CI faktiskt visar grönt är
beslutet: merga") — sessionen verifierade villkoret (CI grönt på huvud-SHA `8d61f966...`) och
utförde själva merget via GitHub API (`merge_pull_request`, merge-commit `c32c339`), i linje med
grundarens uttryckliga instruktion i det ögonblicket. Ingen deploy utförd av sessionen — se
"Kvarstår efter merge" nedan.

## Pass 6 (2026-07-27): MainAI Core Loop v1 — engångsundantag från per-funktion-branch-regeln,
PR #28 öppen

**Grundaren har explicit auktoriserat ett medvetet, engångsundantag** från `CLAUDE.md`s
"en funktion = en branch/PR"-grundprincip för den här uppgiften specifikt (se uppdragets egen
text) — arbete sker kontinuerligt på EN integrationsbranch med många små, logiskt separerade
commits, och EN enda PR öppnas när hela den vertikala kedjan (upload → lagring → worker →
indexering → sökning → chatt med citat → omstartsöverlevnad → providernedgradering → CI →
deploy/rollback-verifiering) är bevisligen fungerande end-to-end.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/mainai-core-loop-v1` | [#28](https://github.com/d1n095/LifeAI/pull/28) | **Mergad** (Pass 7, 2026-07-28), merge-commit `c32c339f9710648604c537c24205e686c083f811`, efter grundarens explicita granskning och två ytterligare fix-rundor (`5bbc979`, `8d61f96`) — se Pass 7-avsnittet ovan. | MainAI Core Loop v1 — hela den vertikala kedjan upload→lagring→worker→indexering→sökning→chatt-med-citat→omstartsöverlevnad→providernedgradering→CI→deploy/rollback, verifierad med RIKTIG körning av `docker-compose.vps.yml`+`docker-compose.vps.ci.yml`-topologin på riktiga GitHub Actions-runners (körning [30304755138](https://github.com/d1n095/LifeAI/actions/runs/30304755138), attempt 2, helt grön — se PR #28:s beskrivning för fullständig punkt-för-punkt-verifiering). Lokal `docker build` av de riktiga bilderna är blockerad i den här sessionens sandlåda (nätverkspolicyn tillåter inte apt-get mot deb.debian.org), se `docs/CORE_LOOP_V1_BACKLOG.md`. Innehåller PR #26:s tidigare öppna innehåll (docs + rundtripstest), cherry-pickat och utökat med chatt-med-citat/omstartsöverlevnad/providernedgradering-steg i samma CI-jobb istället för ett nytt. PR #26 stängd med kommentar som pekar hit (ingen merge, allt innehåll bevarat). | `claude/det-kommer-mer-879lcm` @ `13a9677` (inkluderar PR #27) |

**Verifierat (Pass 6, klart):** Full lokal backend-testsvit (532 passade, 1 medvetet skippad)
körd mot riktig Postgres+Redis i denna sessions sandlåda. `vps-compose-verify`s utökade jobb
och `vps-deploy-rollback-test` kördes verkligen på GitHub Actions (inte bara lokalt) — sista
körningen (`30304755138`, attempt 2) helt grön: alla jobb success eller medvetet skippade.
Attempt 1 hade en infrastrukturflimmer (Docker Hub-timeout vid `pgvector/pgvector:pg16`-pull i
`Backend — unit/integration tests`, orelaterat till den här branchens ändringar — varje annat
jobb som pullar samma image lyckades) — löst med `rerun_failed_jobs`, grönt på omkörning. Under
arbetet avslöjades en RIKTIG bugg i omstartstestet (dockerds `restart: unless-stopped` hann
inte starta om en SIGKILLad worker inom CI:ns 30s-fönster) — fixat genom att explicit köra
`docker compose ... start worker` istället för att lita på dockerds egen timing, se commit
`4d47820`.

## Pass 5 (2026-07-27): PR #27 mergad, PR #26 väntar, storfilsimport-plan i stället för en Caddy-punktfix

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/fix-uploads-volume-permission` | [#27](https://github.com/d1n095/LifeAI/pull/27) | **Mergad**, merge-commit `a3194981e4adf94bb4807660003cbb7a4200e50e` | Akut produktionsbugg: `lifeai_uploads`-volymen var root-ägd, backend/worker kör som UID 10001 — varje riktig uppladdning fick 500 (`PermissionError`). Ny `uploads-init`-engångstjänst (root ENDAST för `chown -R 10001:10001`, `cap_drop: ALL`+`cap_add: [CHOWN]`, `restart: "no"`) som `backend`/`worker` nu väntar på (`depends_on: service_completed_successfully`). Upptäckt av PR #26:s nya CI-rundtripstest, löst i en egen PR per grundarens explicita val (alternativ 2) i stället för att blandas in i #26. | `claude/det-kommer-mer-879lcm` @ cd334d1 |
| `claude/vps-embedding-worker-docs-ci` | [#26](https://github.com/d1n095/LifeAI/pull/26) | **Stängd (inte mergad)**, suppersederad av [#28](https://github.com/d1n095/LifeAI/pull/28) (Pass 6:s `claude/mainai-core-loop-v1`, se ovan) — dess innehåll är cherry-pickat dit och utökat med chatt-med-citat/omstart/providernedgradering i samma CI-jobb. Stängd med en kommentar som pekar till #28 — inget innehåll förlorat. | Docs (`.env.vps.example`, `docs/STRATO_VPS_DEPLOY.md`) + ny CI-rundtripstest (verklig worker, nätverksisolerad Ollama-stub) som bevisar embedding-provider-konfigurationen fungerar end-to-end. Blev CI-rött på exakt den `PermissionError` #27 sedan fixade. | `claude/det-kommer-mer-879lcm` @ cd334d1 (föråldrad — #27 mergad sedan dess) |
| `claude/fix-caddy-upload-body-limit` | — (öppnades aldrig) | **Övergiven, aldrig committad/pushad** — inga commits fanns när branchen togs bort lokalt | Skulle bara höjt Caddys `request_body max_size` 30→65-70 MB för att synka med backendens 60 MB-gräns. Stoppad av grundaren INNAN PR öppnades: de verkliga produktionsfilerna är ~1,3 GB, så 60 MB är inte det arkitektoniska målet — en punktfix hade bara flyttat felet till workern, som läser hela originalfilet till minnet (`library_import.py:574-575`, `raw = f.read()`) i en container med `mem_limit: 384m`. Ersatt av `docs/LARGE_FILE_UPLOAD_PLAN.md` — en fullständig scoped plan för säker storfilsimport (mål ≥2 GB), som måste granskas och brytas ned i PR:er (se dokumentets §3) INNAN någon gräns höjs. **2026-07-27, korrigeringsrunda:** planens första version hade sex tekniska fel (ZIP påstods strömma men buffrar fortfarande varje entry som `bytes` via `_read_with_hard_cap()`s `chunks: list[bytes]`; PR-ordningen exponerade en obegränsad uppladdningsväg före workerns minnesfix; Caddy antogs behöva höjas utan grund; svag concurrency-design för del-mottagning; otillräcklig teststrategi med sparse-nollfiler; för starkt påstående om minnesoberoende) — samtliga korrigerade i dokumentets §0. Ingen kod ändrad i korrigeringen, bara dokumentet. | `claude/det-kommer-mer-879lcm` @ a319498 (aldrig pushad) |

**Uppdaterad rekommenderad ordning (efter Pass 6):** #26 är nu stängd, suppersederad av #28 —
se Pass 6-avsnittet ovan för fullständig status. Storfilsimport-planens PR-kedja (A–G, se
`docs/LARGE_FILE_UPLOAD_PLAN.md`) är ett helt separat, större spår och ska INTE byggas före
en genomgång/godkännande av planen själv.

## Pass 4 (2026-07-27): säkerhetsincidenter + chat context-status awareness

Samma dag som Pass 3 slutade (PR #16 mergad som `502b082`), en snabb sekvens av verifierade
produktionsincidenter, var och en löst i en egen branch/PR från huvudgrenens då-aktuella tip
(inte i förväg, se Merge-regeln):

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/reject-placeholder-secrets` | [#22](https://github.com/d1n095/LifeAI/pull/22) | **Mergad**, merge-commit `33c316b` | Säkerhetsincident: läckta SMTP/Redis-hemligheter + Gemini-platshållarbugg. `looks_like_placeholder_secret()` i alla 5 providrars `is_configured()`, `check_no_duplicate_env_keys()` i `deploy.sh`/CI, rotationsrunbook i `docs/VPS_OPERATIONS_RUNBOOK.md` | `claude/det-kommer-mer-879lcm` @ 502b082 |
| `claude/gemini-header-auth-security-fix` | [#23](https://github.com/d1n095/LifeAI/pull/23) | **Mergad**, merge-commit `5ab6e81` | Säkerhetsincident: Gemini-nyckeln skickades som `?key=...` URL-query och läckte via `httpx.HTTPStatusError` in i Docker-loggar. Flyttad till `x-goog-api-key`-header; `chat_with_fallback()` loggar nu alltid via `classify_provider_exception()` | `claude/det-kommer-mer-879lcm` @ 33c316b |
| `claude/gemini-diagnostic-logging` | [#24](https://github.com/d1n095/LifeAI/pull/24) | **Mergad**, merge-commit `b0481c1` | Fortsatt 404-utredning efter header-fixen: `_normalize_model()` (Compose stripper inte citattecken), enhetlig URL-byggare, Googles egna saniterade felmeddelande ytligt via `ProviderError.category`, `classify_provider_exception()` litar nu på ett redan satt `category` | `claude/det-kommer-mer-879lcm` @ 5ab6e81 |
| `claude/chat-context-status-awareness` | [#25](https://github.com/d1n095/LifeAI/pull/25) | **Mergad**, merge-commit `cd334d105012a4f26f3b9a81fa9beb20fe471e00`, driftsatt och verifierad i produktion | Bekräftad produktionsincident: chat kollapsade varje nollträff-tillstånd (worker nere, filer under bearbetning, saknad embedding-leverantör, indexeringsfel, sökfel just den frågan, genuint ingen träff, inga uppladdade filer) till samma fasta sträng "Ingen relevant kunskap hittades." Ny `app/rag/context_status.py` klassificerar den verkliga orsaken från redan existerande signaler (IndexStatus, worker-heartbeat, `classify_provider_exception`) — strukturerad `context_status` på `ChatMessageOut`, renderad i chat-UI:t. 7 nya regressionstester. Se PR-beskrivningen för fullständig svarsform. Kopiera-knapp/meddelandeåtgärder medvetet UTANFÖR scope — egen, separat uppföljande PR. | `claude/det-kommer-mer-879lcm` @ b0481c1 |

Även upptäckt och åtgärdat under samma pass, inte en egen branch (för litet för en egen PR,
men värt att notera här så det inte glöms): `chat.py`s embedding-provider-catch fångade bara
`ProviderError`, inte ett rått `httpx.HTTPError` — en konfigurerad-men-ogiltig nyckel kunde ge
en ohanterad 500. Fixat som en del av PR #25 (samma commit, samma test), inte en separat PR,
eftersom det är samma kodrad som ändå ändrades för context-status-syftet.

**GitHub-nyckelrotation:** grundaren skapade en ny Gemini-nyckel (även den `AQ.`-prefixad, som
är normalt) men installerar den avsiktligt inte förrän PR #24:s bild är driftsatt — se PR
#24:s incidentbeskrivning. SMTP/Redis-hemligheterna som exponerades innan PR #22 kräver
fortfarande rotation på grundarens faktiska produktions-VPS (operativt, inte kod — runbook
finns i `docs/VPS_OPERATIONS_RUNBOOK.md`).

## Pass 3 (2026-07-26): PR #13 mergad, MainAI Core-orkestrering påbörjad

9. **PR #13** (MainAI Project Memory & Coordination Loop, Fas 1–4) — 18/18 CI grönt, markerad
   ready-for-review och **mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`.
   Detta är första gången huvudgrenen innehåller MainAI:s eget projektminne (`project_notes`,
   `project_checkpoints`, `project_sources`, `project_branch_pr_status`) och den founder-only
   `/admin/memory`-vyn.
10. Grundaren utökade uppdraget samma dag från "bygg ett checkpointsystem" till "bygg första
    operativa kärnan av MainAI" — se `CLAUDE.md`s "MainAI Core"-riktning: konversations-/
    kunskapsretrieval, en lätt systemkarta, agentorkestrering (kod-/granskningsagent via
    befintliga provider-adaptrar), och en GitHub-integration med verklig skriv-behörighet
    bakom hårda säkerhetsgrindar (ingen merge-kapacitet implementerad alls i denna fas).
11. **`claude/mainai-core-orchestration-v1`** grenad direkt från huvudgrenens nya tip
    (`7afb01f`, alltså EFTER att PR #13 mergat — inte i förväg, se Merge-regeln). Bygger:
    migration 0016 (`agent_tasks`/`agent_task_events`), `app/agent_orchestration.py`,
    `app/integrations/github_client.py` (read/branch/commit/PR — medvetet ingen merge-metod),
    `retrieve_relevant_context()`/systemkarta i `app/project_memory.py`, ny `NoteKind.idea`,
    founder-UI `/admin/agents`. Se separat sektion nedan för scope och verifiering.

## Mergekedjan 2026-07-26 — genomförd i sin helhet

**Pass 1 (fristående fixar + processdokumentation):**
1. **PR #9** (`next` 16.2.10→16.2.11) → mergad, merge-commit `0081e562`.
2. **PR #11** (`brace-expansion`/GHSA-mh99-v99m-4gvg, ny CI-allowlist) → mergad, merge-commit
   `6929b700`. Se `docs/SECURITY_BLOCKERS.md` punkt 3.
3. **PR #10** (`CLAUDE.md` + den här filen) → mergad, merge-commit `403adc06`.

**Pass 2 (P1/P2-integration i huvudkedjan — grundarens explicita mandat):**
4. **PR #7** (P1) mergad i sin bas `claude/life-library-durable-worker-merged`
   (merge-commit `16959661`) — det steget skedde redan i pass 1:s förlängning.
5. `claude/life-library-durable-worker-merged` synkades mot huvudgrenens allra senaste tip
   (`5769cffa`, inkl. PR #12:s registeruppdatering) — ren merge, inga konflikter, ny tip
   `aa4d4b9`.
6. **PR #14** (ny, "Integrate Life Library durable worker + P1 into the main line") —
   `head: claude/life-library-durable-worker-merged` → `base: claude/det-kommer-mer-879lcm`.
   Innehåller BÅDE PR #6:s tidigare aldrig-mergade durable-worker-paket OCH P1, eftersom P1
   byggde direkt ovanpå PR #6:s bas-snapshot och de aldrig kan separeras utan
   historieomskrivning (uttryckligen undvikt). 18/18 CI grönt, `mergeable_state: clean`,
   **mergad** — merge-commit `2ddfeddc`. **Detta är första gången huvudgrenen någonsin
   innehållit P1 (eller PR #6).**
7. **PR #8** (P2) — verifierat via `git merge-base` att `909a5f1` (P1:s innehåll) nu är
   ancestor till den nya huvudgrenen, alltså att en ombasering INTE skulle ändra diffen.
   Bas ombasearad direkt till `claude/det-kommer-mer-879lcm` (utan mellansteg), diff
   bekräftat oförändrat (7 filer, +1225/-63), samma redan gröna CI-körning (huvudet
   oförändrat, `mergeable_state: clean`). **Mergad** — merge-commit `89682a18`.
8. **PR #13** (MainAI Project Memory-loopen, se separat sektion nedan) — bas ombasearad från
   `claude/p2-zip-hardening-plan` direkt till `claude/det-kommer-mer-879lcm` på samma sätt
   (verifierat via `git merge-base`, diff oförändrat: 8 filer, +835/-1). Fortsatt draft,
   fortsatt öppen.
9. Full lokal verifiering körd på den fullständigt integrerade koden (real Postgres+Redis i
   Docker): 411/411 tester gröna (346 backend + 65 security/account), inga regressioner.

P7A rördes inte — fortsatt fryst, se eget avsnitt nedan om dess nu ytterligare föråldrade bas.

## Huvudkedjans nuläge (efter PR #13)

```
claude/det-kommer-mer-879lcm (huvudgren, tip 7afb01f)
  innehåller nu: PR #9, #11, #10, #12, PR #14 (durable worker + P1), PR #8 (P2), PR #13
  (MainAI Project Memory & Coordination Loop, Fas 1-4)
  └─ claude/mainai-core-orchestration-v1 — MainAI Core-orkestrering, öppen (ingen PR
       skapad än vid senaste verifiering), @ se lokal branch
       (grenad EFTER PR #13:s merge, inte i förväg — se Merge-regeln)

claude/p7a-governance-ingestion-plan — FRYST, INGEN PR, @ df597f2
  (grenad från en nu mycket föråldrad P2-tip, 15487e2 — inte 7afb01f)
```

**`claude/life-library-durable-worker-merged`, `claude/founder-knowledge-studio-v1`,
`claude/p2-zip-hardening-plan` och `claude/mainai-memory-loop-v1` är nu subsumerade** —
deras innehåll finns i huvudgrenen via PR #14/#8/#13. Brancharna själva kan städas när
grundaren bekräftar (inte gjort automatiskt, se säkerhetsprotokollet mot destruktiva
åtgärder).

## Fristående, orelaterade fixar (grenade direkt från huvudgrenen)

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md`s
grundprincip för varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `0081e562` | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/frontend-npm-audit-brace-expansion` | [#11](https://github.com/d1n095/LifeAI/pull/11) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `6929b700` | `brace-expansion`/GHSA-mh99-v99m-4gvg — daterad, ID-specifik CI-allowlist (`frontend/scripts/check-npm-audit.js`), se `docs/SECURITY_BLOCKERS.md` punkt 3 | `claude/det-kommer-mer-879lcm` @ 0081e562 (efter PR #9) |
| `claude/development-workflow-principles` | [#10](https://github.com/d1n095/LifeAI/pull/10) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `403adc06` | `CLAUDE.md` + den här filen — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ 6929b700 (efter PR #11) |
| `claude/branch-registry-post-merge-chain-update` | [#12](https://github.com/d1n095/LifeAI/pull/12) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `5769cffa` | Registerpost-mergekedja | `claude/det-kommer-mer-879lcm` @ 403adc06 |
| `claude/life-library-durable-worker-merged` | [#14](https://github.com/d1n095/LifeAI/pull/14) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `2ddfeddc` | PR #6 (durable worker/lagring) + P1 (provider-verifiering) — landade i huvudgrenen för första gången | `claude/det-kommer-mer-879lcm` @ 5769cffa |
| `claude/p2-zip-hardening-plan` | [#8](https://github.com/d1n095/LifeAI/pull/8) | **Mergad** i `claude/det-kommer-mer-879lcm` (ombasearad dit efter PR #14), merge-commit `89682a18` | P2: nästlad ZIP-hantering, `encrypted`-status, `archive_path`/`archive_chain` | `claude/det-kommer-mer-879lcm` @ 2ddfeddc (efter PR #14) |
| `claude/mainai-memory-loop-v1` | [#13](https://github.com/d1n095/LifeAI/pull/13) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`. 18/18 CI grönt. | MainAI Project Memory & Coordination Loop, Fas 1–4: `project_notes`/`project_checkpoints`/`project_sources`/`project_branch_pr_status`, resumption-brief, founder-UI `/admin/memory` | `claude/det-kommer-mer-879lcm` @ 89682a18 |
| `claude/chat-message-persistence-fix` | [#17](https://github.com/d1n095/LifeAI/pull/17) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `8dcf8161`. 18/18 CI grönt före merge, `mergeable_state: clean`. | PR A (LLM Coupling & Failure-Boundary Audit): användarmeddelandet persisteras och committas OBEROENDE av providerkallet; ny `MessageStatus`/`in_reply_to_id`/`error_category` på `Message`, migration `0016_chat_message_status`; `POST /messages/{id}/retry`; `ChatMessageOut` skiljer explicit `user_message_saved` från `assistant_status`. Se sektionen nedan för fullständig audit-bakgrund. | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/search-embedding-failure-fallback` | [#18](https://github.com/d1n095/LifeAI/pull/18) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `f250e728`. 18/18 CI grönt före merge, `mergeable_state: clean`. | PR B: `hybrid_search()` accepterar `vector: list[float] \| None`, hoppar över den semantiska kanalen (inte en fejkad nollvektor) när providern saknas. Ny `LibrarySearchResponseOut` med `semantic_search_available`/`degraded_reason`. | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/mainai-local-first-principle` | [#19](https://github.com/d1n095/LifeAI/pull/19) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `ce432613`. CI grönt före merge, `mergeable_state: clean`. | Grundprincip "MainAI är systemets intelligens, inte en extern tjänst" i `docs/MAINAI_ARCHITECTURE.md` §1, PR C:s stängningsbeslut (se nedan). | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/mainai-core-orchestration-v1` | [#16](https://github.com/d1n095/LifeAI/pull/16) | **Öppen, draft — under granskning.** Efter att PR #17/#18/#19 mergades blev `mergeable_state: dirty` (migration 0016 kolliderade — se nedan), åtgärdat genom att döpa om `0016_agent_orchestration.py` → `0017_agent_orchestration.py` (`down_revision` uppdaterad till PR #17:s `0016_chat_message_status`) och en `git merge` av den nya huvudgrenens tip in i branchen (`schemas.py`/`lib/api.ts` auto-mergade konfliktfritt, bara den här filen krockade). Full lokal verifiering omkörs efter mergen innan draft-status lyfts. | MainAI Core v0: kategoriserad retrieval + systemkarta, agentorkestrering (`agent_tasks`/`agent_task_events`, migration **0017** efter omnumrering), minimal GitHub-klient (läsning/branch/commit/PR, ALDRIG merge i klienten), founder-UI `/admin/agents`. Se separat scope-sektion nedan. Dispatch returnerar en säker, icke-läckande 503 om samtliga providers misslyckas. | `claude/det-kommer-mer-879lcm` @ ce432613 (efter merge av PR #17/#18/#19) |

## MainAI Core: agentorkestrering (`claude/mainai-core-orchestration-v1`, PR #16) — scope och verifiering

Bygger vidare på Fas 1–4 (nu i huvudgrenen), enligt CLAUDE.md's 2026-07-26 "MainAI Core"-
riktning: inte bara ett checkpointsystem, utan första vertikala kedjan samtal → minne →
helhetsbild → problem → agentuppdrag → kod → granskning → GitHub-PR → checkpoint.

**Byggt i denna omgång:**
- `retrieve_relevant_context()` — kategoriserad, relevans-rankad hämtning (heuristisk
  token-overlap, samma metod som `detect_conflicts_and_duplicates()` redan använder) i
  stället för att dumpa hela minnet. Skiljer uttryckligen `verifierade_fakta_och_status` /
  `grundarens_beslut` / `ej_beslutade_ideer` (ny `NoteKind.idea`) / `blockerare` /
  `nasta_steg` / `osakra_eller_motstridiga` / `historik`.
- `build_system_map()`/`ingest_system_map()` — lätt, textbaserad skanning av routers/
  modeller/migrationer/frontend-routes under `PROJECT_ROOT`, lagrad via samma
  content-addressed storage som allt annat i modulen (`ProjectSource(source_type="system_map")`).
  Medvetet smal denna omgång — dupliceras inte mot redan befintlig admin-status
  (`/api/admin/library/ops`, `/api/admin/providers`).
- Migration **0017** (omnumrerad från 0016 efter att PR #17:s `0016_chat_message_status`
  mergades först — se raden ovan): `agent_tasks` (ett avgränsat uppdrag — titel, filer,
  begränsningar, acceptanskriterier, krävda tester) + `agent_task_events` (append-only
  historik: dispatch, resultat, testresultat, granskning, GitHub-operationer).
- `app/agent_orchestration.py` — `create_agent_task`/`dispatch_task` (kodagent via befintlig
  `chat_with_fallback`)/`record_test_results`/`review_task` (granskningsagent, BLOCKERAD utan
  registrerade testresultat, kan aldrig godkänna på röda tester även om modellen säger
  "approved")/`prepare_github_pr`/`attempt_auto_merge` (ALLTID blockerad — se nedan). Dispatch
  fångar nu `ProviderError` och returnerar en fast, icke-läckande 503 istället för en ohanterad
  500 — se modulens "Local-first status"-avsnitt för samma Idag/Målarkitektur-uppdelning som
  `docs/MAINAI_ARCHITECTURE.md` §1:s grundprincip.
- `app/integrations/github_client.py` — minimal REST-klient (läsning, branch, commit, PR).
  **Ingen merge-metod finns i klienten alls** — inte bara avstängd bakom en flagga, genuint
  frånvarande som kod. `github_write_enabled` (default `False`) styr om `prepare_github_pr()`
  bara FÖRESLÅR exakt PR-innehåll (branch/commit/PR-text, ingen GitHub-anrop) eller faktiskt
  skapar branch/commit/PR. `github_auto_merge_enabled` finns som konfigurationsflagga men
  gatear ingen faktisk kapacitet ännu.
- Founder-UI `/admin/agents` — uppdragslista, detaljvy med händelsehistorik, knappar för
  varje steg i kedjan. Verifierat i riktig Chromium-webbläsare mot en riktig backend: login,
  uppdragsskapande, dispatch (korrekt, ren felyta när ingen provider-nyckel är konfigurerad —
  exakt samma beteende som resten av appen), och det alltid-blockerade merge-försöket.

**Explicit avgränsat bort denna omgång** (registrerat, inte bortglömt):
- Verklig GitHub-skrivbehörighet är byggd och testad (mockad HTTP-nivå) men aldrig körd mot
  ett riktigt repo i denna session — kräver en riktig `GITHUB_TOKEN` som grundaren
  provisionerar separat (samma mönster som providernycklarna, aldrig i chatten).
- Semantisk (embedding-baserad) retrieval — nuvarande implementation är medvetet en
  heuristik, inte en ny vektorpipeline för noteringar.
- Hela "Autonomous Verification & Interaction Layer" (webbläsarstyrd funktionstestning,
  persona-simulering, digital tvilling av grundaren) — en betydligt större, separat
  initiativ som inte påbörjats.

**Verifiering:** 15 nya tester i `test_agent_orchestration.py` (inkl. ett fullständigt
vertikalt bevis: note → task → dispatch → test → review → PR-förslag → checkpoint → kall
läsning, samt en test för den icke-läckande 503:an på total providerkollaps), 5 nya i
`test_project_memory.py` (retrieval + systemkarta). Full befintlig svit senast omkörd (före
denna merge): 455 gröna, 1 medvetet skippad, inga regressioner — omkörs igen efter mergen och
migrationsomnumreringen innan draft-status lyfts. Migrationsrundtripp verifieras på nytt mot
den förlängda kedjan (…→0016→0017→nedgradering×2→uppgradering). Frontend: `tsc`/`eslint`/
`next build` gröna, UI verifierad i riktig webbläsare mot riktig backend+Postgres.

## LLM Coupling & Failure-Boundary Audit — genomförd (2026-07-26)

En extern audit av grundaren identifierade två verkliga fel — inte hypotetiska — där ett
AI-providerfel kunde få oavsiktliga konsekvenser för icke-AI-funktionalitet: (1) chatt
tappade det redan sparade användarmeddelandet om providern misslyckades efteråt, (2)
biblioteks-sökningen 500:ade helt om embedding-providern var otillgänglig trots att dess
textmatchningskanal inte behöver någon provider alls. Grundaren godkände audit-splitten och
skärpte båda kraven: PR A måste skilja explicit mellan "meddelande sparat" och "AI-svar
misslyckades" i kontraktet (inte en slentrianmässig 200:a), och PR B måste ha en RIKTIG
lokal fallback (inte bara fånga felet och returnera tomt). PR A (#17), PR B (#18) och
principdokumentationen (#19) är nu alla mergade i huvudgrenen. Minimal dispatch-fix i PR #16
är byggd och pushad (se sektionen ovan) — kvar är att slutföra PR #16:s granskning och merga
den.

**PR C stängd, inte byggd — verifierat inte längre relevant.** Grundaren godkände "Skip PR C"
med ett uttryckligt tilläggskrav: verifiera först att `/api/documents/upload` verkligen saknar
kvarvarande konsumenter innan något stängs eller städas. Verifiering genomförd:

- `POST /api/documents/upload` har INGEN frontend-anropare kvar. `frontend/lib/api.ts`s
  `uploadDocument()`-funktion existerar men anropas ingenstans — `app/(shell)/documents/page.tsx`
  gör bara `router.replace("/library")` sedan commit `0d9f487` ("Life Library: single upload
  hub...", 2026-07-22). All riktig uppladdning går via `/api/library`s importpipeline, som
  redan har full `ImportJob`-baserad status, säker felklassificering och en riktig retry-
  åtgärd (byggt i P1/P2/STEG-arbetet, långt mer robust än vad PR C skulle byggt).
- Backend-sidans felhantering som PR C skulle lagt till **finns redan** för `/documents/upload`-
  vägen: `app/rag/ingest.py`s `index_document()` sätter redan distinkta `IndexStatus`-värden
  (`extraction_failed`/`awaiting_provider`/`blocked_provider`/`indexing_failed`/`failed`) och
  använder redan `classify_provider_exception()` — aldrig rå `str(exc)` — för varje
  felläge, inklusive embedding-providerfel EFTER en godkänd pre-flight-kontroll. Detta byggdes
  redan under P1, innan den här auditen. En `reindex_document_id()`-retry-funktion finns redan
  skriven men anropas ingenstans (orphaned).
- Enda verkliga luckan (`DocumentOut` exponerar inte `status`/`error_message`, ingen
  `/retry`-rutt) gäller alltså en väg utan någon aktiv UI-konsument — att bygga den skulle
  vara arbete för en död kodväg, inte en verklig felyta en grundare kan träffa på.
- `frontend/e2e/shell-pages.spec.ts`s "documents: empty state..."-test refererar fortfarande
  den gamla `/documents`-sidans UI-text/knappar (från commit `a46dc7a`, FÖRE
  konsolideringen) och skulle idag fela mot verklig kod — men det upptäcks aldrig, eftersom
  `.github/workflows/ci.yml`s "E2E — Playwright (full stack)"-jobb explicit bara kör
  `e2e/auth.spec.ts e2e/security.spec.ts e2e/account.spec.ts`. Filen är alltså redan
  exkluderad ur CI, inte bara föråldrad.

**Ny uppföljningsuppgift (inte byggd nu, se grundarens explicita instruktion att inte utöka
en död kodväg):** en separat städ-branch/PR som tar bort `POST /api/documents/upload` och dess
bakgrundsindexering (`_index_in_background`, den orphanade `reindex_document_id()`), tar bort
eller uppdaterar det föråldrade `frontend/e2e/shell-pages.spec.ts`, och rättar
`docs/MAINAI_ARCHITECTURE.md` rad 303 (§5, som fortfarande beskriver `/api/documents/upload`
som det aktiva uppladdningsflödet) — `GET /api/documents` och `DELETE /api/documents/{id}`
ska sannolikt vara kvar (fortsatt bakåtkompatibel läsning/borttagning av samma delade
`documents`-tabell, se `test_a_source_deleted_via_library_disappears_from_the_older_documents_router_too`).
Detta är EN ny, ren "ta bort död kod"-uppgift — inte en utökning av PR C:s ursprungliga scope.

## Stängda utan merge (subsumerade, inte relevanta att slå ihop)

| PR | Branch | Status | Anledning |
|---|---|---|---|
| [#4](https://github.com/d1n095/LifeAI/pull/4) | `claude/founder-knowledge-studio-v1` | **Stängd** (inte mergad) | `git merge-base --is-ancestor 909a5f1 origin/claude/det-kommer-mer-879lcm` = sant — hela innehållet landade redan via PR #14. Stale bas (`claude/night-shift-mainai-web`). |
| [#6](https://github.com/d1n095/LifeAI/pull/6) | `claude/life-library-durable-worker` | **Stängd** (inte mergad) | `git merge-base --is-ancestor a6d16b5 origin/claude/det-kommer-mer-879lcm` = sant — hela innehållet landade redan via PR #14. Stale bas (`claude/life-library-upload-queue`). |

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge)

1. ~~PR #9~~, ~~#11~~, ~~#10~~, ~~#7~~, ~~#8~~, ~~#14~~, ~~#13~~, ~~#16~~, ~~#17~~, ~~#18~~,
   ~~#19~~, ~~#22~~, ~~#23~~, ~~#24~~, ~~#25~~, ~~#27~~, ~~#28~~, ~~#29~~, ~~#30~~ — samtliga
   mergade i huvudgrenen (se Pass 4/5/7/10-25-avsnitten ovan). ~~PR #4~~, ~~PR #6~~, ~~PR #26~~
   stängda utan merge (#4/#6 subsumerade, se ovan; #26 suppersederad av #28, se Pass 6).
2. ~~PR C~~ — stängd, inte byggd. Se "LLM Coupling & Failure-Boundary Audit"-sektionen ovan
   för verifiering och den nya, separata "ta bort död kod"-uppföljningsuppgiften.
3. **PR #32** (`claude/frontend-npm-audit-ghsa-mh99-source-ids`) — öppen, oberoende av PR #31,
   kan mergas till huvudgrenen NÄR SOM HELST (helt grön, "All required checks passed", rör
   endast `frontend/scripts/check-npm-audit.js` + `docs/SECURITY_BLOCKERS.md`). Rekommenderas
   mergas FÖRE PR #31, så att PR #31 sedan kan uppdateras från huvudgrenen och få en grön
   `npm audit`-kontroll — men enligt Merge-regeln ska PR #31 INTE uppdateras i förväg innan
   PR #32 faktiskt är mergad.
4. **PR #31** (`claude/s1a-memory-source-implementation`) — draft, öppen, INTE mergbar än
   (grundaren har inte gett fräsch granskning/godkännande av Pass 26, produktionsdataprofilen
   är inte gjord). Grön på alla obligatoriska kontroller utom `npm audit` (väntar på PR #32,
   punkt 3).
5. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip innan aktivering — dess bas (`15487e2`) är nu långt bakom både
   P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **PR #31 blockeras INTE av PR #32** i egentlig mening (PR #31:s eget innehåll är oberoende
  korrekt) men PR #31:s `npm audit`-CI-kontroll förblir röd tills PR #32 mergas till
  huvudgrenen och PR #31 uppdateras därefter — se punkt 3/4 ovan.
- **PR #31 mergas inte** förrän grundaren gett en fräsch, uttrycklig granskning/godkännande av
  Pass 26 OCH produktionsdataprofilen är genomförd (se `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
  §4.8:s "Status"-avsnitt).
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något öppet PR.

## Kvarstår efter PR #28:s merge (2026-07-28)

- **Deploy till produktions-VPS:n är INTE utförd av den här sessionen.** Grundarens sista
  instruktion bad om merge OCH deploy "med den befintliga verifierings- och
  rollback-processen", men den här sessionen har varken SSH-nycklar till produktions-VPS:en
  eller tillåtelse att köra en verklig deploy autonomt (se `CLAUDE.md`s standing regler och
  denna sessions upprepade SSH-avslag). VPS Phase 5+6 gjorde deploy explicit manuellt grindad
  (`docs/VPS_OPERATIONS_RUNBOOK.md`) — grundaren behöver köra det faktiska deploy-steget
  (`scripts/vps/deploy.sh`) själv, eller uttryckligen förse sessionen med en riktig, aktuell
  SSH-nyckel och en förnyad bekräftelse i det ögonblicket deployet ska köras.
- Efter en lyckad deploy: `_reconcile_orphaned_documents` (ny i denna PR) kör automatiskt vid
  nästa worker-pollcykel och ska reparera det redan fastnade `MAINAI_CONTEXT_BUNDLE.md`-
  dokumentet (och alla andra dokument i samma läge) utan manuellt ingrepp — se Pass 7 ovan.
  Detta bör verifieras mot produktions-`GET /api/library/{id}`-statusen efter deploy, inte bara
  antas.

## Vilka brancher väntar på ett beroende innan de bör uppdateras

Enligt Merge-regeln — dessa ska INTE röras förrän beroendet faktiskt är mergat, inte i
förväg:

- **P7A** väntar på ett separat, uttryckligt beslut om att börja implementation, plus sin
  egen ombasering när det beslutet tas — inte på något annat öppet PR just nu.

## Konflikter

**Löst 2026-07-26: migrationsnummer-krock mellan PR #16 och PR #17.** Båda branchades från
samma tip (`7afb01f`) och skapade oberoende av varandra en `0016_*.py`-migration —
`0016_agent_orchestration.py` (PR #16) och `0016_chat_message_status.py` (PR #17), båda med
`down_revision = "0015"`. Väntat och redan flaggat i förväg (se tidigare passets anteckning
om att detta skulle lösas "när PR #16 rebasas efter PR A/B merge, inte i förväg"). När PR #17
mergades blev PR #16:s `mergeable_state: dirty`. Löst genom att döpa om PR #16:s fil till
`0017_agent_orchestration.py` och sätta `revision = "0017"`, `down_revision = "0016"` (pekar
nu på PR #17:s migration), plus en vanlig `git merge` av huvudgrenens nya tip in i PR #16:s
branch (`schemas.py`/`frontend/lib/api.ts` auto-mergade konfliktfritt eftersom PR #16/#17/#18
lägger till olika, icke-överlappande klasser i samma filer; bara den här filen — som båda
sidor redigerat samtidigt — krockade textuellt). Migrationsrundtripp och full testsvit
verifieras på nytt efter denna ändring innan PR #16 mergas.

Utöver detta: inga andra kända filkonflikter. Samtliga integrationssteg (branch-synk, PR #14,
PR #8:s ombasering, PR #13:s ombasering) verifierades konfliktfria via `git merge-base` innan
de utfördes — se "Mergekedjan"-sektionen ovan för detaljer per steg.

Om en verklig filkonflikt upptäcks i framtiden ska den listas här explicit — vilka brancher,
vilka filer, och vilken lösning som föreslås — inte bara upptäckas i förbigående när en merge
misslyckas.

## Risk för dubbelarbete

Ingen känd, aktiv risk för dubbelarbete just nu. P7A:s bas är föråldrad (se ovan) men
branchen är fryst, så ingen aktiv utvecklingsrisk finns förrän ett beslut tas att återuppta
den — vid det laget måste den ombaseras mot huvudgrenens då-aktuella tip, inte mot P2:s
gamla tip.

Innan en ny branch/implementation påbörjas: jämför dess tilltänkta scope mot ALLA rader i
tabellerna ovan, inte bara den senaste. Om något överlappar, uppdatera det här avsnittet
INNAN arbetet påbörjas, inte efteråt.

## Stale/redan sammanslagna brancher (kandidater för städning, INTE raderade)

Verifierat via `git merge-base --is-ancestor` mot `claude/det-kommer-mer-879lcm` — dessa är
redan fullt innehållna i huvudgrenen. Listade här som referens, inte raderade utan explicit
tillåtelse (destruktiv åtgärd, se säkerhetsprotokollet):

`claude/fix-entrypoint-startup-race`, `claude/fix-pooler-auth-hardening`,
`claude/fix-render-public-port`, `claude/fix-supabase-pooler-role`,
`claude/fix-supabase-pooler-tenant-suffix`, `claude/founder-only-launch`,
`claude/integrate-founder-vps`, `claude/mainai-architecture-designs`,
`claude/night-shift-mainai-web`, `claude/render-service-name-fix`,
`claude/strato-vps-prep`, `claude/verify-combined-container`,
`claude/frontend-npm-audit-next-16-2-11` (PR #9, mergad),
`claude/frontend-npm-audit-brace-expansion` (PR #11, mergad),
`claude/development-workflow-principles` (PR #10, mergad),
`claude/life-library-durable-worker-merged` (PR #14, mergad — bar PR #6 + P1 in i huvudgrenen),
`claude/founder-knowledge-studio-v1` (PR #7's head, subsumerad via PR #14),
`claude/p2-zip-hardening-plan` (PR #8, mergad),
`claude/mainai-memory-loop-v1` (PR #13, mergad).

## Subsumerade i den aktiva kedjan (inte längre fristående)

Dessa branchars innehåll finns redan helt inom `claude/founder-knowledge-studio-v1` (P1)
högre upp i kedjan — de behöver inget eget beslut, bara noteras som vad de blev:

- `claude/life-library-upload-queue` → innehåll i → `claude/life-library-durable-worker` →
  innehåll i → `claude/life-library-durable-worker-merged` (PR #7:s bas-snapshot).

## Orphaned — kräver ett beslut, inte del av någon aktiv kedja

- `claude/fkp-v1.1` — 1 commit ("FKP v1.1: docs-only korrigering och integration av
  review-overlay + samtalsregister"), varken mergad i huvudgrenen eller del av P1/P2/P7A-
  kedjan. Status okänd tills grundaren tar ställning: merga, revidera, eller överge.

## Ej fullständigt granskade

Denna lista byggdes från en snabb `git merge-base`-genomgång av samtliga fjärrbranchar vid
tillfället ovan — den täcker ANCESTRY (är X en förfader till Y), inte innehållet i varje
branch i detalj. Om en branch saknas här, eller om något ser fel ut, uppdatera det här
dokumentet efter verifiering — gissa inte.
