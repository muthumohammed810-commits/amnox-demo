# AMNOX — neighbourhood grocery marketplace

AMNOX is a local-first multi-vendor marketplace for vegetables, fruits, permitted meat and seafood, grains, spices, dairy, bakery, household goods and other everyday essentials. The customer market, shop owner portal, super admin portal, and delivery partner portal use separate route namespaces and server-verified roles.

## Run locally

Requires Python 3.11+; there are no third-party Python dependencies.

```powershell
cd C:\Users\DELL\Documents\Codex\2026-10-29\files-pasted-by-the-user-master\work
python -u server.py
```

Open http://127.0.0.1:8000. The app listens on `HOST`/`PORT` environment settings; defaults are `127.0.0.1:8000`. SQLite uses `data/nila.sqlite3` by default so existing local data is preserved. Set `AMNOX_DB_PATH` to use another database file. The first startup adds AMNOX tables and development catalog seed data without dropping legacy tables. Seeded shops, products, inventory and records are development data.

## Render DEMO ONLY deployment

`render.yaml` prepares the existing Dockerized Python app as a free Render web service. Render free services sleep after 15 minutes without traffic and use an ephemeral filesystem: all SQLite changes are lost after restart, redeploy, or spin-down, and wake-up can take about a minute. Free Render web services do not support persistent disks. This is suitable only for a synthetic, read-only catalog preview—not real customer or order data. The demo database uses `/tmp/amnox-demo.sqlite3`, which is writable by the unprivileged container user; it is disposable and must never contain real data. See [Render free-service limitations](https://render.com/docs/free), [web service port binding](https://render.com/docs/web-services), and [Blueprint configuration](https://render.com/docs/blueprint-spec).

To deploy, put this existing project in a Git repository accessible to your Render account, create a Render Blueprint from it, and apply `render.yaml`. The Docker image also defaults to `APP_ENV=demo`, `AMNOX_DB_PATH=/tmp/amnox-demo.sqlite3`, and synthetic demo seeding so a service created outside a Blueprint still fails safely as DEMO ONLY. The service binds `0.0.0.0` on Render's `PORT` (configured as `10000`), runs `python /app/container-entrypoint.py`, and checks `/api/health`. It uses a new disposable database, seeds synthetic catalog entries, and rejects customer registration, shop applications, checkout, and legacy orders server-side. No local database is packaged: `.dockerignore` excludes `data/`, including `data/nila.sqlite3` and `data/verify.sqlite3`.

The public demo has secure cookies, same-origin checks for POST requests, server-side portal authorization, and an in-process login throttle. It does not provision public test credentials; portal login forms are visible, while portal pages and APIs remain inaccessible without an explicitly provisioned role account. The rate limiter is process-local and resets on restart. Never use real customer information, credentials, or production secrets with the DEMO ONLY service.

Before accepting any real orders: use a paid service with durable storage (or migrate SQLite/schema to managed PostgreSQL), provision verified operator accounts with unique strong credentials, configure durable sessions and distributed abuse controls, enable backups and restore checks, review the custom HTTP server and application security, and configure production secrets and HTTPS settings. Do not point the demo database path at the existing local database.

## Customer routes

- `/`, `/products`, `/categories`, `/shops`, `/shops/{shopSlug}`, `/products/{productId}`
- `/cart`, `/checkout`, `/orders`, `/orders/{orderId}`, `/account`, `/account/addresses`, `/account/favourites`, `/support`
- `/shop/register` submits a shop application.

Shopping supports search, categories, stock indication, units, quarter-quantity increments for kg/litre items, price/stock server validation, and a one-shop basket. Checkout is Cash on Delivery only. The server recalculates totals and uses an idempotency key. Stock is reserved in a SQLite `BEGIN IMMEDIATE` transaction and restored if a shop rejects the order. Order items retain product-name, unit and price snapshots.

## Operator accounts and separate portals

Customer accounts can be registered at `/account/register`. Shop, admin and rider accounts cannot self-select a role; the operator provisions them from a local terminal:

```powershell
python server.py create-user --email admin@example.com --name "Platform Admin" --role admin
python server.py create-user --email shop@example.com --name "Shop Owner" --role shop --shop-id 1
python server.py create-user --email rider@example.com --name "Delivery Partner" --role rider
```

The command prompts for a password (minimum 10 characters) and stores a PBKDF2-HMAC-SHA256 digest. Shop accounts must point to an approved shop. Canonical portal URLs are `/admin`, `/shop`, and `/delivery`: each serves its role-specific login when signed out and the matching dashboard after authentication. Historical `/admin/login`, `/admin/dashboard`, `/shop/login`, `/shop/dashboard`, `/delivery/login`, and `/delivery/dashboard` URLs redirect to their canonical portal. Customer shopping remains at `/`; customer account sign-in remains at `/account/login`. A login creates an HttpOnly, SameSite=Strict session cookie; server operations check the authenticated account role and shop ownership. The development role selector from the previous preview has been removed; legacy Nila API routes return HTTP 410.

Shop applications are reviewed at `/admin/shop-applications`; after approval, provision an account for the shop id shown in `/admin/shops`. Admin and rider accounts require operator provisioning. In production, provision access only after identity verification and use HTTPS.

If the verified applicant already has a customer account, link it only after checking the applicant's identity and approved shop:

```powershell
python server.py link-shop-user --email owner@example.com --shop-id 2
```

Approval does not automatically elevate an existing account.

## Development checks

```powershell
python -m py_compile server.py test_amnoX.py
node --check web\amx.js
python -m unittest -v test_amnoX
```

Tests cover catalog routes, SPA route serving, customer registration, header-role escalation rejection, idempotent checkout, stock decrement and restoration, shop order isolation, rider assignment, delivery state transitions and OTP completion.

## Data and security boundaries

- New tables include users, sessions, categories, shops, shop applications, products, stock movements, customer addresses, grocery orders, order-item snapshots, order history, delivery assignments and audit records.
- Legacy restaurant/menu/order/rider tables are kept in the existing database for recoverability; the customer UI and AMNOX APIs use the grocery model. Legacy restaurant data has not been automatically converted into shop/product records.
- Admin actions require a signed-in provisioned administrator. Shop API queries scope records to the account's linked shop. Riders see only assignments linked to their rider account. Customer order list access is scoped to the signed-in customer.
- The app now has production-mode origin checks for POST requests, same-site secure cookies, server-side portal-route authorization, a basic per-IP-and-account login throttle, versioned additive SQLite migrations, and production demo-seed protection. These safeguards do not replace a security review, platform-level or distributed rate limiting, password recovery/email verification, automated backup/restore drills, formal retention controls, PostgreSQL for scale, or a hardened production HTTP server stack.
- Account verification, private shop documents, media upload, address book CRUD, favorites, coupons, refunds, support tickets, commission/settlement ledger, and full category/settings management are not implemented yet.
- Maps, live GPS, push/SMS/email, payment gateway, online payment, Android clients and APK builds are not configured. No live delivery estimate, payment charge, or external notification is claimed. OTP is local order verification with expiry and a five-attempt lockout.
- Legal, food safety, tax, consumer protection, privacy and local operating requirements require qualified review for Kerala/India.

`server.py` contains the API and additive schema setup. `web/amx.js`, `web/amx.css` and `web/index.html` form the responsive client. `test_amnoX.py` is the focused integration suite. Local databases and runtime artifacts are ignored by Git.
