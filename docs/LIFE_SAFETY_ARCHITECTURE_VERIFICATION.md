# Säkerhetsarkitektur — verifiering mot Grundardirektivets Item G (2026-11-14)

Grundardirektivet ("Turn Life into a working engineering coordinator") skriver explicit:

> G. Extend the existing safety architecture, not create a new security AI:
> * AI is an unprivileged, replaceable coordinator
> * Security Kernel remains independently authoritative
> * Compromised workers are isolated, never imported into Founder MainAI
> * Recovery/control room uses a separately verified environment
> * No unrestricted credentials or hidden backdoors

Ordvalet "**extend**, not create a new security AI" gör detta till en verifierings- och
dokumentationsuppgift, inte en ny-kod-uppgift: varje punkt ska kontrolleras mot BEFINTLIG kod
med konkret filref, aldrig antas eller påstås utan belägg. Det här dokumentet följer samma
disciplin som `app/mainai_execution/evidence_reconciliation.py`s `activated`-fält och
Founder Alpha-granskningens "UNKNOWN/BLOCKED, never PASS": en punkt som inte kan beläggas
redovisas ÄRLIGT som GAP, inte som "troligen OK".

Metod: en research-pass läste igenom relevant kod (RLS, `agent_coordination`, `worker.py`,
`recovery_approval.py`, `adapter_config.py`, samt en grep över hela repot för de exakta
termerna "Security Kernel" och "first_cycle_go"). Inget av det som redovisas nedan som
BEKRÄFTAT är gissat; varje GAP är verifierad frånvaro (grep: 0 träffar), inte en förbisedd
sökning.

## 1. "AI is an unprivileged, replaceable coordinator" — **BEKRÄFTAT**

**Unprivileged:** `app/rls.py`s `apply_mainai_execution_privileges()` (rad ~712) återkallar
`UPDATE`/`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER` från den begränsade runtime-rollen
`mainai_app` på varje MainAI-styrd tabell (mönstret upprepat för `work_candidates`,
`github_evidence_candidates`, m.fl. denna session) och GRANTar `EXECUTE` på smala
`erase_own_*_children()`-funktioner istället för rå `DELETE`. Funktionen verifierar
dessutom de FAKTISKA privilegierna efter boot (rad 784–816) — den litar inte på att
GRANT/REVOKE-satserna kördes, och vägrar boota om anslutningen som verifierar är
autentiserad som `mainai_app` själv ("refusing to treat the restricted runtime role as
trustworthy", rad 786–788). `app/mainai_execution/approval.py`s `require_task_approval()`
är den enda vägen förbi godkännande-grinden, och inget kodpath i exekveringskedjan låter
MainAI bevilja sig själv godkännande.

**Replaceable:** `app/worker.py` beskriver sig själv explicit som den omstart-säkra workern;
allt tillstånd (`MainAITask`, `mainai_jobs`, leases) ligger i Postgres, aldrig i
processminnet — en worker-process kan dö och ersättas utan att tillstånd förloras. Inget
singleton-antagande hittades.

## 2. "Security Kernel remains independently authoritative" — **GAP (namngivning)**

`grep -ri "security.kernel"` över hela repot: **0 träffar.** Det finns ingen modul eller
dokumentation som kallar sig "Security Kernel". Det här är ett direktivs-begrepp utan
motsvarande namngiven modul i den här kodbasen — jag påstår INTE en falsk matchning.

Den FUNKTION begreppet troligen syftar på existerar dock och är oberoende auktoritativ:
Postgres-nivåns RLS + SECURITY DEFINER-funktioner i `app/rls.py` verkställs på databaslagret,
utanför och under varje applikationskodpath, och verifieras vid boot (inte bara deklareras).
Men det finns ingen enskild modul/dokument som uttryckligen heter "Security Kernel".
**Öppen fråga till grundaren:** är "Security Kernel" ett framtida namn för RLS-lagret, eller
syftar det på något som ännu inte finns i den här kodbasen (t.ex. en extern, separat
verifierad tjänst)? Jag har inte gissat ett svar.

## 3. "Compromised workers are isolated, never imported into Founder MainAI" — **BEKRÄFTAT**

`app/agent_coordination/dispatch.py:317` `apply_dispatch_result()` — enligt sin egen
docstring "the ONE place a dispatched worker's result is handed back into durable state":
uppdaterar aldrig med en gissning ("never overwrites with a guess"), går alltid via den
BEFINTLIGA tillståndsmaskinen (`transition_status()` — ett anrop från ett icke-`running`
tillstånd ger `InvalidTransitionError`, fail-closed), och skriver evidens via
`record_assignment_execution()`/`record_assignment_outcome()` — aldrig en andra
sanningskälla. En workers output (`head_sha`/`output_refs`/`upstream_pr_ref`) registreras som
EVIDENS, aldrig som direkt betrodd sanning. Det här är exakt samma "signal producer != truth
writer"-doktrin som redan bevisad för `WorkCandidate`/`GithubEvidenceCandidate` denna
session — nu bekräftad att gälla `agent_coordination`s egen worker-resultat-väg också. Ingen
ordagrann "quarantine"/"compromised worker"-text finns, men den strukturella isoleringen
(evidens-bara, grindad tillståndsmaskin) är verklig.

## 4. "Recovery/control room uses a separately verified environment" — **GAP (substans)**

`app/mainai_execution/recovery_approval.py` har en verklig grind
(`RecoveryApprovalRequiredError`/`recovery_requires_approval()`, rad 37–78), och
`execute_takeover()` måste klara den innan återhämtning fortsätter — samma rigör som den
vanliga exekveringsauktoritetskedjan. Men jag hittade INGET belägg för ett separat
"control room"-begrepp eller en bokstavligen SEPARAT VERIFIERAD MILJÖ (t.ex. ett annat
deploy-mål, en annan uppsättning credentials, eller en annan nätverksgräns) för återhämtning
jämfört med normal MainAI-exekvering — det körs i samma process/miljö med en STRÄNGARE
godkännande-grind, inte en SEPARAT verifierad miljö. Den här punkten är INTE sann ännu som
den är ordagrant skriven i direktivet — den läser som framtida arbete, inte ett befintligt
invariant. **Öppen fråga till grundaren:** ska detta byggas (en faktiskt separat
drift-/verifieringsmiljö för recovery), eller räcker den redan existerande,
strängare godkännande-grinden som funktionell motsvarighet?

## 5. "No unrestricted credentials or hidden backdoors" — **BEKRÄFTAT, med en delvis GAP**

`app/agent_coordination/adapter_config.py`s egen modul-docstring: "No credential, secret,
session token, or API key is ever read, stored, or referenced by this module." Den enda
ordagranna "backdoor"-referensen i hela repot är `docs/LIFE_DEVELOPMENT_OPERATOR.md:59`:
"LifeAI itself receives no backdoor: changes to Life use the same isolated, bounded, verified
and governed pipeline" — ett redan existerande, explicit påstående som matchar direktivets
ordval nära.

**Delvis GAP:** `first_cycle_go` (namngivet i direktivets eget "Hard limits"-avsnitt: "Do not
... override `first_cycle_go=false`") har **0 träffar** (`grep -ri "first_cycle_go"`) någonstans
i hela repot. Det är INTE en verkligt verkställd flagga i den här kodbasen idag. Antingen är
det ett direktivs-begrepp för något som ännu inte byggts här, eller syftar det på tillstånd
som hålls i ett system UTANFÖR det här repot (t.ex. en extern kontrollplan grundaren
administrerar direkt). Jag har INTE byggt denna flagga själv — att göra det utan att veta
vad den egentligen ska gate:a vore att gissa en säkerhetsmekanism, exakt det direktivets
Item G varnar mot ("not create a new security AI"). **Öppen fråga till grundaren:** var lever
`first_cycle_go` idag, och om den inte finns än — vad exakt ska den spärra, och var hör den
hemma?

## Sammanfattning

| # | Punkt | Status |
|---|---|---|
| 1 | Unprivileged, replaceable coordinator | **Bekräftat** |
| 2 | Security Kernel oberoende auktoritativ | **Gap** — begreppet saknar namngiven motsvarighet; funktionen (RLS) finns |
| 3 | Komprometterade workers isolerade | **Bekräftat** |
| 4 | Recovery/control room, separat verifierad miljö | **Gap** — grind finns, separat MILJÖ finns inte |
| 5 | Inga obegränsade credentials / backdoors | **Bekräftat** (credentials/backdoors); **delvis gap** (`first_cycle_go` saknas helt) |

Tre öppna frågor ovan (punkt 2, 4, 5) är avsiktligt INTE lösta här — de är arkitekturbeslut
som enligt `CLAUDE.md` ska pausas på och lyftas till grundaren, inte avgöras enväldigt av en
enskild branch. Se `docs/BRANCH_REGISTRY.md` för motsvarande öppna fråga om Item F:s
three-way worker-execution-konvergens, som följer samma princip.
