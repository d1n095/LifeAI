# Egen domän (Strato) — plan, inte ännu utförd

Du äger en domän hos Strato. Det här dokumentet beskriver **exakt vilka DNS-poster** som
kommer behövas för att köra MainAI på `app.<din-domän>` med en verifierad avsändare
`noreply@<din-domän>`, och hur applikationens egna miljövariabler ska sättas när det görs.

**Inget av detta är genomfört än.** Domännamnet fylls i och DNS-kopplingen görs först efter att
den lokala implementationen och minnestesterna av den kombinerade containern är godkända —
det här dokumentet är förberedelsen, inte utförandet. Ingen kod i det här repot refererar
ännu till en riktig domän; `render.yaml` pekar fortfarande på `lifeai-1.onrender.com`.

**Strato-inloggningsuppgifter lagras aldrig i kod, GitHub eller miljövariabler.** DNS-posterna
nedan skrivs in manuellt i Strato's DNS-panel av dig — det enda som hamnar i det här repot är
domännamnet självt (i `render.yaml`s `FRONTEND_ORIGINS`/`PUBLIC_APP_URL` och liknande) och
`render.yaml`s Custom Domain-konfiguration, aldrig ett lösenord eller en API-nyckel till
Strato. Om en e-postleverantör (Resend/Brevo, se nedan) behöver ett API-nyckel för att skicka
mejl går den till Render som `SMTP_PASSWORD`/motsvarande (redan `sync: false` i `render.yaml`
— fylls i manuellt i Render-dashboarden, aldrig committat), inte till Strato.

## 1. `app.<din-domän>` som publik adress

Render Free stödjer anpassade domäner ("Custom Domains") på webbtjänster utan extra kostnad —
Render provisionerar TLS-certifikatet automatiskt via Let's Encrypt så snart DNS-posten pekar
rätt.

**DNS-post att lägga till i Strato:**

| Typ | Namn | Värde | TTL |
|---|---|---|---|
| CNAME | `app` | `lifeai-1.onrender.com` (den nuvarande Render-hostnamnet — verifiera exakt värde i Render Dashboard → tjänsten `LifeAI` → Settings → Custom Domains innan du lägger till posten, Render visar där den exakta CNAME-target den vill ha) | 3600 (eller Stratos standard) |

**Klick-steg (görs senare, inte nu):**

1. Render Dashboard → tjänsten `LifeAI` → Settings → Custom Domains → Add Custom Domain →
   ange `app.<din-domän>`.
2. Render visar en CNAME-post att lägga till — lägg till exakt den i Strato's DNS-panel.
3. Vänta på DNS-propagering (kan ta allt från minuter till någon timme) och att Render visar
   domänen som verifierad med ett giltigt TLS-certifikat.
4. Uppdatera `render.yaml`s `FRONTEND_ORIGINS`/`PUBLIC_APP_URL` från
   `https://lifeai-1.onrender.com` till `https://app.<din-domän>` (se avsnitt 3 nedan för hela
   listan av variabler det påverkar).

## 2. `noreply@<din-domän>` som verifierad avsändare

`SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_EMAIL` är redan `sync: false` i
`render.yaml` (fylls i manuellt i Render-dashboarden — se `docs/RENDER_DEPLOY.md`). Att
avsändaradressen ska vara `noreply@<din-domän>` istället för en delad/generisk adress kräver
att den e-postleverantör du väljer (t.ex. Resend eller Brevos gratisnivå, båda redan nämnda i
`docs/RENDER_DEPLOY.md`) verifierar att du äger domänen — det görs genom att lägga till DNS-
poster **den leverantören anger i sin egen dashboard efter att du lagt till domänen där**.
Exakta DKIM-postens värde går inte att förutsäga här (det är en leverantörsgenererad
publik nyckel, unik per konto) — nedan är strukturen alla vanliga leverantörer (Resend, Brevo,
SES, m.fl.) använder, så du vet vad du letar efter när du är där.

### SPF

En (1) SPF-post per domän — om du redan har en SPF-post (t.ex. för annan e-post på domänen)
måste leverantörens `include:` läggas till i den **befintliga** posten, inte som en ny separat
TXT-post (DNS tillåter bara en SPF-post per domän; flera TXT-poster med `v=spf1` bryter
valideringen).

| Typ | Namn | Värde (exempel — den valda leverantören anger sitt exakta `include:`-värde) | TTL |
|---|---|---|---|
| TXT | `@` (root) eller den subdomän mejl skickas "From:" | `v=spf1 include:<leverantörens-spf-host> ~all` | 3600 |

### DKIM

| Typ | Namn (leverantören anger exakt prefix, ofta något i stil med) | Värde |
|---|---|---|
| TXT eller CNAME | `<selector>._domainkey.<din-domän>` (t.ex. `resend._domainkey` eller `mail._domainkey`) | Leverantörens publika nyckel — kopieras exakt från deras dashboard efter att du lagt till domänen där |

### DMARC

En DMARC-post är inte strikt obligatorisk för att SMTP-leverantören ska skicka mejl, men
rekommenderas starkt — utan den kan mottagande mejlservrar (Gmail m.fl.) fortfarande ta emot
mejlet men behandla det mer misstänksamt, vilket ökar risken att verifierings-/
återställningsmejl hamnar i skräppost.

| Typ | Namn | Värde | TTL |
|---|---|---|---|
| TXT | `_dmarc.<din-domän>` | `v=DMARC1; p=quarantine; rua=mailto:<en-adress-du-läser>@<din-domän>` | 3600 |

`p=quarantine` (inte `p=reject`) rekommenderas som startpunkt — `reject` kan blockera legitima
mejl om SPF/DKIM inte är perfekt konfigurerade från början; höj till `p=reject` efter att
`rua`-rapporterna bekräftat att SPF/DKIM passerar konsekvent.

**Klick-steg (görs senare, inte nu):**

1. Skapa konto hos vald leverantör (Resend eller Brevo, gratisnivå — verifiera exakt
   gratisnivåns skickningsgräns i deras dashboard innan produktionsanvändning).
2. Lägg till `<din-domän>` (eller en subdomän som `mail.<din-domän>`, leverantörens val) som
   "sending domain" i deras dashboard.
3. Kopiera exakt de DNS-poster leverantören visar (SPF `include:`, DKIM-selector och -nyckel,
   ev. deras egen rekommenderade DMARC-post) till Strato's DNS-panel.
4. Vänta på att leverantören visar domänen som verifierad.
5. Sätt `SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_EMAIL=noreply@<din-domän>`
   i Render-dashboarden (manuellt, `sync: false` — se `docs/RENDER_DEPLOY.md`).

## 3. Vad som ändras i `render.yaml` när domänen är på plats

När DNS-posterna i avsnitt 1 och 2 är verifierade, dessa `render.yaml`-värden byts från
`lifeai-1.onrender.com` till `app.<din-domän>` (fortfarande `https://`, ändrar bara värdnamnet):

| Variabel | Nytt värde |
|---|---|
| `FRONTEND_ORIGINS` | `https://app.<din-domän>` |
| `PUBLIC_APP_URL` | `https://app.<din-domän>` |

`COOKIE_SECURE=true` och `COOKIE_SAMESITE=none` (redan satta) kräver ingen ändring — de är
domän-agnostiska. `SMTP_FROM_EMAIL` sätts separat (avsnitt 2 ovan), inte här.

Detta är en enrads-URL-ändring i en redan committad fil — inget nytt Blueprint-fält, inget nytt
DNS utöver det som redan beskrivs ovan. Den här ändringen görs (och pushas, med separat
godkännande som allt annat i den här arkitekturen) efter att domänen är verifierad i både
Render och hos e-postleverantören, inte innan — annars pekar `PUBLIC_APP_URL` på en domän som
ännu inte serverar något.
