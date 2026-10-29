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
- [x] Keep the customer storefront focused; add an accessible password visibility control, explicit unsupported recovery message, order history privacy, idempotent multiple-order handling, collision-resistant order IDs, encrypted per-order delivery codes, five-attempt verification limits, and OTP-free operator payloads/logs.
- [x] Extend integration coverage for password visibility wiring, customer order privacy, multiple orders, distinct order IDs/OTPs, invalid/replayed delivery codes, and existing role isolation.
- [x] Fix Render startup permissions: make /app writable for the non-root container user and store only disposable demo SQLite data in writable /tmp.
- [ ] Verify the updated DEMO ONLY deployment after changes are pushed; the Render free instance has ephemeral storage and must remain read-only for real-order purposes.
- [ ] Before accepting real orders, replace ephemeral Render SQLite with durable storage: preferably managed PostgreSQL (or a paid Render service with an attached persistent disk and SQLite configured on that disk). Configure a stable secret `AMNOX_OTP_ENCRYPTION_KEY`, verified accounts, distributed rate limiting, backups/restore drills, and complete security review. Do not upload the local database to the free demo.
- [ ] External security review, password recovery/email verification, distributed rate limiting, automated backup/restore drills, managed PostgreSQL migration, and a hardened production HTTP server stack.
- [ ] Production legal, security, food-safety, tax, privacy and consumer-protection review.
