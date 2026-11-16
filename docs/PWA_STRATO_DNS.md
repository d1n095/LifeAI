# MainAI PWA — installerbar via egen subdomän (STRATO DNS)

**Status: runbook. INGEN DNS-ändring och INGEN deploy görs av den här branchen.** Det här
beskriver exakt hur en dedikerad subdomän pekas mot den *befintliga* hostingen så att MainAI
kan installeras som en PWA på macOS och Google Pixel. Grundaren utför stegen manuellt när han
vill; ingenting här triggas av CI eller en merge. Produktions-DNS ändras aldrig utan explicit
godkännande.

## Vad PWA:n kräver av infrastrukturen

En installerbar PWA (och WebAuthn/passkeys) kräver bara tre saker, som den nuvarande
arkitekturen redan ger:

1. **Stabil HTTPS-origin** — Caddy sköter automatisk TLS (Let's Encrypt) för `{$DOMAIN}`
   (se `docs/STRATO_VPS_DEPLOY.md`). En PWA installeras bara från ett säkert ursprung.
2. **Web app manifest + service worker + ikoner** — levereras nu av frontend:
   `/manifest.webmanifest` (`app/manifest.ts`), `/sw.js` (`public/sw.js`),
   `/icons/*` (`public/icons/`). Inget backend-arbete behövs.
3. **Samma origin för appen** — webbläsaren pratar bara med frontend-origin; backend nås bara
   via frontendens egen `/api/*`-proxy (`app/api/[...path]/route.ts`) på det privata nätverket.
   PWA:n ändrar ingenting i den modellen.

Passkeys: WebAuthn RP ID = subdomänens registrerbara domän. En stabil HTTPS-subdomän är själva
förutsättningen; PWA:n försvagar inte autentiseringen (HttpOnly-cookies + CSRF oförändrade, se
`frontend/docs/AUTH_THREAT_MODEL.md`). Service workern cachar **aldrig** `/api/*` eller någon
autentiserad vy — bara publika, hashade statiska assets.

## Steg 1 — Välj subdomän

T.ex. `app.<din-domän>` (byt `<din-domän>` mot den riktiga domänen). Resten antar `app.`.

## Steg 2 — STRATO DNS-post

I STRATO:s kundpanel → **Domäner → DNS-inställningar** för domänen:

- **VPS-hosting (Caddy, nuvarande rekommenderade arkitektur):** lägg till en **A-post**
  (och **AAAA** om servern har IPv6) för värdnamnet `app` som pekar på VPS:ens publika IP:

  | Typ  | Namn (host) | Värde              | TTL   |
  |------|-------------|--------------------|-------|
  | A    | `app`       | `<VPS_PUBLIK_IPv4>`| 3600  |
  | AAAA | `app`       | `<VPS_PUBLIK_IPv6>`| 3600  |

  (STRATO visar ofta `@` för apex; för en subdomän anges bara `app` som host.)

- **Render-hosting (äldre alternativet, se `docs/RENDER_DEPLOY.md`):** lägg i stället till en
  **CNAME** `app` → det CNAME-mål Render anger för tjänstens custom domain, och registrera
  subdomänen i Render-dashboardens *Custom Domains*.

Inga wildcards, ingen apex-flytt — bara den dedikerade subdomänen. Den befintliga hostingen och
backend rörs inte.

## Steg 3 — Låt Caddy utfärda certifikat för subdomänen

Caddy provisionerar TLS automatiskt när `DOMAIN` (env, se `docker-compose.vps.yml` /
`/etc/lifeai/lifeai.env`) inkluderar den nya subdomänen och portarna 80/443 är nåbara utifrån
(krävs för ACME-utmaningen). Uppdatera `DOMAIN` till `app.<din-domän>` och kör på VPS:en:

```bash
docker compose -f docker-compose.vps.yml up -d
```

(Detta är grundarens vanliga manuella deploy-steg — ingen automatik.)

## Steg 4 — App-origin-variabler

Sätt frontendens publika URL så att t.ex. e-postlänkar och ev. CORS stämmer med den nya
origin:

- `PUBLIC_APP_URL=https://app.<din-domän>`
- `FRONTEND_ORIGINS` — inkludera `https://app.<din-domän>` (behövs bara om någon klient pratar
  korsvis med backend; i proxy-modellen är appen same-origin).

Cookie-flaggor (`cookie_secure=true`) är redan korrekta för HTTPS. Byt inget i auth-modellen.

## Steg 5 — Verifiera

```bash
dig +short app.<din-domän>                      # pekar på rätt IP
curl -I https://app.<din-domän>/manifest.webmanifest   # 200, content-type manifest+json
curl -I https://app.<din-domän>/sw.js                  # 200, content-type javascript
```

- **macOS:** öppna `https://app.<din-domän>` i Chrome/Edge → installera via adressfältets
  install-ikon (eller MainAI:s egen "Installera MainAI"-knapp). Safari: Arkiv → Lägg till i
  Dock. Appen öppnas i ett eget standalone-fönster.
- **Google Pixel (Chrome):** öppna URL:en → menyn → *Installera app* / *Lägg till på
  startskärmen*. Verifiera att den startar i standalone-läge (ingen webbläsar-chrome) och att
  inloggning fungerar.
- Kör gärna Lighthouse → *Installable* för en maskinell kontroll.

## Vad som INTE görs här

- Ingen DNS-post skapas av den här branchen.
- Ingen deploy, ingen ändring av produktionens `DOMAIN`/hosting utan grundarens godkännande.
- Ingen ny backend, inget nytt auth-system, inga inbäddade hemligheter.
