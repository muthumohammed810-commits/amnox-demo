"""Focused integration coverage for AMNOX marketplace boundaries and order flow."""
import hashlib
import http.cookiejar
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MarketplaceIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.temp.name) / "amnoX.sqlite3"
        server.initialize()
        with server.connect() as db:
            self.shop_ids = [r[0] for r in db.execute("SELECT id FROM shops ORDER BY id")]
            self.product = db.execute("SELECT * FROM am_products WHERE shop_id=? ORDER BY id LIMIT 1", (self.shop_ids[0],)).fetchone()
            self.initial_stock = self.product["stock_milli"]
            salt = b"integration-test-salt"
            for email, role, shop in (("operator@example.test", "admin", None), ("seller@example.test", "shop", self.shop_ids[0]), ("seller2@example.test", "shop", self.shop_ids[1]), ("rider@example.test", "rider", None)):
                digest = hashlib.pbkdf2_hmac("sha256", b"test-password-123", salt, 180000).hex() + "$" + salt.hex()
                db.execute("INSERT INTO am_users(email,name,role,password_hash,shop_id,created_at) VALUES(?,?,?,?,?,?)", (email, email.split('@')[0], role, digest, shop, server.now()))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None, expected=200):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with self.client.open(req, timeout=5) as res:
                self.assertEqual(res.status, expected)
                raw = res.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            self.assertEqual(e.code, expected, raw.decode())
            return json.loads(raw or b"{}")

    def login(self, email, jar=None):
        client = self.client if jar is None else jar
        req = urllib.request.Request(self.base + "/api/auth/login", data=json.dumps({"email": email, "password": "test-password-123"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with client.open(req, timeout=5) as res:
            return json.loads(res.read())

    def place_order(self, shop_id=None, key="stable-order-key"):
        shop_id = shop_id or self.shop_ids[0]
        product = self.product if shop_id == self.shop_ids[0] else None
        if product is None:
            with server.connect() as db: product = db.execute("SELECT * FROM am_products WHERE shop_id=? ORDER BY id LIMIT 1", (shop_id,)).fetchone()
        body = {"shop_id": shop_id, "items": [{"id": product["id"], "quantity_milli": 1000}], "name": "Test Customer", "phone": "9876543210", "address": "12 Sample Road, Kochi", "payment_method": "cod"}
        return self.request("/api/am/orders", "POST", body, {"Idempotency-Key": key}, 201)

    def test_public_catalog_and_spa_routes(self):
        data = self.request("/api/am/catalog")
        self.assertGreaterEqual(len(data["items"]), 15)
        self.assertTrue(all("restaurant_id" not in x for x in data["items"]))
        for category in data["categories"]:
            filtered = self.request("/api/am/catalog?category=" + category["slug"])
            self.assertTrue(filtered["items"], category["slug"] + " should have seeded products")
            self.assertTrue(all(item["category_slug"] == category["slug"] for item in filtered["items"]))
            path = "/products?category=" + category["slug"]
            req = urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(req.status, 200)
            self.assertIn(b"AMNOX", req.read())
        for path in ("/categories", "/products", "/shops", "/account/login", "/account", "/shop/login", "/delivery/login", "/admin/login", "/shop/dashboard", "/admin/dashboard", "/delivery/dashboard"):
            req = urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(req.status, 200)
            self.assertIn(b"AMNOX", req.read())
        html = urllib.request.urlopen(self.base + "/", timeout=5).read()
        for link in (b'href="/categories"', b'href="/products"', b'href="/account/login"', b'href="/account"', b'href="/shop/login"', b'href="/delivery/login"', b'href="/admin/login"'):
            self.assertIn(link, html)
        with server.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM am_schema_migrations").fetchone()[0], 2)

    def test_portal_pages_enforce_roles_on_server(self):
        # Canonical portal roots serve the application, which chooses login vs dashboard by session.
        for path in ("/admin", "/shop", "/delivery"):
            with self.client.open(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"AMNOX", response.read())

        # Legacy entry and dashboard links safely consolidate to the canonical URL.
        for path, canonical in (("/admin/login", "/admin"), ("/admin/dashboard", "/admin"),
                                ("/shop/login", "/shop"), ("/shop/dashboard", "/shop"),
                                ("/delivery/login", "/delivery"), ("/delivery/dashboard", "/delivery")):
            req = urllib.request.Request(self.base + path)
            with self.assertRaises(urllib.error.HTTPError) as response:
                urllib.request.build_opener(NoRedirectHandler()).open(req, timeout=5)
            self.assertEqual(response.exception.code, 302)
            self.assertEqual(response.exception.headers.get("Location"), canonical)
            response.exception.close()

        self.login("operator@example.test")
        with self.client.open(self.base + "/admin", timeout=5) as response:
            self.assertEqual(response.status, 200)
        shop_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.login("seller@example.test", shop_client)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            shop_client.open(self.base + "/admin", timeout=5)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

    def test_canonical_portals_require_their_own_role(self):
        clients = {
            "admin": self.client,
            "shop": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())),
            "rider": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())),
        }
        for email, client in (("operator@example.test", clients["admin"]),
                              ("seller@example.test", clients["shop"]),
                              ("rider@example.test", clients["rider"])):
            self.login(email, client)

        for role, path in (("admin", "/admin"), ("shop", "/shop"), ("rider", "/delivery")):
            with clients[role].open(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)

        for role, forbidden_path in (("admin", "/shop"), ("admin", "/delivery"),
                                     ("shop", "/admin"), ("shop", "/delivery"),
                                     ("rider", "/admin"), ("rider", "/shop")):
            with self.subTest(role=role, path=forbidden_path):
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    clients[role].open(self.base + forbidden_path, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()

    def test_production_does_not_seed_demo_shops_or_products(self):
        old_path, old_seed = server.DB_PATH, server.SEED_DEMO_DATA
        try:
            server.DB_PATH = Path(self.temp.name) / "production.sqlite3"
            server.SEED_DEMO_DATA = False
            server.initialize()
            with server.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM am_categories").fetchone()[0], 9)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM am_products").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0], 0)
        finally:
            server.DB_PATH, server.SEED_DEMO_DATA = old_path, old_seed

    def test_role_header_cannot_grant_admin_access(self):
        result = self.request("/api/am/admin/summary", headers={"X-Nila-Role": "admin"}, expected=401)
        self.assertIn("authorized AMNOX account", result["error"])

    def test_production_rejects_post_without_same_origin(self):
        previous = server.APP_ENV
        server.APP_ENV = "production"
        try:
            result = self.request("/api/auth/login", "POST", {"email": "operator@example.test", "password": "wrong"}, expected=403)
            self.assertIn("origin", result["error"].lower())
            result = self.request("/api/auth/login", "POST", {"email": "operator@example.test", "password": "wrong"}, {"Origin": self.base}, expected=401)
            self.assertIn("incorrect", result["error"].lower())
        finally:
            server.APP_ENV = previous

    def test_production_login_throttle_uses_render_client_ip_header(self):
        previous = server.APP_ENV
        server.APP_ENV = "production"
        try:
            class Request:
                headers = {"CF-Connecting-IP": "203.0.113.8", "X-Forwarded-For": "198.51.100.7"}
                client_address = ("127.0.0.1", 1234)
            expected = hashlib.sha256("203.0.113.8\0user@example.test".encode()).hexdigest()
            self.assertEqual(server.login_bucket(Request(), "user@example.test"), expected)
        finally:
            server.APP_ENV = previous

    def test_demo_mode_blocks_pii_and_order_writes_and_protects_portals(self):
        previous = server.APP_ENV
        server.APP_ENV = "demo"
        try:
            for path in ("/", "/admin", "/shop", "/delivery"):
                with urllib.request.urlopen(self.base + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            for path, payload in (
                ("/api/auth/register", {"name": "Demo Person", "email": "demo@example.test", "password": "demo-password-123"}),
                ("/api/am/shop-applications", {"shop_name": "Test Shop", "owner_name": "Demo Person", "email": "demo@example.test", "phone": "9876543210", "category": "grocery", "area": "Kochi", "address": "Demo Street"}),
                ("/api/am/orders", {"name": "Demo Person", "phone": "9876543210", "address": "Demo Street, Kochi", "shop_id": self.shop_ids[0], "items": [{"id": self.product["id"], "quantity_milli": 1000}]}),
            ):
                result = self.request(path, "POST", payload, {"Origin": self.base, "Idempotency-Key": "demo-blocked"}, 403)
                self.assertIn("DEMO ONLY", result["error"])
            for portal in ("admin", "shop", "delivery"):
                req = urllib.request.Request(self.base + f"/{portal}/dashboard")
                protected = urllib.request.build_opener(NoRedirectHandler())
                try:
                    protected.open(req, timeout=5)
                    self.fail("Unauthenticated portal dashboard unexpectedly returned normally.")
                except urllib.error.HTTPError as response:
                    self.assertEqual(response.code, 302)
                    self.assertEqual(response.headers["Location"], f"/{portal}")
                    response.read()
                    response.close()
        finally:
            server.APP_ENV = previous

    def test_checkout_rejects_oversell_and_invalid_pack_fraction(self):
        with server.connect() as db:
            db.execute("UPDATE am_products SET stock_milli=5000 WHERE id=?", (self.product["id"],))
        body = {"shop_id": self.shop_ids[0], "items": [{"id": self.product["id"], "quantity_milli": 6000}], "name": "Test Customer", "phone": "9876543210", "address": "12 Sample Road, Kochi", "payment_method": "cod"}
        result = self.request("/api/am/orders", "POST", body, {"Idempotency-Key": "oversell-key"}, 409)
        self.assertIn("Not enough stock", result["error"])
        with server.connect() as db:
            pack = db.execute("SELECT id FROM am_products WHERE shop_id=? AND unit='250 g' LIMIT 1", (self.shop_ids[0],)).fetchone()
        body["items"] = [{"id": pack["id"], "quantity_milli": 250}]
        result = self.request("/api/am/orders", "POST", body, {"Idempotency-Key": "pack-fraction-key"}, 400)
        self.assertIn("Quantity step", result["error"])
        with server.connect() as db:
            stock = db.execute("SELECT stock_milli FROM am_products WHERE id=?", (self.product["id"],)).fetchone()[0]
            self.assertEqual(stock, 5000)

    def test_customer_registration_idempotency_and_inventory(self):
        self.request("/api/auth/register", "POST", {"name": "Customer One", "email": "customer@example.test", "password": "strong-password-1"}, expected=200)
        first = self.place_order()
        second = self.request("/api/am/orders", "POST", {"shop_id": self.shop_ids[0], "items": [{"id": self.product["id"], "quantity_milli": 1000}], "name": "Test Customer", "phone": "9876543210", "address": "12 Sample Road, Kochi", "payment_method": "cod"}, {"Idempotency-Key": "stable-order-key"}, 200)
        self.assertEqual(first["public_id"], second["public_id"])
        with server.connect() as db:
            stock = db.execute("SELECT stock_milli FROM am_products WHERE id=?", (self.product["id"],)).fetchone()[0]
            self.assertEqual(stock, self.initial_stock - 1000)
            snapshot = db.execute("SELECT product_name,unit_price FROM am_order_items WHERE order_id=?", (first["id"],)).fetchone()
            self.assertEqual(snapshot["product_name"], self.product["name"])

    def test_shop_isolation_and_rejected_order_restock(self):
        order = self.place_order()
        self.login("seller2@example.test")
        result = self.request(f"/api/am/shop/orders/{order['id']}", "POST", {"action": "accept"}, expected=404)
        self.assertIn("not found", result["error"])
        self.login("seller@example.test")
        self.request(f"/api/am/shop/orders/{order['id']}", "POST", {"action": "reject", "reason": "Shop is closing"})
        with server.connect() as db:
            stock = db.execute("SELECT stock_milli FROM am_products WHERE id=?", (self.product["id"],)).fetchone()[0]
            self.assertEqual(stock, self.initial_stock)

    def test_order_state_rider_assignment_and_otp_delivery(self):
        order = self.place_order(key="delivery-flow")
        self.login("seller@example.test")
        for action in ("accept", "packing", "ready"):
            self.request(f"/api/am/shop/orders/{order['id']}", "POST", {"action": action})
        self.login("operator@example.test")
        rider = self.request("/api/am/admin/users?role=rider")["items"][0]
        self.request(f"/api/am/admin/orders/{order['id']}", "POST", {"rider_user_id": rider["id"]})
        self.login("rider@example.test")
        self.request("/api/am/rider/status", "POST", {"order_id": order["id"], "action": "picked_up"})
        self.request("/api/am/rider/status", "POST", {"order_id": order["id"], "action": "en_route"})
        with server.connect() as db:
            db.execute("UPDATE am_orders SET delivery_otp_hash=?,otp_expires=? WHERE id=?", (hashlib.sha256(b"123456").hexdigest(), int(time.time()) + 120, order["id"]))
        self.request("/api/am/rider/status", "POST", {"order_id": order["id"], "action": "complete", "otp": "123456"})
        with server.connect() as db:
            status = db.execute("SELECT status FROM am_orders WHERE id=?", (order["id"],)).fetchone()[0]
            self.assertEqual(status, "delivered")


if __name__ == "__main__":
    unittest.main()
