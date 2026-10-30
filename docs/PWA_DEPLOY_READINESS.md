# MainAI PWA — deployment readiness & HTTPS config (PREPARED, NOT APPLIED)

Companion to `docs/PWA_STRATO_DNS.md`. This is the concrete, ready-to-apply configuration for
serving the PWA from a dedicated HTTPS subdomain on the **existing** Caddy VPS. **Nothing here
is applied; no DNS change and no deploy are performed.** The Founder executes these steps
manually once approved.

An HTTPS origin is **required** for the Google Pixel install: Android only treats a site as an
installable PWA from a trusted (valid-cert) HTTPS origin. Plain HTTP over a LAN IP does **not**
qualify and must not be used as proof. macOS can be install-tested from `http://localhost`
(a secure context) without deploying; the Pixel cannot.

## 1. Pick the subdomain
`app.<your-domain>` (used below).

## 2. DNS (STRATO) — see `docs/PWA_STRATO_DNS.md`
A/AAAA record `app` → VPS public IP (or CNAME for Render). Not done here.

## 3. Caddy — add the subdomain so it gets an automatic Let's Encrypt cert
The existing `Caddyfile` serves `{$DOMAIN}` and reverse-proxies `frontend:3000`. Two options:

- **Simplest:** set the env so `DOMAIN` *is* the new host, and point everything there:
  `DOMAIN=app.<your-domain>` (in `/etc/lifeai/lifeai.env` on the VPS).
- **Serve both apex and app:** make the site address a list. The block body is unchanged
  (same `reverse_proxy frontend:3000`); only the address line gains the host:

  ```caddyfile
  {$DOMAIN}, app.{$BASE_DOMAIN} {
      # ... existing body unchanged (reverse_proxy frontend:3000, headers, etc.)
  }
  ```

Ports 80 and 443 must be reachable from the internet for the ACME challenge. No app code change.

## 4. Origin / app env (same backend, same hosting)
In `/etc/lifeai/lifeai.env`:

- `PUBLIC_APP_URL=https://app.<your-domain>`
- `FRONTEND_ORIGINS` — include `https://app.<your-domain>` (only matters if any client calls
  the backend cross-origin; the proxy model is same-origin).
- Cookies: `COOKIE_SECURE=true` (already correct for HTTPS). In the same-origin proxy model
  `COOKIE_SAMESITE=lax` is sufficient; leave as-is unless a cross-origin client is introduced.

No new secrets. No provider keys. No backend code changes.

## 5. Apply on the VPS (Founder's manual step — the only thing that changes what's served)
```bash
# on the VPS, after DNS + env are set:
docker compose -f docker-compose.vps.yml pull
docker compose -f docker-compose.vps.yml up -d
```
The frontend image must be built from this PWA branch (it adds manifest/sw/icons). Build+push
via the existing `.github/workflows/build-images.yml` (push to the image registry is non-
deploying), then the manual `up -d` above picks it up.

## Deployment-readiness checklist
- [ ] PWA branch `claude/mainai-pwa-installable` reviewed by an independent examiner
      (see `docs/CROSSPLATFORM_SECURITY_REVIEW_SCOPE.md`) — **author does not self-certify.**
- [ ] `next build` green on the branch (verified locally 2026-10-29).
- [ ] Frontend image built from this branch and pushed to the registry (no deploy).
- [ ] Subdomain chosen; STRATO A/AAAA (or CNAME) created.
- [ ] `DOMAIN` / Caddy address updated to include the subdomain; 80/443 reachable.
- [ ] `PUBLIC_APP_URL` / `FRONTEND_ORIGINS` updated.
- [ ] `COOKIE_SECURE=true` confirmed.
- [ ] Founder approval to run `docker compose … up -d` (explicit; not automated).
- [ ] Post-deploy: `curl -I https://app.<domain>/manifest.webmanifest` → 200;
      SW registers in Chrome DevTools → Application → Service Workers (no errors).
- [ ] macOS Chrome: install + standalone launch verified.
- [ ] Pixel Chrome (HTTPS): install + standalone launch verified.
- [ ] Lighthouse → Installable: pass.

## Explicitly NOT done here
No DNS record created, no `DOMAIN` changed, no image pushed, no `docker compose up`, no
provider calls. Deploy/DNS happen only with explicit Founder approval.
