# Life — långsiktig produktvision (uppskjuten) & principer för externa verktyg/leverantörer

**Status: PLANERAD, EJ PÅBÖRJAD — ren dokumentation.** Det här dokumentet konsoliderar
grundarens tidigare direktiv om att samla den långsiktiga LifeAI-visionen, nu kompletterat
med nyligen klargjorda principer (grundardirektiv 2026-11-15, Priority 4). Ingen kod i det
här dokumentet är byggd eller ska byggas förrän den uttryckliga spärren nedan är uppfylld.
Det ändrar ingenting i produktionskoden, aktiverar ingenting och fattar inget tekniskt
beslut i förväg.

> **Byggspärr:** inget i det här dokumentet påbörjas förrän MainAI:s grund är färdig — se
> `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` (arkitektur, datamodell, byggordning §8) och
> `docs/ROADMAP.md` (fasindelad plan). Det här dokumentet är avsiktligt skrivet nu, medan
> principerna är färska efter grundarens direktiv, men det är dokumentation — inte ett
> åtagande om när arbetet startar, och inte en genväg förbi den ordningen.

Genomsökt innan det här skrevs: `docs/ROADMAP.md`, `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`,
`docs/LIFE_LIBRARY_PLAN.md`, samt alla `docs/LIFE_*.md`- och `docs/MAINAI_*.md`-filer för
"ElevenLabs", "patent", "mining"/"compute" eller en motsvarande konsoliderad visionsdokument.
Inget existerande dokument täcker det här — ingen duplicering skapas.

## 1. Princip: studera externa verktyg, samla inte på leverantörer

Grundprincipen som styr ALLA framtida produktbeslut i det här dokumentet, och som redan
gäller för hur MainAI byggs idag (se `docs/ROADMAP.md`s "Icke-mål": ingen
SaaS-mellanhand/LangChain/LlamaIndex undveks medvetet redan i MVP-fasen):

- **Life ska studera externa verktyg för användbara tekniska idéer — inte ackumulera
  onödiga leverantörer.** Ett externt verktyg undersöks för ATT FÖRSTÅ hur det löser ett
  problem, inte som en standardväg att integrera det rakt av.
- **Föredra egna, modulära implementationer där det är motiverat.** "Motiverat" betyder: när
  en egen implementation ger bättre kontroll över data/sekretess, färre beroenden, eller en
  arkitektur som redan passar MainAI:s egna lager (trust-zoner, RLS, egress-policy) bättre än
  ett externt verktygs antaganden gör.
- **Underhåll ersättningsbara, säkerhetsgranskade tredjepartsutökningar när de verkligen är
  användbara.** Det är inte ett förbud mot externa beroenden — det är ett krav på att varje
  beroende som väljs (a) kan bytas ut utan att låsa in hela systemet, och (b) genomgår samma
  säkerhetsgranskningsdisciplin som allt annat i det här repot (se `CLAUDE.md`s
  "Isolera, verifiera, samordna och granska").

Den här principen gäller retroaktivt som tolkningsram för varje produktområde nedan — inget
av dem ska tolkas som "bygg en integration mot leverantör X" förrän ett konkret, motiverat
behov finns, och aldrig som ett permanent låst val.

## 2. Life Innovation & Patent Intelligence (uppskjutet produktområde)

Ett framtida kunskaps-/analysområde ovanpå Life Library (se `docs/LIFE_LIBRARY_PLAN.md`) för
att hjälpa grundaren förstå existerande innovationer, patentlandskap och tekniska
möjligheter inom ett valt område — INTE ett automatiserat patentansöknings- eller
juridikverktyg, och inte en ersättning för professionell patentrådgivning.

**Vad det är tänkt att göra, på ett konceptuellt plan:**
- Strukturerad sökning/sammanställning av offentligt tillgänglig patent- och
  innovationsinformation, kopplad till Life Library-arkitekturens redan existerande
  källspårning/proveniens (källa → tolkning → osäkerhet → konflikt per påstående, samma
  Trust Engine-princip som resten av biblioteket).
- Jämförande analys ("vad finns redan, vad är nytt, var är vita fläckar") — inte juridisk
  rådgivning, inte automatiserad patentansökan.

**Explicit INTE låst här:** vilken specifik datakälla, API eller tredjepartstjänst (om någon)
som används för patentdata är ett framtida, motiverat beslut — i linje med §1, undersöks
externa verktyg för idéer innan något integreras permanent.

## 3. ElevenLabs-forskning (uppskjutet produktområde)

Ett undersökningsspår, inte ett åtagande att integrera ElevenLabs permanent. Kopplar till
den redan planerade LifeCast-podcastproduktionen i `docs/LIFE_LIBRARY_PLAN.md` (L0–L8,
generering av kunskapsprodukter inklusive ljud).

**Vad "forskning" betyder här, konkret, enligt §1:**
- Studera ElevenLabs (och jämförbara röstsyntes-/ljudgenereringsverktyg) för att FÖRSTÅ
  tekniska avvägningar: latens, röstkvalitet, flerspråksstöd, kostnadsmodell,
  dataskydds-/sekretesshantering för uppladdat röstmaterial.
- Avgöra om ett EGET, enklare röstsyntesval (lokal modell, eller en annan leverantör) bättre
  passar MainAI:s egen trust-zon-/egress-arkitektur, eller om ElevenLabs specifikt löser ett
  problem egen implementation inte motiverat kan lösa lika bra.
- Om ElevenLabs (eller någon leverantör) väljs: den integreras som en **ersättningsbar,
  säkerhetsgranskad** tredjepartsutökning — bakom samma typ av provider-abstraktion
  MainAI redan använder för AI-leverantörer (se `docs/AI_PROVIDER_ARCHITECTURE.md`), aldrig
  hårdkodad som ett permanent beroende.

## 4. Multi-coin Life Compute & Mining + Life Mining Advisor (uppskjutet produktområde)

Ett framtida verktyg för att hjälpa grundaren resonera om beräkningsresurser (egen hårdvara,
GPU-kapacitet som redan nämns i `docs/ROADMAP.md` Fas 4) kan användas för kryptovaluta-mining
som en sidovinst, samt för att jämföra mining mot andra användningar av samma hårdvara
(t.ex. lokal AI-inferens, se samma Fas 4).

**Life Mining Advisor måste jämföra, konkret, innan någon rekommendation ges:**
- **Kompatibla mynt** — vilka kryptovalutor som faktiskt kan minas med tillgänglig/planerad
  hårdvara.
- **Algoritmer** — vilken hash-/konsensusalgoritm varje mynt kräver, och hårdvarans faktiska
  lämplighet för den.
- **Nuvarande utrustning** — vad som redan finns, dess faktiska prestanda (inte
  tillverkarens marknadssiffror).
- **Uppgraderingar** — vad en eventuell uppgradering skulle kosta kontra tillföra.
- **Energikostnader** — faktisk el-/driftskostnad per tidsenhet, inte en engångsuppskattning.
- **Temperaturer** — termisk belastning och dess effekt på hårdvarans livslängd/stabilitet.
- **Svårighetsgrad (difficulty)** — nätverkets aktuella och trendmässiga svårighetsgrad per
  mynt, eftersom den direkt påverkar lönsamheten över tid.
- **Förväntad lönsamhet** — en beräkning som faktiskt kombinerar alla punkter ovan, inte en
  enskild "hashrate"-siffra isolerat.

**Explicit krav från grundaren, bindande för hela det här produktområdet:**
- **Lås inte in framtida teknikval.** Life Mining Advisor ska vara konstruerad för att kunna
  jämföra NYA mynt/algoritmer/hårdvara när de dyker upp — aldrig hårdkodad mot dagens
  specifika lista.
- **Anta inte lönsamhet.** Verktyget ska presentera en beräkning baserad på aktuella,
  verifierbara siffror (energikostnad, svårighetsgrad, etc.) — aldrig ett påstående om att
  mining ÄR lönsamt som en inbyggd förutsättning. Lönsamhet kan vara negativ, och verktyget
  måste kunna visa det ärligt.

## 5. Gemensamt för alla produktområden ovan

- **Allt i det här dokumentet är uppskjutet tills MainAI:s grund är klar** (se byggspärren
  överst). Det här är inte en prioritetsordning mellan de tre produktområdena sinsemellan —
  ordningen bestäms när grunden faktiskt är klar, inte här.
- Inget av produktområdena ovan har en vald leverantör, ett valt bibliotek eller en vald
  dataskälla idag. Varje sådant val är ett separat, framtida, motiverat beslut som §1:s
  princip styr — aldrig förutsatt av att det nämns i det här dokumentet.
- Det här dokumentet introducerar inget nytt exekveringssystem, ingen ny
  auktoritets-/godkännandeväg och ingen ny leverantörsintegration. Det är rent beskrivande.

## Se även

- `docs/ROADMAP.md` — fasindelad plan, Fas 4 (lokal modell/GPU-kapacitet, relevant för §4).
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — byggordning (§8); allt ovan väntar på den.
- `docs/LIFE_LIBRARY_PLAN.md` — Life Library/LifeCast, relevant kontext för §2 och §3.
- `docs/AI_PROVIDER_ARCHITECTURE.md` — den existerande provider-abstraktionsmodell §3:s
  "ersättningsbar, säkerhetsgranskad" princip ska återanvända, inte duplicera.
- `CLAUDE.md` — "Isolera, verifiera, samordna och granska", grundprincipen §1 bygger vidare på.
