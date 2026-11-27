# Tillfälliga fel — verkligt E2E (återhämtning, backoff, attempts-budget)

**Branch:** `freebuff/transient-failure-recovery-20261127`
**Bas:** `freebuff/governed-execution-park-20261127` (`09ed1bf8`, F1 + F7)

Ingen merge till default, ingen produktionsdeployment, ingen ändring av `POLICY_LOCK`, ingen
påverkan på befintlig staging, `first_cycle_go=false` oförändrat. Branchen innehåller **bara**
verifieringen och dess fynd — ingen produktionskod ändras.

## Varför ett eget E2E

F1/F7-verifieringen körde hela vägen mål → plan → task → jobb → verifiering, men den rörde
aldrig den **tillfälliga** felvägen. Enhetstesterna för `lease.py`, `recovery` och
`execute_takeover` bevisar var för sig att klockan räknas rätt, men inte att en **dödad
worker** mitt i ett körande jobb faktiskt leder tillbaka till arbete — eller att budgeten
stoppar en task som felar om och om igen. Det är den kedjan som mäts här, i den isolerade
stacken, med riktig Postgres, Redis, backend, worker och Ollama.

Ett tillfälligt fel modelleras som det det är i produktion för den här motorn: **workern
dör** (SIGKILL) mitt i ett körande jobb. Lease-utgång, takeover och omförsök är den riktiga
transientvägen — ingen påhittad felflagga, ingen påtvingad status.

## Vad skriptet gör

`e2e-transient.py` (körs mot den isolerade stacken, se `evidence/e2e-transient.txt` för
körningen):

**Fas 1 — återhämtning**
1. Loggar in som grundare och skapar ett mål via `/api/mainai/execution/goals`, planerar och
   godkänner taskarna (`open_pr` avbryts — den kräver remote-write som körningen medvetet inte
   ger).
2. Väntar tills en `repo_edit`-task är **running** och `kill -9`:ar workerprocessen.
3. Verifierar att processen är borta, startar workern igen med det riktiga startskriptet.
4. Väntar på taskens **durabila** utfall och kräver: `completed` på **försök 2 av 3**,
   minst en durabil retry/backoff-händelse, **exakt en** `completed`-händelse (ingen
   dubbelkörning) och ett **nytt** jobb för omförsöket.

**Fas 2 — attempts-budget**
Samma väg, men workern dödas varje gång tasken kör. Kräver att upprepade avbrott aldrig lämnar
tasken hängande: den ska sluta i ett durabilt terminalt läge, `attempts` aldrig över
`max_attempts`, ingen kvarlämnad retry-klocka, inga nya jobb efter att budgeten är slut och
ingen `completed`-händelse för en task som inte är klar.

## Vad den sista körningen visade i fas 2

Fas 2 kräver att upprepade avbrott aldrig lämnar tasken hängande. I den sista, gröna körningen
hann `run_tests`/`repo_edit`-tasken bli `completed` på första försöket innan ett andra avbrott
kunde läggas (receptet är snabbt; fönstret är några sekunder), så körningen bevisar att
budgeten **aldrig överskrids**, att inget jobb skapas i efterhand och att ingen retry-klocka
lämnas kvar — men den bevisar inte att budgeten **tar slut** i just den här körningen. Det
gjorde de tidigare körningarna (5 avbrott i rad), och de avslöjade samtidigt T2 nedan: tasken
blev `running` med ett `queued` jobb och `attempts = 1` utan att någon väg drev den vidare.
Budgetens hårda stopp är alltså enhetstestat (`test_supervisor_*`), inte E2E-bevisat.

## Fynd (nya, verkliga — inte åtgärdade i den här branchen)

**T1 — en SIGKILL mitt i en tick lämnar mål-leasen aktiv i upp till en timme.**
`run_supervisor()` claimar `supervisor_goal_leases` i en egen committad transaktion
(`dispatch_ready_task()`), med TTL `2 × längsta enskilda operator-åtgärd` = 3 600 s. F7
släpper leasen när **ticken kastar** — men en dödad process kastar ingenting. Efter
SIGKILL:en låg raden kvar `active` med `expires_at = acquired_at + 1 h`, och målet kunde inte
plockas upp igen förrän TTL:en gått ut. Det är en medveten avvägning (leasen får aldrig gå ut
under en genuint lång åtgärd), men konsekvensen — upp till en timmes stillestånd för just det
målet efter en krasch — bör hanteras, t.ex. med kort lease som förnyas per task, eller
återtag när `worker_id`/generation bevisligen är en annan process. **Rapporteras, åtgärdas
inte här** (egen branch; rör lease-kontraktet som F7 just ändrat).

E2E:t väntar därför inte ut timmen: `expire_goal_lease()` flyttar leasen till det exakta
tillstånd den har **vid TTL:ns slut** (`expires_at = now() - 1 s`) och rör ingenting annat.
Jobbets egen lease-utgång, takeover och omförsök sköts av den riktiga återhämtningspipen.

**T2 — en krasch mellan dispatch-commit och jobbclaim lämnar tasken `running` utan återhämtning.**
Om workern dör efter att `dispatch_ready_task()` committat men innan jobbet claimats ligger
jobbet kvar `queued` och tasken `running`. Ingen av de befintliga vägarna tar den vidare
(jobbklaimningen hämtar `queued`-jobb men tasken är inte längre `ready`; recovery/takeover
utgår från ett jobb som faktiskt kört). Resultat: tasken står still på `attempts = 1`,
`next_retry_at = NULL`, och **attempts-budgeten triggar aldrig**. Reproducerat i körningen
2026-11-27 (se `evidence/e2e-transient.txt`, "T2-reproduktion"). E2E:t dödar därför bara
taskar som är **genuint running** — det tillståndet ska inte tillverkas artificiellt av
testet, men fyndet är verkligt och bör få en egen branch.

## Vad som INTE verifierats

- Ingen webbläsare i miljön: UI:t är inte granskat (körningen är ren API/DB/worker).
- Ingen docker/sudo: containerbaserad verifiering är NOT_RUN.
- F2:s commit-steg kräver `stage_scoped_changes`/`commit_scoped_changes` i envelopen; det
  E2E:t från F1/F7 saknar dem, så F2 verifieras av sina enhetstester + supervisor-sviten, inte
  här.
