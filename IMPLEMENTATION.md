# AMNOX implementation checklist

## Existing workspace review
- [x] Continue in the existing workspace; no separate repo created.
- [x] Preserve legacy SQLite tables and records; add grocery schema additively.
- [x] Remove the browser role selector and disable the legacy restaurant API.

## Grocery vertical slice
- [x] AMNOX customer catalog with grocery categories, shops, search, product units and stock display.
- [x] One-shop basket and Cash on Delivery checkout.
- [x] Server-side price/stock checks, idempotency key, stock reservation transaction and restock when a shop rejects an order.
- [x] Product snapshots in order lines and auditable inventory adjustments.
- [x] Customer account registration and database-backed sessions.
- [x] Separate shop, admin and rider route namespaces and layouts.
- [x] Server role checks, shop ownership checks and rider assignment checks.
- [x] Shop applications, administrator review, shop suspension/reactivation, product creation and inventory adjustments.
- [x] Shop order accept/reject/packing/ready workflow.
- [x] Admin rider assignment with one active order per rider.
- [x] Rider pickup, en-route status and expiring OTP delivery verification with attempt limit.
- [x] Operator-only CLI account provisioning; browser role selection grants no privileges.
- [x] README with local launch, account provisioning, data model and limitations.

## Verification
- [x] `python -m py_compile server.py test_amnoX.py`.
- [x] `node --check web/amx.js`.
- [x] `python -m unittest -q test_amnoX` — 11 integration cases passed, including demo write blocking and portal authorization.
- [x] Existing SQLite file migrated additively; legacy restaurant rows preserved.
- [x] Isolated DEMO ONLY preview verified at `http://127.0.0.1:8001/` on a fresh demo database; all eight requested categories return synthetic products, portal login pages render, and unauthenticated dashboards redirect to login.
- [x] Demo registration, shop-application, and order POSTs return 403 and leave users/applications/orders tables empty; existing `nila.sqlite3` and `verify.sqlite3` were not modified.
- [x] Legacy food-order endpoint returns 410; unauthenticated admin API returns 401.

## Remaining work
- [ ] Full customer address CRUD, customer order detail, favourites and support tickets.
- [ ] Product image upload, product editing/archive, stock-change threshold configuration, substitutions/refunds.
- [ ] Shop profile/hours/availability and complete sales/settlement ledger.
- [ ] Admin CRUD/settings for categories, coupons, fees, commissions, payments, refunds, reports and support.
- [ ] Rider availability/profile, zones, navigation provider, history/earnings and Android app.
- [x] Add production fail-closed environment settings, secure cookies/security headers, POST origin checks, direct portal route authorization, basic login throttling, and versioned additive SQLite migrations.
- [x] Add a provider-neutral container image and persistent-volume entrypoint; exclude local data and secrets from the image build context.
- [x] Add a Render Free DEMO ONLY Blueprint using a new ephemeral SQLite path, synthetic catalog data, `/api/health`, and server-side rejection of sign-ups, shop applications, and orders.
- [ ] Connect an authorized Render account to a Git repository containing this workspace, create the Blueprint service, deploy, and verify public routes. No Render account/source connection is available in this workspace.
- [ ] Before accepting real orders, replace the DEMO ONLY service with paid durable storage or managed PostgreSQL; configure production secrets, verified accounts, rate limiting, backups, and complete security review. Do not upload the local database to the free demo.
- [ ] External security review, password recovery/email verification, distributed rate limiting, automated backup/restore drills, managed PostgreSQL migration, and a hardened production HTTP server stack.
- [ ] Production legal, security, food-safety, tax, privacy and consumer-protection review.
