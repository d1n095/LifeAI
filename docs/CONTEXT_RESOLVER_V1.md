# Conversation Context Resolver v1

**Kod:** `backend/app/context/resolver.py`. **Tester:** `backend/tests/backend/test_context_resolver.py`
(26 tester). **Kopplat till chatt:** `backend/app/routers/chat.py` (`resolve_context()` anropas
efter historikhämtning, resultatet exponeras på svaret som `context_intent`/
`context_confidence`).

## Vad det är

En liten, regelbaserad klassificerare (INTE ett LLM-anrop) som bedömer vilken TYP av tur
grundarens senaste meddelande är, givet den senaste konversationshistoriken. Syftet är att ge
en granskningsbar, testbar, deterministisk första bedömning som framtida beteende (t.ex. hur
mycket historik som vägs in, om MainAI ska fråga tillbaka) kan byggas på — inte ett påstående
om att systemet "förstår" konversationen.

**Varför regelbaserat och inte en LLM-klassificering:** varje bedömning är granskningsbar och
testbar utan ett providernyckel, deterministisk (samma indata ger alltid samma utdata — viktigt
för test), och billig (inget extra AI-anrop per meddelande). Priset är att det är en heuristik,
inte äkta språkförståelse — se avsnittet om kända begränsningar nedan.

## Integrationsstatus ikväll: rent observationellt

`resolve_context()` anropas i `chat.py` EFTER att historiken redan hämtats, och resultatet
används INTE för att ändra retrieval, systemprompten eller vilken historik som skickas till
providern. Det exponeras bara på svaret (`context_intent`, `context_confidence`) så att
grundaren och en framtida UI/beteende kan se och bygga vidare på klassificeringen utan att
dagens chattbeteende beror på den. Detta är en medveten avgränsning, inte en glömd koppling.

## Kategorier

| Intent | Betydelse |
|---|---|
| `continuation` | Fortsätter samma spår ("nu då?", "nästa", "gör samma") |
| `new_topic` | Byter ämne |
| `correction` | Rättar något MainAI eller grundaren själv nyss sa |
| `question_about_previous` | Frågar om ett tidigare svar ("vad menade du?") |
| `pronoun_reference` | Kort, pronomentungt meddelande som syftar på något redan i konversationen |
| `navigation_question` | Frågar hur man gör något i gränssnittet ("vad ska jag trycka på?") |
| `work_command` | Ber MainAI göra/skapa/ändra något |
| `idea_worth_saving` | En idé värd att spara |
| `explicit_memory` | Explicit ber MainAI komma ihåg något |
| `uncertain_reference` | Osäker referens — ett förstklassigt resultat, inte ett dolt fel |

## Fönster

`MAX_VERBATIM_MESSAGES = 5` (de senaste meddelandena används ordagrant), `MAX_WEIGHTED_MESSAGES
= 15` (ett bredare fönster används för ämnesöverlappsberäkning). `LONG_GAP = 30 minuter` —
en lång tidslucka nedgraderar en annars hög konfidens på `continuation`/`pronoun_reference`
till `medium` (ett meddelande efter 45 minuters tystnad är en svagare fortsättningssignal än
ett skickat 10 sekunder senare).

## Klassificeringsordning (precedens)

Matcharna körs i fast ordning, första träff vinner:
`explicit_memory -> correction -> navigation_question -> question_about_previous ->
idea_worth_saving -> continuation -> pronoun_heavy -> work_command -> ämnesöverlapp-fallback`.

Ordningen är medveten: en explicit minnesmarkör ("kom ihåg det här") ska aldrig missas för att
meddelandet också råkar innehålla ett arbetskommando-ord, t.ex.

## Obligatoriska ordagranna testfraser (alla verifierade i test_context_resolver.py)

`"nu då?"`, `"nästa"`, `"gör samma"`, `"vad ska jag trycka på?"`, `"han är klar nu"`,
`"lägg till det i förra"`, `"den finns inte där"`, `"sluta tappa tråden"` — plus scenarierna
ämnesbyte-och-återgång, tidslucka mellan meddelanden, två projekt med liknande namn, och
gammal kontext ersatt av ny information.

## Den absoluta begränsningen: ingen psykologisk inferens

**Modulen har ingen vokabulär eller logik för stress, humör eller känslotillstånd
överhuvudtaget — genuint frånvarande, inte "upptäcker men undertrycker".** Om en framtida
anropare vill ha en sådan signal får den ENDAST komma från att användaren själv uttryckt det
explicit, ordagrant, aldrig inferens från tonläge, ordval eller skiljetecken. Verifierat av
`test_never_infers_emotional_or_psychological_state`, som skannar de faktiska
markörlistkonstanterna (inte hela modulen — modulens egen docstring nämner legitimt
"stress"/"humör" när den FÖRKLARAR begränsningen, vilket annars gett en falsk testträff).

## Kända begränsningar (dokumenterade, inte dolda)

- **`_topic_overlap()` är ordöverlapp, inte semantisk förståelse.** Ett nytt meddelande som
  råkar dela ord med tidigare historik (utan att faktiskt handla om samma sak) kan felaktigt
  klassificeras som en fortsättning. Detta är en känd avvägning för en billig, granskningsbar
  heuristik — inte ett påstått fullständigt löst problem.
- **Markörlistorna är korta och bokstavliga med avsikt** — lättare att granska och lita på än
  en stor fuzzy-matchande lista, men missar formuleringar som inte finns i listan. Ett exempel
  som redan hittades och åtgärdades under utveckling: "där"/"dit" togs bort ur pronomenlistan
  eftersom de är extremt vanliga svenska lokativa adverb ("där borta") som gav fler falska
  positiva än sanna pronomen-referenser — "den finns inte där" fångas ändå korrekt via en mer
  specifik `_CORRECTION_MARKERS`-fras.
- **Projekt-namn-disambiguering** föredrar det mest specifika (längsta) matchande namnet när
  ett namn är en delsträng av ett annat (t.ex. "Life OS" vs. "Life OS Mobile") istället för att
  flagga falsk tvetydighet — men två genuint olika namn som båda matchar text räknas
  fortfarande som verklig tvetydighet (`project_ambiguous=True`, inget namn gissas).
- **Inget persisteras ännu.** Klassificeringen beräknas per anrop och sparas inte som en egen
  rad — se `docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md`s "inte byggt ikväll"-avsnitt för vad en
  framtida djupare integration (t.ex. att faktiskt variera hur mycket historik som skickas till
  providern beroende på klassificering) skulle kräva.
