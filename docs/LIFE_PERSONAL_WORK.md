# LifeAI Personal Work — tasks, calendar, search and dashboard

**Branch:** `freebuff/life-workspaces`, bas `04c82c3aa4ea411835c48561f6c03cef4bb87999`
(Founder Alpha). Kod-SHA `0d917c32f267ab98996f8d517a257d2fa369f065`; dokumentationscommit
ligger ovanpå. Ingen PR, push, merge eller deployment är gjord. `first_cycle_go=false`.

Detta är nästa produktlager ovanpå den redan levererade Life Workspaces-implementationen
(`5f733c2`). Det bygger vidare på befintliga `projects`, `tasks`, `calendar_events`,
`documents`, `conversations`, retrieval och MainAI execution — ingen parallell minnes-,
exekverings- eller kalendermotor införs.

## Vad som är implementerat

### A — Persistent projektkontext

- Projektets syfte, mål, milstolpar, beslut, källor, uppgifter och samtal finns i samma
  owner-scopade workspace.
- Beslut har `version`, `supersedes_id`, `epistemic_status` (`confirmed` / `uncertain` /
  `proposed`), `origin` (`founder` / `ai`) och `superseded_at`. Ett gammalt beslut markeras
  `superseded` och ett nytt radas med `supersedes_id`; historiken finns kvar.
- Workspace-kontexten är bounded och byggs från befintliga moduler. Chatt med `project_id`
  använder projektkontexten i systemprompten och retrieval; ingen ny minnesdatabas skapas.
- Källattribution och osäkerheter visas i workspace-vyn. AI-förslag, osäkra och bekräftade
  beslut är visuellt åtskilda.
- Alla läsningar och skrivningar är founder-gated och owner-filtrerade.

### B — Uppgiftshantering

- Skapa, redigera, ta bort och filtrera uppgifter; sök på titel/beskrivning.
- Prioritet, status, deadline, projekt, milstolpe och beroenden.
- Statusflöde `todo -> in_progress -> done`, `blocked` och `cancelled`; en uppgift med
  oavslutat beroende blir `blocked` och frigörs när beroendet blir `done`.
- `task_dependencies` med cykeldetektering, `task_events` som append-only historik,
  `version` för optimistisk samtidighet och `deleted_at` för soft-delete.
- Milstolpar räknar `task_count` / `completed_task_count`; status kan sättas manuellt.
- Uppgiftsdetaljen visar `kind="user_task"` och `execution_authority="none"`, så en
  användaruppgift aldrig förväxlas med ett auktoriserat MainAI-jobb.
- UI: `/tasks` med filter, skapaformulär, detaljpanel, beroenden, historik och blocked-badge.
  Projektvyn visar och skapar milstolpar och kopplar uppgifter till dem.

### C — Kalender

- Skapa, redigera och ta bort egna evenemang; kategori `personal` / `work` / `school`.
- Tidszon sparas per evenemang. Tidszonsmedvetna indata normaliseras till samma naiva
  UTC-representation som lagras; detta stänger en verklig 500-defekt i konfliktfrågan.
- Upprepning `daily` / `weekly` / `monthly` materialiseras som ett bounded antal rader
  (max 52, standardhorisont 180 dagar) med gemensamt `series_id`. Ingen extern kalender,
  ingen background scheduler och inga autonoma ändringar.
- Konfliktdetektering via `/api/calendar/conflicts`, med `exclude_event_id` vid redigering.
- Evenemang kan kopplas till projekt, uppgift och milstolpe. UI: `/calendar` med
  skapa/redigera/ta bort, serieomfattning, konfliktkontroll och kategori-/datumfilter.

### D — Founder dashboard

- `/api/dashboard` är en bounded aggregering över aktiva projekt, öppna/blockerade
  uppgifter, kommande evenemang, senaste beslut/dokument/samtal och supervised MainAI.
- Dashboarden gör ett fast antal queries i stället för en query per rad. Testet mäter
  faktiska SQL-statements och har budget `<= 40`.
- `/` använder endpointen, visar loading, fel, tomt läge och alla paneler från verklig
  databasstate. Inga klienträknade hela samlingar och ingen påhittad exempeldata.

### E — Sökning

- `/api/search` söker owner-filtrerat i projekt, uppgifter, dokument, konversationer och
  kalenderhändelser. Filter: typ, projekt, datumintervall, `limit`/`offset`.
- Resultaten har typ, titel, snippet, projektkoppling, tidsstämpel och en stabil `href`.
- `limit` är bounded (1–50) och pagineringen är stabil; testet verifierar att sidorna inte
  överlappar och att totalen är densamma.
- Snippets läses först efter owner-filtret. En andra användare får 403 av founder-gaten
  innan någon owner-fråga körs.
- UI: `/search` med typ-/projekt-/datumfilter och föregående/nästa.

### F — Full-stack-acceptans

`backend/tests/backend/test_personal_work_management_api.py` kör mot riktig PostgreSQL och
verifierar:

1. projekt, milstolpe, två beroende uppgifter och blocked/unblock-flödet,
2. uppgiftsdetalj, historik, filter och stale-version 409,
3. kalender med återkommande serie, konflikt, redigering, borttagning och länkar,
4. beslut som ersätts och behåller den gamla raden som `superseded`,
5. dashboard och sökning över projekt/uppgift/dokument/konversation/kalender,
6. en ny applikationsprocess (ny `TestClient`) läser samma beständiga rader,
7. en andra användare nekas av founder-gaten.

Dokumentraden skapas i testet och ingår i sökningen. Uppladdning/indexering/retrieval körs
inte om i den här filen; de befintliga Life Workspaces-/RAG-sviterna äger den vägen och
kördes som regression nedan.

### G — Prestanda och återhämtning

- Dashboard: **24 SQL-statements**, **58,0 ms** i den lokala mätningen (budget 40).
- Sökning: **39,6 ms** i samma mätning; paginering verifierad utan dubbletter.
- Optimistisk `expected_version` ger 409 vid stale klient; dependency-uppdatering och
  milstolpens räknare uppdateras i samma transaktion.
- Workspace-regressionen kör idempotent sync, stale/interrupted sync, retry och
  chat-idempotens (15 passed). Browserresan laddar om sidan och dashboarden efter
  persistens.
- Inga nya cacher eller infrastrukturkomponenter infördes.

### H — Produktkvalitet

- Alla nya formulär har labels, tangentbordsinskick (Enter) och tydliga disabled-lägen.
- Responsiva grid layouter, konsekvent sidebar (Uppgifter, Kalender, Sök) och länkar mellan
  dashboard, projekt, uppgifter och kalender.
- Fel visas med `role="alert"`; tomma lägen är explicita; 409 hanteras genom omläsning.
- Terminologin skiljer `Användaruppgift` från `MainAI`-jobb. Befintliga komponenter och
  API-klienten återanvänds; ingen dubbel projekt-/uppgiftsmodell.

## Datamodell och migration

Additiv migration `0095_personal_work_management` (efter `0094`):

- `workspace_milestones`, `task_dependencies`, `task_events`.
- `tasks`: `due_at`, `completed_at`, `blocked_reason`, `milestone_id`, `version`,
  `deleted_at`; statusvärden `blocked` och `cancelled`.
- `workspace_decisions`: `version`, `supersedes_id`, `epistemic_status`, `origin`,
  `superseded_at`.
- `calendar_events`: kategori, tidszon, recurrence, `series_id`, `recurrence_index`,
  projekt-/uppgifts-/milstolpslänkar, `version`, soft-delete och avbokningsfält.

Alla nya tabeller har RLS och owner-scopade frågor. Migrationen rör inte R15/Security
Kernel, retrieval-indexet eller MainAI:s execution-tabeller.

## Verifiering (faktiskt körd)

| Kontroll | Resultat | Evidens |
| --- | --- | --- |
| Ny PostgreSQL-acceptans | **3 passed** | `docs/evidence/life-personal-work/backend-tests.txt` |
| Workspace/projekt/chat-regression | **15 passed** | `docs/evidence/life-personal-work/workspace-regression.txt` |
| Playwright full-stack | **1 passed, 0 failed, 0 flaky** | `docs/evidence/life-personal-work/playwright-personal-work.txt` |
| Frontend lint | **0 errors**, 1 befintlig varning i `lib/api.ts` | `frontend-lint.txt` |
| TypeScript | **PASS** | `frontend-tsc.txt` |
| Produktionsbuild | **PASS**, 29 routes inkl. `/tasks`, `/calendar`, `/search` | `frontend-build.txt` |
| Dashboardbudget | **24 SQL-statements** (<= 40) | `backend-tests.txt` |

Browserresan krävde `LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib`
för att Chromium skulle hitta `libatk` m.fl. i den isolerade testmiljön. Detta är en
miljöförutsättning, inte en produktionsändring.

## Verklig defekt som hittades och rättades

Den första browserkörningen gav 500 i `/api/calendar/conflicts`: webbläsaren skickar
tidszonsmedvetna ISO-strängar medan kalenderrader lagras naiva. `list_conflicts()` jämförde
dem direkt i Python och kastade `TypeError: can't compare offset-naive and offset-aware
datetimes`. Fixen normaliserar alla range-/konfliktgränser till samma naiva UTC-form.
Regressionstestet använder nu uttryckligen `timezone.utc`-medvetna indata.

## Gränser och kvarstående

- Ingen GitHub write-access: ingen push, PR, merge eller exakt-head CI. Leveransen är en
  verifierad lokal bundle.
- Inga produktions-VPS-ändringar, inga nya credentials, ingen autonom aktivering.
- Ingen extern kalendersynk och inga autonoma kalenderändringar.
- Full fyratimmars soak och produktionsresursmätning är inte körda; de ingår inte i denna
  leverans. Befintliga workspace-tester täcker idempotens, stale och interrupted sync.
- Den nya acceptansen använder riktig PostgreSQL men inte en riktig LLM; chat/retrieval
  använder befintliga deterministiska testfixtures när den vägen körs.

## Reproduktion

```bash
cd backend
export DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_workmgmt_test
export APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_workmgmt_test
export REDIS_URL=redis://127.0.0.1:56379/0
export LIFEAI_TEST_DATABASE_NAME=lifeos_workmgmt_test
python -m pytest tests/backend/test_personal_work_management_api.py -q -s
python -m pytest tests/backend/test_workspaces_api.py tests/backend/test_projects_detail.py \
  tests/backend/chat/test_chat_request_idempotency.py -q

cd ../frontend
npm run lint && npx tsc --noEmit && npm run build

export LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib
export E2E_FOUNDER_PASSWORD='TestFounderPassword123!'
export E2E_INTERNAL_API_URL=http://127.0.0.1:8010
npx playwright test e2e/personal-work.spec.ts
```

Backend-harnessen startas med `python scripts/ci/run_e2e_backend.py` och `COOKIE_SECURE=true`
på localhost, med en migrerad isolerad databas. Detta är samma harness som befintliga
Playwright-sviter använder; endast AI- och e-posttransporten är fake.
