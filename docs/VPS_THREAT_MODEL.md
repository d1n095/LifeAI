# Strato VPS — hotmodell

Vad som faktiskt hotar den här topologin, vad som redan är byggt för att stoppa det, och vad
som medvetet är kvar som en öppen uppföljningspunkt. Bygger vidare på
`docs/VPS_ARCHITECTURE.md`s tillitsgränser — läs den först. Skriven mot vad som faktiskt är
implementerat (verifierat i CI, se `.github/workflows/ci.yml`s `vps-*`-jobb), inte mot en
hypotetisk framtida version.

## Tillgångar (vad som faktiskt är värt att skydda)

| Tillgång | Var | Varför den är känslig |
|---|---|---|
| Sessionskakor (`refresh_token`, access-token) | Webbläsaren, HttpOnly-kaka | Kapning ger full kontoåtkomst — se `docs/AUTH_THREAT_MODEL.md` för den befintliga, redan verifierade auth-hotmodellen (rotation, återanvändningsdetektion, XSS-oläslighet) |
| `/etc/lifeai/lifeai.env` | VPS-disk, `root:root 0600` | Innehåller `SECRET_KEY` (signerar sessionstoken), databasuppgifter, SMTP-uppgifter, `FOUNDER_PASSWORD` |
| Applikationsdata (konversationer, dokument, projekt) | Supabase Postgres | Grundarens privata data — RLS är den sista spärren om ett applikationslager-fel skulle uppstå |
| TLS-privatnycklar | Docker-volymen `caddy_data` | Kapning möjliggör man-in-the-middle mot alla framtida besökare tills certifikatet återkallas |
| VPS:ens SSH-åtkomst | `authorized_keys` för deploy-användaren | Full root-liknande kontroll (deploy-användaren är i `docker`-gruppen, vilket i praktiken är root-ekvivalent) |
| GHCR-imagepush-behörighet | GitHub Actions-hemligheter | Kapning möjliggör att skicka en skadlig image som sedan digest-pinnas och deployas |

## Aktörer/hot som faktiskt är relevanta för den här appen

Grundaren är den ENDA avsedda användaren (`require_founder`, se
`backend/app/deps.py` och `docs/VPS_ARCHITECTURE.md`s publika-registrering-avsnitt) — det
smalnar av hotmodellen betydligt jämfört med en fleranvändar-SaaS:

1. **Opportunistisk internetscanning** (botnät som söker öppna portar/kända sårbarheter) —
   HÖG sannolikhet, LÅG påverkan om mitigerat. Mitigerat av: `ufw` (bara 22/80/443 öppna, se
   `scripts/vps/40_configure_firewall.sh`), backend/frontend har ingen `ports:`-publicering
   alls (verifierat i `vps-compose-verify`), `unattended-upgrades` för säkerhetspatchar (se
   `scripts/vps/50_enable_auto_updates.sh`).
2. **Komprometterad byggkedja** (en skadlig commit/dependency som hamnar i en pushad image) —
   LÅG sannolikhet, HÖG påverkan. Delvis mitigerat: digest-pinning gör att `deploy.sh` aldrig
   kan "råka" deploya en nyare, oinspekterad version av en tag; `npm audit`/CI-tester körs på
   varje push. INTE fullt mitigerat: se `docs/VPS_SUPPLY_CHAIN.md` för vad som återstår
   (SHA-pinning av GitHub Actions, SBOM/sårbarhetsskanning).
3. **Stulen/läckt SSH-nyckel för deploy-användaren** — LÅG sannolikhet, HÖG påverkan.
   Mitigerat av: `41_harden_ssh.sh`s krav på nyckelbaserad auth innan lösenordsauth
   inaktiveras, `PermitRootLogin no`. INTE mitigerat av något automatiskt — om en nyckel
   misstänks läckt måste den manuellt tas bort ur `authorized_keys` och en ny genereras (se
   `docs/VPS_OPERATIONS_RUNBOOK.md`).
4. **Överdimensionerad request-body/resursutmattning** — MEDEL sannolikhet, LÅG-MEDEL
   påverkan. Mitigerat: Caddyfile `request_body { max_size 30MB }` (verifierat i CI att den
   ger `413` innan requesten någonsin når frontend/backend), `pids_limit`/`mem_limit`/`cpus`
   på alla tre tjänster (se `docs/VPS_DOCKER_HARDENING.md`), Redis-baserad rate limiting på
   känsliga endpoints (registrering avstängd i produktion ändå, se nedan).
5. **Självständig kontoregistrering/kontoövertagande** — irrelevant i produktion: publik
   registrering är helt avstängd (`ENVIRONMENT=production` gate, se
   `docs/VPS_ARCHITECTURE.md`s "Vad som skiljer VPS:en"-tabell och backend-testerna för
   `require_founder`). Den här hotvektorn existerar bara i icke-produktionsmiljöer.
6. **Kompromissad Supabase-uppkoppling** — utanför den här VPS-hotmodellens omfattning
   (extern leverantör), men noterat: `DATABASE_URL` är TLS, och `mainai_app`-rollen kan
   aldrig kringgå RLS (se `docs/VPS_ARCHITECTURE.md`s tillitsgränser).
7. **Kompromissad frontend/caddy används som språngbräda mot cache-tjänsten** — MITIGERAT
   genom nätverkstopologi, inte bara lösenord: `redis`/Valkey-tjänsten (**ERSATTE tidigare
   extern Upstash/Redis Cloud-beroende — kör numera privat lokalt på VPS:en**, se
   `docs/VPS_ARCHITECTURE.md`s "Redis vs Valkey") ligger på ett separat privat Docker-nätverk
   (`lifeai_data`, `internal: true`) som ENDAST backend är anslutet till. frontend och caddy
   är inte anslutna till `lifeai_data` överhuvudtaget — även om endera komprometterades helt
   skulle de sakna varje route till cache-tjänsten (ingen DNS-post, inget nätverksgränssnitt
   att skicka trafik över), inte bara nekas av `requirepass`. Cache-tjänsten lagrar heller
   ingen känslig data (bara hastighetsbegränsningsräknare/jobblås) och ingen data på disk
   (`tmpfs`), så även ett fullständigt containerintrång ger ingen varaktig datakälla att
   stjäla.

## Caddy header review

(Refererad från `Caddyfile`s egen kommentar.) Headrarna som faktiskt sätts, och varför:

| Header | Varför den är säker att sätta blint för DEN HÄR appen |
|---|---|
| `X-Content-Type-Options: nosniff` | Appen förlitar sig aldrig på webbläsarens MIME-sniffing för att avgöra hur innehåll ska tolkas — Next.js/FastAPI sätter alltid korrekta `Content-Type`-headers själva. Ingen kod att bryta. |
| `X-Frame-Options: DENY` | En grundare-only-app har inget legitimt behov av att någonsin ramas in (`<iframe>`) av en annan sida. Ingen inbäddningsfunktion existerar att bryta. |
| `Referrer-Policy: strict-origin-when-cross-origin` | Appen skickar inga känsliga värden i URL:er (sessionen är en HttpOnly-kaka, inte en query-parameter) — det finns inget att läcka via `Referer`-headern som denna policy inte redan döljer vid korsning till en annan origin. |
| `Strict-Transport-Security` | Bara aktiv när Caddy faktiskt terminerar TLS för en riktig domän (inert vid `DOMAIN=:80` i CI, se Caddyfile-kommentaren) — standard, lågrisk härdning för HTTPS-only-drift. |
| `-Server` (tas bort) | Läcker ingen information värd att dölja i sig, men minskar brus för automatiserad fingeravtrycksskanning utan kostnad. |

### Content-Security-Policy — medvetet INTE satt än

Detta är den öppna uppföljningspunkten `docs/VPS_DOCKER_HARDENING.md` pekar hit till. En CSP
måste byggas från en verklig inventering av vilka käll-/stil-/anslutningskällor appen
faktiskt använder (Next.js egna genererade chunk-URL:er, eventuella inline-stilar från
komponentbibliotek, `connect-src` för alla externa anrop appen gör från klienten). En gissad
CSP riskerar att blint bryta UI:t utan att faktiskt stänga något verkligt hål — appens egna
`X-Frame-Options`/samma-ursprung-arkitektur (se `docs/VPS_ARCHITECTURE.md`) täcker redan den
vanligaste CSP-motiveringen (clickjacking). **Att göra innan CSP läggs till:** en
`Content-Security-Policy-Report-Only`-körning mot en riktig session för att fånga alla
faktiska källor utan att riskera att bryta något, sedan en riktig `Content-Security-Policy`
byggd från den datan — spårat som framtida arbete, inte gissat här.

## Vad som medvetet INTE är byggt (och varför det är rätt beslut just nu)

- **Web Application Firewall (WAF) / DDoS-skydd på applikationsnivå**: den här appen har en
  ENDA avsedd användare och inget offentligt registreringsflöde i produktion — ett
  fullskaligt WAF löser ett problem den här hotmodellen inte har. Om domänen någonsin blir
  mål för riktad DDoS är nästa steg ett CDN/proxy-lager framför Caddy (t.ex. Cloudflare), inte
  något som byggs in i `docker-compose.vps.yml` självt.
- **Intrångsdetektionssystem (IDS/fail2ban)**: `ufw` + nyckelbaserad SSH + inga publicerade
  applikationsportar utöver 80/443 minskar attackytan tillräckligt för den här skalan; att
  lägga till `fail2ban` är ett rimligt framtida steg men inte kritiskt kvalificerande för en
  enanvändarapp med redan minimal exponerad yta.
- **Automatisk sårbarhetsskanning av den körande servern** (t.ex. schemalagd `trivy`/`grype`
  mot de körande imagesna på VPS:en): täcks delvis av `docs/VPS_SUPPLY_CHAIN.md`s CI-baserade
  skanning av imagesna FÖRE deploy — en ytterligare på-server-skanning är övervägd men inte
  kritisk givet att `deploy.sh` redan vägrar deploya annat än digest-pinnade, CI-byggda
  images.

## Ändringshistorik för denna hotmodell

Den här filen är avsedd att uppdateras när topologin ändras väsentligt (nya tjänster, nya
externa beroenden, nya publika endpoints) — inte en engångsartefakt. Se
`docs/vps/START_HERE.md` för var den passar in i den bredare dokumentationen.
