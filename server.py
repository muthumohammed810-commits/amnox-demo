"""AMNOX local-first grocery marketplace API; legacy Nila data is retained."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import hashlib
import hmac
import getpass
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
APP_ENV = os.environ.get("APP_ENV", "development")
if APP_ENV not in {"development", "demo", "staging", "production"}:
    raise RuntimeError("APP_ENV must be development, demo, staging, or production.")
_configured_db_path = os.environ.get("AMNOX_DB_PATH")
if APP_ENV in {"demo", "production"} and not _configured_db_path:
    raise RuntimeError("Set AMNOX_DB_PATH to an explicit database path for hosted environments.")
DB_PATH = Path(_configured_db_path or ROOT / "data" / "nila.sqlite3")
if APP_ENV in {"demo", "production"} and not DB_PATH.is_absolute():
    raise RuntimeError("AMNOX_DB_PATH must be absolute in hosted environments.")
def secure_env():
    return APP_ENV in {"demo", "staging", "production"}
SEED_DEMO_DATA = os.environ.get("AMNOX_SEED_DEMO_DATA", "true" if APP_ENV in {"development", "demo"} else "false").lower() in {"1", "true", "yes"}
if APP_ENV == "production" and SEED_DEMO_DATA:
    raise RuntimeError("Demo data seeding is disabled in production.")
if APP_ENV == "demo" and not SEED_DEMO_DATA:
    raise RuntimeError("The public demo requires synthetic demo catalog seeding.")
if APP_ENV in {"staging", "production"} and os.environ.get("AMNOX_SEED_DEMO_DATA", "false").lower() in {"1", "true", "yes"}:
    raise RuntimeError("Demo catalog seeding is allowed only in development or demo environments.")
LOCK = threading.RLock()
LOGIN_LOCK = threading.Lock()
LOGIN_FAILURES = defaultdict(deque)
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 8


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def initialize():
    # Preserve a one-time SQLite backup before the first tracked schema migration.
    if DB_PATH.is_file() and DB_PATH.stat().st_size:
        with sqlite3.connect(DB_PATH) as source:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                backup_path = DB_PATH.with_name(DB_PATH.name + ".pre-migrations.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        source.backup(backup)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS restaurants (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, cuisine TEXT NOT NULL,
          description TEXT NOT NULL, area TEXT NOT NULL, price_level INTEGER NOT NULL,
          vegetarian INTEGER NOT NULL DEFAULT 0, is_open INTEGER NOT NULL DEFAULT 1,
          delivery_fee INTEGER NOT NULL DEFAULT 2500, min_order INTEGER NOT NULL DEFAULT 0,
          image TEXT NOT NULL DEFAULT '', prep_minutes INTEGER NOT NULL DEFAULT 25
        );
        CREATE TABLE IF NOT EXISTS menu_items (
          id INTEGER PRIMARY KEY, restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
          name TEXT NOT NULL, description TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0),
          category TEXT NOT NULL, vegetarian INTEGER NOT NULL DEFAULT 0,
          available INTEGER NOT NULL DEFAULT 1, image TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS riders (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
          zone TEXT NOT NULL, vehicle TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'offline',
          verified INTEGER NOT NULL DEFAULT 0, active_order INTEGER REFERENCES orders(id)
        );
        CREATE TABLE IF NOT EXISTS customers (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT UNIQUE NOT NULL,
          customer_id INTEGER REFERENCES customers(id), customer_name TEXT NOT NULL,
          customer_phone TEXT NOT NULL, address TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
          restaurant_id INTEGER NOT NULL REFERENCES restaurants(id), status TEXT NOT NULL,
          payment_method TEXT NOT NULL, payment_status TEXT NOT NULL, subtotal INTEGER NOT NULL,
          delivery_fee INTEGER NOT NULL, total INTEGER NOT NULL, delivery_otp_hash TEXT NOT NULL,
          otp_expires INTEGER NOT NULL, rider_id INTEGER REFERENCES riders(id),
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_items (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          menu_item_id INTEGER NOT NULL REFERENCES menu_items(id), item_name TEXT NOT NULL,
          unit_price INTEGER NOT NULL, quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 20),
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY, actor_role TEXT NOT NULL, actor_id TEXT NOT NULL,
          action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
          details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_restaurant ON orders(restaurant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_items_restaurant ON menu_items(restaurant_id, available);
        CREATE TABLE IF NOT EXISTS am_users (
          id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('customer','shop','admin','rider')),
          password_hash TEXT NOT NULL, shop_id INTEGER, rider_id INTEGER,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES am_users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_categories (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
          icon TEXT NOT NULL DEFAULT '✳', active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS shops (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
          description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, area TEXT NOT NULL,
          address TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
          delivery_fee INTEGER NOT NULL DEFAULT 2500, minimum_order INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'approved', is_open INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shop_applications (
          id INTEGER PRIMARY KEY, shop_name TEXT NOT NULL, owner_name TEXT NOT NULL,
          email TEXT NOT NULL, phone TEXT NOT NULL, category TEXT NOT NULL, area TEXT NOT NULL,
          address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', review_note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS am_products (
          id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id),
          category_id INTEGER NOT NULL REFERENCES am_categories(id), name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL,
          price INTEGER NOT NULL CHECK(price>=0), sale_price INTEGER CHECK(sale_price IS NULL OR sale_price>=0),
          stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(stock_milli>=0), low_stock_milli INTEGER NOT NULL DEFAULT 5000,
          status TEXT NOT NULL DEFAULT 'active', image TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_stock_moves (
          id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES am_products(id),
          user_id INTEGER REFERENCES am_users(id), delta_milli INTEGER NOT NULL,
          reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_addresses (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES am_users(id),
          label TEXT NOT NULL, address TEXT NOT NULL, area TEXT NOT NULL, phone TEXT NOT NULL,
          is_default INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS am_orders (
          id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, user_id INTEGER REFERENCES am_users(id),
          shop_id INTEGER NOT NULL REFERENCES shops(id), customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL,
          delivery_address TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
          payment_method TEXT NOT NULL, payment_status TEXT NOT NULL, subtotal INTEGER NOT NULL,
          delivery_fee INTEGER NOT NULL, total INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
          delivery_otp_hash TEXT NOT NULL, otp_expires INTEGER NOT NULL, rider_user_id INTEGER,
          otp_attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_order_items (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES am_orders(id) ON DELETE CASCADE,
          product_id INTEGER NOT NULL REFERENCES am_products(id), product_name TEXT NOT NULL,
          unit TEXT NOT NULL, unit_price INTEGER NOT NULL, quantity_milli INTEGER NOT NULL,
          line_total INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_order_history (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES am_orders(id),
          status TEXT NOT NULL, actor_user_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS am_deliveries (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE REFERENCES am_orders(id),
          rider_user_id INTEGER REFERENCES am_users(id), status TEXT NOT NULL,
          assigned_at TEXT, picked_at TEXT, delivered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS am_audit (
          id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_am_products_shop ON am_products(shop_id,status);
        CREATE INDEX IF NOT EXISTS idx_am_products_category ON am_products(category_id,status);
        CREATE INDEX IF NOT EXISTS idx_am_orders_shop ON am_orders(shop_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_am_orders_user ON am_orders(user_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_am_orders_status ON am_orders(status,created_at);
        CREATE TABLE IF NOT EXISTS am_schema_migrations (
          version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        """)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # Version 1 is the additive baseline: CREATE TABLE/INDEX IF NOT EXISTS above.
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT OR IGNORE INTO am_schema_migrations(version,applied_at) VALUES(1,?)", (now(),))
            version = 1
        if version < 2:
            order_columns={r['name'] for r in db.execute('PRAGMA table_info(am_orders)')}
            if 'otp_attempts' not in order_columns:
                db.execute('ALTER TABLE am_orders ADD COLUMN otp_attempts INTEGER NOT NULL DEFAULT 0')
            db.execute("PRAGMA user_version=2")
            db.execute("INSERT OR IGNORE INTO am_schema_migrations(version,applied_at) VALUES(2,?)", (now(),))
        if SEED_DEMO_DATA and db.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 0:
            restaurants = [
                ("Pepper & Palm", "Kerala", "Coastal Kerala cooking, rooted in Malabar spice and seasonal produce.", "Kozhikode", 2, 0, 1, 2500, 0, "", 24),
                ("Curry Leaf Kitchen", "South Indian", "Comforting rice meals, dosas and fresh chutneys.", "Kochi", 1, 1, 1, 2000, 0, "", 20),
                ("The Fig & Flour", "Bakery", "Small-batch bakes, sandwiches and coffee for slow afternoons.", "Kochi", 2, 1, 1, 3000, 0, "", 18),
                ("Wok & Wharf", "Asian", "Bright, wok-fired bowls with vegetables and house sauces.", "Thiruvananthapuram", 2, 0, 1, 2500, 0, "", 28),
            ]
            db.executemany("INSERT INTO restaurants(name,cuisine,description,area,price_level,vegetarian,is_open,delivery_fee,min_order,image,prep_minutes) VALUES(?,?,?,?,?,?,?,?,?,?,?)", restaurants)
            foods = [
                (1,"Malabar chicken biryani","Dum-cooked rice, chicken, fried onion and mint.",24500,"House favourites",0),
                (1,"Kallummakkaya roast","Mussels with roasted coconut and curry leaf.",21000,"Coastal plates",0),
                (1,"Paneer ghee roast","Peppery ghee roast with soft paneer.",18500,"Coastal plates",1),
                (1,"Pathiri & chicken stew","Rice flatbread with gentle coconut stew.",19500,"Comfort plates",0),
                (2,"Masala dosa","Golden dosa, potato masala and coconut chutney.",12500,"Breakfast all day",1),
                (2,"Kerala veg meals","Rice, seasonal sides, sambar and pickle.",16000,"Meals",1),
                (2,"Ghee podi idli","Steamed idli tossed in podi and ghee.",9500,"Breakfast all day",1),
                (3,"Banana bread slice","Toasted banana loaf with a little sea salt.",8500,"Bakes",1),
                (3,"Mushroom melt","Sourdough, mushrooms and aged cheddar.",18500,"Sandwiches",1),
                (3,"Lemon olive oil cake","Soft citrus cake, baked in small batches.",12500,"Bakes",1),
                (4,"Chilli basil tofu bowl","Wok-fired tofu, basil and jasmine rice.",19000,"Rice bowls",1),
                (4,"Sesame chicken noodles","Noodles, greens and toasted sesame.",21500,"Noodles",0),
            ]
            db.executemany("INSERT INTO menu_items(restaurant_id,name,description,price,category,vegetarian) VALUES(?,?,?,?,?,?)", foods)
        if SEED_DEMO_DATA and db.execute("SELECT COUNT(*) FROM riders").fetchone()[0] == 0:
            db.executemany("INSERT INTO riders(name,phone,zone,vehicle,status,verified) VALUES(?,?,?,?,?,?)", [
                ("Akhil Menon", "•••• 2041", "Kozhikode central", "Scooter", "available", 1),
                ("Nisha K", "•••• 7732", "Kochi north", "Motorcycle", "available", 1),
                ("Rahul P", "•••• 0198", "Kochi south", "Bicycle", "offline", 1),
            ])
        if db.execute("SELECT COUNT(*) FROM am_categories").fetchone()[0] == 0:
            categories = [
                ('Vegetables','vegetables','🥬',1),('Fruits','fruits','🍊',2),('Meat & seafood','meat-seafood','🐟',3),
                ('Rice, grains & pulses','grains-pulses','🌾',4),('Grocery & essentials','grocery-essentials','🧺',5),
                ('Spices & masalas','spices-masalas','🌶️',6),('Dairy & packaged food','dairy-packaged','🥛',7),
                ('Bakery','bakery','🥖',8),('Household','household','🧹',9)]
            db.executemany('INSERT INTO am_categories(name,slug,icon,sort_order) VALUES(?,?,?,?)',categories)
        if SEED_DEMO_DATA and db.execute("SELECT COUNT(*) FROM shops").fetchone()[0] == 0:
            shops = [
                ('Green Basket Market','green-basket-market','Fresh picks from nearby farms, sorted every morning.','Vegetables & fruits','Kochi','Market Road, Kochi','',2500,0,'approved',1,now()),
                ('Malabar Harvest','malabar-harvest','Seasonal fruit and pantry staples from Kerala growers.','Fruits & pantry','Kozhikode','Mavoor Road, Kozhikode','',3000,0,'approved',1,now()),
                ('Harbour Fresh','harbour-fresh','Fresh catch and chilled seafood from the local harbour.','Fish & seafood','Kochi','Fort Kochi, Kochi','',3500,0,'approved',1,now()),
                ('Everyday Provisions','everyday-provisions','Grains, spices and household essentials for the week.','Groceries','Thiruvananthapuram','Pattom, Thiruvananthapuram','',2500,0,'approved',1,now())]
            db.executemany('INSERT INTO shops(name,slug,description,category,area,address,phone,delivery_fee,minimum_order,status,is_open,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',shops)
            cat={r['slug']:r['id'] for r in db.execute('SELECT id,slug FROM am_categories')}
            products=[
                (1,'Local tomato','Firm, ripe tomatoes from nearby growers.','kg',4200,None,125000,'vegetables'),(1,'Green chilli','Fresh green chilli, sorted by hand.','250 g',1800,None,48000,'vegetables'),(1,'Nadan cucumber','Cool, crisp local cucumber.','kg',3600,None,72000,'vegetables'),(1,'Red onion','Everyday red onion.','kg',4400,None,92000,'vegetables'),
                (2,'Robusta banana','Naturally ripened Kerala bananas.','kg',5800,5200,81000,'fruits'),(2,'Alphonso mango','Seasonal mangoes, available while stock lasts.','kg',16500,None,28000,'fruits'),(2,'Sweet lime','Juicy mosambi, sold by weight.','kg',9200,None,35000,'fruits'),
                (3,'Seer fish steaks','Cleaned and chilled; price per 500 g pack.','500 g',34500,None,18000,'meat-seafood'),(3,'Fresh sardines','Local catch, cleaned on request.','kg',22000,None,26000,'meat-seafood'),
                (4,'Matta rice','Kerala red rice, unpolished.','kg',7200,None,175000,'grains-pulses'),(4,'Toor dal','Cleaned and packed pulses.','kg',13800,None,65000,'grains-pulses'),(4,'Whole wheat flour','Stone-ground everyday atta.','kg',6200,None,85000,'grains-pulses'),
                (4,'Coconut oil','Cold-pressed coconut oil.','1 litre',24800,None,22000,'grocery-essentials'),(4,'Jaggery blocks','Traditional palm jaggery.','500 g',8400,None,31000,'grocery-essentials'),
                (4,'Kashmiri chilli powder','Bright colour and mellow heat.','100 g',6800,None,42000,'spices-masalas'),(4,'Garam masala','Small-batch house spice blend.','100 g',7600,None,27000,'spices-masalas'),
                (4,'Fresh curd','Set curd, chilled.','500 g',4600,None,16000,'dairy-packaged'),(4,'Tea powder','Strong Assam blend.','250 g',13200,None,48000,'dairy-packaged'),
                (4,'Milk bread','Freshly baked daily.','pack',4200,None,14000,'bakery'),(4,'Dish wash liquid','Refill-friendly household cleaner.','500 ml',9500,None,19000,'household')]
            db.executemany('INSERT INTO am_products(shop_id,name,description,unit,price,sale_price,stock_milli,category_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',[(shop,n,d,u,p,s,stock,cat[c],now(),now()) for shop,n,d,u,p,s,stock,c in products])


def cents(value):
    return f"₹{value / 100:,.2f}"


def rowdict(row):
    return dict(row) if row is not None else None


def audit(db, role, actor, action, entity, entity_id, details=None):
    db.execute("INSERT INTO audit_log(actor_role,actor_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
               (role, actor, action, entity, str(entity_id), json.dumps(details or {}), now()))


def login_bucket(handler, email):
    # On Render, CF-Connecting-IP is set by its Cloudflare edge. Avoid trusting
    # X-Forwarded-For because it may contain caller-supplied values.
    source = handler.headers.get("CF-Connecting-IP", "").strip() if secure_env() else ""
    if not source:
        source = handler.client_address[0]
    return hashlib.sha256((source + "\0" + email).encode()).hexdigest()


def login_is_limited(bucket, timestamp=None):
    timestamp = time.monotonic() if timestamp is None else timestamp
    with LOGIN_LOCK:
        if len(LOGIN_FAILURES) > 10000:
            for key in tuple(LOGIN_FAILURES):
                attempts = LOGIN_FAILURES[key]
                while attempts and timestamp - attempts[0] >= LOGIN_WINDOW_SECONDS:
                    attempts.popleft()
                if not attempts:
                    LOGIN_FAILURES.pop(key, None)
                if len(LOGIN_FAILURES) <= 8000:
                    break
        attempts = LOGIN_FAILURES[bucket]
        while attempts and timestamp - attempts[0] >= LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= LOGIN_MAX_FAILURES


def login_failed(bucket):
    timestamp = time.monotonic()
    with LOGIN_LOCK:
        attempts = LOGIN_FAILURES[bucket]
        while attempts and timestamp - attempts[0] >= LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        attempts.append(timestamp)


def login_succeeded(bucket):
    with LOGIN_LOCK:
        LOGIN_FAILURES.pop(bucket, None)


def order_payload(db, order):
    order = dict(order)
    order["items"] = [dict(r) for r in db.execute("SELECT item_name,unit_price,quantity,note FROM order_items WHERE order_id=?", (order["id"],))]
    order["rider"] = rowdict(db.execute("SELECT id,name,phone,zone,vehicle,status FROM riders WHERE id=?", (order["rider_id"],)).fetchone()) if order["rider_id"] else None
    order["subtotal_display"] = cents(order["subtotal"])
    order["delivery_fee_display"] = cents(order["delivery_fee"])
    order["total_display"] = cents(order["total"])
    order["restaurant"] = rowdict(db.execute("SELECT id,name,area,cuisine,prep_minutes FROM restaurants WHERE id=?", (order["restaurant_id"],)).fetchone())
    return order


class APIError(Exception):
    def __init__(self, message, code=400):
        self.message, self.code = message, code


class Handler(BaseHTTPRequestHandler):
    server_version = "AMNOXLocal/0.2"

    def log_message(self, fmt, *args):
        if APP_ENV in {"development", "demo"}:
            super().log_message(fmt, *args)

    def role(self):
        # Legacy Nila endpoints never trust browser-supplied role headers.
        return "customer"

    def actor(self):
        return self.headers.get("X-Nila-Actor", "local")[:80]

    def require(self, *roles):
        if self.role() not in roles:
            raise APIError("This action is not available for your role.", 403)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if secure_env(): self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 64_000: raise APIError("Request body is too large.", 413)
            return json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise APIError("Request body must be valid JSON.")

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/am/") or path.startswith("/api/auth/"):
                return self.am_get(path)
            if path.startswith(("/api/restaurants", "/api/restaurant/", "/api/orders", "/api/dispatch", "/api/rider", "/api/admin")):
                raise APIError("The retired restaurant-ordering API is no longer available. Use AMNOX grocery endpoints.",410)
            with connect() as db:
                if path == "/api/restaurants":
                    q = parse_qs(urlparse(self.path).query)
                    term = (q.get("q", [""])[0] or "")[:80].strip()
                    cuisine = (q.get("cuisine", [""])[0] or "")[:40]
                    veg = q.get("veg", [""])[0] == "1"
                    sql = "SELECT DISTINCT r.* FROM restaurants r LEFT JOIN menu_items m ON m.restaurant_id=r.id AND m.available=1 WHERE (r.name LIKE ? OR r.cuisine LIKE ? OR m.name LIKE ?)"
                    args = [f"%{term}%"] * 3
                    if cuisine: sql += " AND r.cuisine=?"; args.append(cuisine)
                    if veg: sql += " AND (r.vegetarian=1 OR m.vegetarian=1)"
                    sql += " ORDER BY r.name LIMIT 40"
                    items = [dict(r) for r in db.execute(sql, args)]
                    for item in items: item["delivery_fee_display"] = cents(item["delivery_fee"])
                    return self.send_json({"items": items, "cuisines": [r[0] for r in db.execute("SELECT DISTINCT cuisine FROM restaurants ORDER BY cuisine")]})
                if path.startswith("/api/restaurants/"):
                    parts = path.strip("/").split("/")
                    rid = int(parts[2])
                    restaurant = rowdict(db.execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone())
                    if not restaurant: raise APIError("Restaurant not found.", 404)
                    restaurant["menu"] = [dict(r) for r in db.execute("SELECT * FROM menu_items WHERE restaurant_id=? AND available=1 ORDER BY category,name", (rid,))]
                    for item in restaurant["menu"]: item["price_display"] = cents(item["price"])
                    return self.send_json(restaurant)
                if path == "/api/orders":
                    self.require("customer", "admin")
                    orders = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall()
                    return self.send_json({"items": [order_payload(db, r) for r in orders]})
                if path.startswith("/api/orders/"):
                    self.require("customer", "restaurant", "dispatch", "rider", "admin")
                    oid = path.rsplit("/",1)[1]
                    order = db.execute("SELECT * FROM orders WHERE public_id=?", (oid,)).fetchone()
                    if not order: raise APIError("Order not found.", 404)
                    result = order_payload(db, order)
                    if self.role() not in ("rider", "admin", "dispatch"):
                        result.pop("delivery_otp_hash", None); result.pop("otp_expires", None); result.pop("customer_phone", None)
                    return self.send_json(result)
                if path == "/api/restaurant/orders":
                    self.require("restaurant", "admin")
                    return self.send_json({"items": [order_payload(db,r) for r in db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100")]})
                if path == "/api/dispatch":
                    self.require("dispatch", "admin")
                    orders = [order_payload(db,r) for r in db.execute("SELECT * FROM orders WHERE status IN ('placed','accepted','preparing','ready','assigned','picked_up','en_route') ORDER BY created_at")]
                    riders = [dict(r) for r in db.execute("SELECT id,name,phone,zone,vehicle,status,verified,active_order FROM riders ORDER BY name")]
                    return self.send_json({"orders": orders, "riders": riders})
                if path == "/api/rider":
                    self.require("rider", "admin")
                    rider_id = int(self.headers.get("X-Nila-Actor", "1"))
                    rider = db.execute("SELECT * FROM riders WHERE id=?", (rider_id,)).fetchone()
                    if not rider: raise APIError("Rider not found.", 404)
                    orders = [order_payload(db,r) for r in db.execute("SELECT * FROM orders WHERE rider_id=? AND status IN ('assigned','picked_up','en_route') ORDER BY id DESC", (rider_id,))]
                    return self.send_json({"rider": dict(rider), "orders": orders})
                if path == "/api/admin/summary":
                    self.require("admin")
                    return self.send_json({
                      "customers": db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
                      "restaurants": db.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0],
                      "riders": db.execute("SELECT COUNT(*) FROM riders WHERE verified=1").fetchone()[0],
                      "orders_today": db.execute("SELECT COUNT(*) FROM orders WHERE created_at>=?", (now()[:10],)).fetchone()[0],
                      "active_orders": db.execute("SELECT COUNT(*) FROM orders WHERE status NOT IN ('delivered','cancelled')").fetchone()[0],
                      "gross_today": cents(db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE created_at>=? AND status='delivered'", (now()[:10],)).fetchone()[0])
                    })
            if path == "/api/health": return self.send_json({"ok": True, "mode": "DEMO ONLY" if APP_ENV == "demo" else APP_ENV})
            if path == "/": self.path = "/index.html"
            return self.serve_static(urlparse(self.path).path)
        except APIError as e: self.send_json({"error": e.message}, e.code)
        except (ValueError, IndexError): self.send_json({"error": "Invalid identifier."}, 400)
        except Exception as e:
            print(f"API error: {type(e).__name__}: {e}")
            self.send_json({"error": "The request could not be completed."}, 500)

    def serve_static(self, urlpath):
        namespace = urlpath.strip("/").split("/", 1)[0]
        if namespace in {"admin", "shop", "delivery"}:
            is_login = urlpath in {"/admin/login", "/shop/login", "/delivery/login"}
            is_public_registration = urlpath == "/shop/register"
            if not is_login and not is_public_registration:
                required_role = {"admin": "admin", "shop": "shop", "delivery": "rider"}[namespace]
                with connect() as db:
                    user = self.am_user(db)
                if user is None:
                    self.send_response(303)
                    self.send_header("Location", f"/{namespace}/login")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if user["role"] != required_role:
                    self.send_error(403, "This portal requires an authorized account.")
                    return
        path = (ROOT / "web" / urlpath.lstrip("/")).resolve()
        base = (ROOT / "web").resolve()
        if base not in path.parents:
            self.send_error(404); return
        if not path.is_file() and '.' not in Path(urlpath).name:
            path = base / "index.html"
        if not path.is_file(): self.send_error(404); return
        data = path.read_bytes()
        mime = {".html":"text/html; charset=utf-8", ".css":"text/css; charset=utf-8", ".js":"text/javascript; charset=utf-8", ".svg":"image/svg+xml"}.get(path.suffix, "application/octet-stream")
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "same-origin")
        if secure_env(): self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if secure_env():
                origin = self.headers.get("Origin", "")
                request_host = self.headers.get("Host", "").lower()
                if not origin or urlparse(origin).netloc.lower() != request_host:
                    raise APIError("Request origin could not be verified.", 403)
            data = self.body()
            if path.startswith("/api/am/") or path.startswith("/api/auth/"):
                return self.am_post(path, data)
            if path.startswith(("/api/restaurants", "/api/restaurant/", "/api/orders", "/api/dispatch", "/api/rider", "/api/admin")):
                raise APIError("The retired restaurant-ordering API is no longer available. Use AMNOX grocery endpoints.",410)
            if path == "/api/orders": return self.create_order(data)
            if path.startswith("/api/restaurant/orders/"): return self.restaurant_action(path.rsplit("/",1)[1], data)
            if path == "/api/dispatch/assign": return self.assign(data)
            if path == "/api/rider/status": return self.rider_status(data)
            if path == "/api/rider/complete": return self.complete_delivery(data)
            if path == "/api/rider/availability": return self.availability(data)
            raise APIError("Route not found.", 404)
        except APIError as e: self.send_json({"error": e.message}, e.code)
        except (ValueError, KeyError, TypeError): self.send_json({"error": "Invalid request data."}, 400)
        except Exception as e:
            print(f"API error: {type(e).__name__}: {e}")
            self.send_json({"error": "The request could not be completed."}, 500)

    def create_order(self, data):
        self.require("customer", "admin")
        name = str(data.get("name", "")).strip()[:80]
        phone = str(data.get("phone", "")).strip()[:20]
        address = str(data.get("address", "")).strip()[:300]
        instructions = str(data.get("instructions", "")).strip()[:300]
        if len(name) < 2 or not phone.replace("+", "").replace(" ", "").isdigit() or len(phone) < 8 or len(address) < 8:
            raise APIError("Enter a name, reachable phone number and complete delivery address.")
        key = self.headers.get("Idempotency-Key", "")[:100]
        if not key: raise APIError("Checkout requires an idempotency key.")
        items = data.get("items")
        if not isinstance(items, list) or not items or len(items) > 30: raise APIError("Your cart is empty or too large.")
        try:
            rid = int(data.get("restaurant_id"))
            parsed = [(int(i["id"]), int(i["quantity"]), str(i.get("note", "")).strip()[:200]) for i in items]
        except (ValueError, KeyError, TypeError): raise APIError("Cart contains invalid items.")
        if any(q < 1 or q > 20 for _, q, _ in parsed): raise APIError("Quantity must be between 1 and 20.")
        if len({iid for iid,_,_ in parsed}) != len(parsed): raise APIError("Combine repeated dishes before checkout.")
        payment = data.get("payment_method", "cod")
        if payment not in ("cod", "development"): raise APIError("Choose cash on delivery or the clearly labelled local development payment.")
        with LOCK, connect() as db:
            previous = db.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if previous: return self.send_json(order_payload(db, previous), 200)
            restaurant = db.execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
            if not restaurant or not restaurant["is_open"]: raise APIError("This restaurant is currently unavailable.", 409)
            subtotal, order_items = 0, []
            for iid, quantity, note in parsed:
                item = db.execute("SELECT * FROM menu_items WHERE id=? AND restaurant_id=? AND available=1", (iid,rid)).fetchone()
                if not item: raise APIError("A cart item is no longer available.", 409)
                subtotal += item["price"] * quantity
                order_items.append((item, quantity, note))
            if subtotal < restaurant["min_order"]: raise APIError("The cart does not meet this restaurant's minimum order.", 409)
            fee = restaurant["delivery_fee"]
            if payment == "development" and APP_ENV != "development": raise APIError("Development payment is disabled.", 403)
            customer = db.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            if customer: customer_id = customer[0]
            else:
                cur = db.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)", (name,phone,now())); customer_id = cur.lastrowid
            otp = f"{secrets.randbelow(1_000_000):06d}"
            public_id = "NL-" + secrets.token_hex(4).upper()
            status, payment_status = "placed", "authorized_cod" if payment == "cod" else "development_simulated"
            cur = db.execute("INSERT INTO orders(public_id,customer_id,customer_name,customer_phone,address,instructions,restaurant_id,status,payment_method,payment_status,subtotal,delivery_fee,total,delivery_otp_hash,otp_expires,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (public_id,customer_id,name,phone,address,instructions,rid,status,payment,payment_status,subtotal,fee,subtotal+fee,secrets.token_hex(32),int(time.time())+3600,now(),now(),key))
            oid = cur.lastrowid
            # OTP plaintext is deliberately only displayed in local dev output; the API accepts a one-time code.
            # A salted digest is stored below.
            import hashlib
            db.execute("UPDATE orders SET delivery_otp_hash=? WHERE id=?", (hashlib.sha256(otp.encode()).hexdigest(), oid))
            for item, qty, note in order_items:
                db.execute("INSERT INTO order_items(order_id,menu_item_id,item_name,unit_price,quantity,note) VALUES(?,?,?,?,?,?)", (oid,item["id"],item["name"],item["price"],qty,note))
            audit(db,self.role(),self.actor(),"order_created","order",public_id,{"payment_status":payment_status})
            order = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if APP_ENV == "development": print(f"[local development only] Delivery verification code for {public_id}: {otp}")
            return self.send_json(order_payload(db,order), 201)

    def restaurant_action(self, public_id, data):
        self.require("restaurant", "admin")
        actions = {"accept": ("placed","accepted"), "reject": ("placed","cancelled"), "preparing": ("accepted","preparing"), "ready": ("preparing","ready")}
        action = data.get("action")
        if action not in actions: raise APIError("Unsupported restaurant action.")
        with connect() as db:
            order = db.execute("SELECT * FROM orders WHERE public_id=?", (public_id,)).fetchone()
            if not order: raise APIError("Order not found.",404)
            old,new = actions[action]
            if order["status"] != old: raise APIError(f"Cannot {action} an order in {order['status']} state.",409)
            if action == "reject" and len(str(data.get("reason", "")).strip()) < 3: raise APIError("Provide a short rejection reason.")
            db.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?",(new,now(),order["id"]))
            audit(db,self.role(),self.actor(),"restaurant_"+action,"order",public_id,{"reason":str(data.get("reason",""))[:200]})
        self.send_json({"ok":True,"status":new})

    def assign(self, data):
        self.require("dispatch", "admin")
        public_id, rider_id = str(data.get("order_id", "")), int(data.get("rider_id", 0))
        with LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM orders WHERE public_id=?", (public_id,)).fetchone()
            rider = db.execute("SELECT * FROM riders WHERE id=?", (rider_id,)).fetchone()
            if not order: raise APIError("Order not found.",404)
            if order["status"] not in ("accepted","preparing","ready"): raise APIError("Only accepted restaurant orders can be assigned.",409)
            if not rider or not rider["verified"] or rider["status"] != "available" or rider["active_order"] is not None: raise APIError("Choose an approved, available rider without an active delivery.",409)
            if order["rider_id"]: raise APIError("This order is already assigned.",409)
            db.execute("UPDATE riders SET status='busy',active_order=? WHERE id=?", (order["id"],rider_id))
            db.execute("UPDATE orders SET rider_id=?,status='assigned',updated_at=? WHERE id=?",(rider_id,now(),order["id"]))
            audit(db,self.role(),self.actor(),"rider_assigned","order",public_id,{"rider_id":rider_id})
        self.send_json({"ok":True,"status":"assigned"})

    def rider_status(self, data):
        self.require("rider", "admin")
        public_id = str(data.get("order_id", ""))
        action = data.get("action")
        mapping = {"picked_up": ("assigned", "picked_up"), "en_route": ("picked_up", "en_route")}
        if action not in mapping: raise APIError("Unsupported rider action.")
        rider_id = int(self.actor()) if self.actor().isdigit() else 1
        with connect() as db:
            order = db.execute("SELECT * FROM orders WHERE public_id=?", (public_id,)).fetchone()
            if not order or order["rider_id"] != rider_id: raise APIError("This delivery is not assigned to this rider.",403)
            old,new = mapping[action]
            if order["status"] != old: raise APIError(f"Cannot change {order['status']} order to {new}.",409)
            db.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?",(new,now(),order["id"]))
            audit(db,self.role(),self.actor(),"rider_"+action,"order",public_id)
        self.send_json({"ok":True,"status":new})

    def complete_delivery(self, data):
        self.require("rider", "admin")
        public_id, otp = str(data.get("order_id", "")), str(data.get("otp", ""))
        rider_id = int(self.actor()) if self.actor().isdigit() else 1
        import hashlib
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM orders WHERE public_id=?", (public_id,)).fetchone()
            if not order or order["rider_id"] != rider_id: raise APIError("This delivery is not assigned to this rider.",403)
            if order["status"] != "en_route": raise APIError("Delivery must be en route before completion.",409)
            if int(time.time()) > order["otp_expires"]: raise APIError("Delivery verification code has expired.",409)
            if len(otp) != 6 or not secrets.compare_digest(hashlib.sha256(otp.encode()).hexdigest(),order["delivery_otp_hash"]): raise APIError("Delivery verification code is incorrect.",403)
            db.execute("UPDATE orders SET status='delivered',updated_at=? WHERE id=?",(now(),order["id"]))
            db.execute("UPDATE riders SET status='available',active_order=NULL WHERE id=?",(rider_id,))
            audit(db,self.role(),self.actor(),"delivery_completed","order",public_id)
        self.send_json({"ok":True,"status":"delivered"})

    def availability(self, data):
        self.require("rider", "admin")
        status = data.get("status")
        if status not in ("available", "offline"): raise APIError("Choose available or offline.")
        rider_id = int(self.actor()) if self.actor().isdigit() else 1
        with connect() as db:
            rider = db.execute("SELECT * FROM riders WHERE id=?", (rider_id,)).fetchone()
            if not rider: raise APIError("Rider not found.",404)
            if rider["active_order"] is not None and status == "offline": raise APIError("Finish the active delivery before going offline.",409)
            db.execute("UPDATE riders SET status=? WHERE id=?",(status,rider_id))
            audit(db,self.role(),self.actor(),"rider_availability","rider",rider_id,{"status":status})
        self.send_json({"ok":True,"status":status})

    def am_user(self, db):
        cookie = next((x.strip().split('=',1)[1] for x in self.headers.get('Cookie','').split(';') if x.strip().startswith('am_session=')), '')
        if not cookie: return None
        return rowdict(db.execute("SELECT u.* FROM am_sessions s JOIN am_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",(hashlib.sha256(cookie.encode()).hexdigest(),int(time.time()))).fetchone())

    def am_require(self, db, *roles):
        user = self.am_user(db)
        if not user or user['role'] not in roles: raise APIError('Sign in with an authorized AMNOX account to continue.',401 if not user else 403)
        return user

    def am_audit(self, db, user, action, entity, entity_id, details=None):
        db.execute("INSERT INTO am_audit(user_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",(user['id'] if user else None,action,entity,str(entity_id),json.dumps(details or {}),now()))

    def am_get(self,path):
        with connect() as db:
            if path == '/api/auth/me':
                user=self.am_user(db)
                if user: user={k:user[k] for k in ('id','email','name','role','shop_id','rider_id')}
                return self.send_json({'user':user})
            if path == '/api/am/catalog':
                qs=parse_qs(urlparse(self.path).query); term=(qs.get('q',[''])[0] or '')[:80].strip(); cat=(qs.get('category',[''])[0] or '')[:80]; shop=(qs.get('shop',[''])[0] or '')[:80]
                sql="SELECT p.*,c.name category_name,c.slug category_slug,s.name shop_name,s.slug shop_slug,s.area shop_area,s.delivery_fee,s.is_open shop_open FROM am_products p JOIN am_categories c ON c.id=p.category_id JOIN shops s ON s.id=p.shop_id WHERE p.status='active' AND s.status='approved' AND s.is_open=1 AND p.stock_milli>0"
                args=[]
                if term: sql+=" AND (p.name LIKE ? OR p.description LIKE ? OR c.name LIKE ? OR s.name LIKE ?)"; args.extend([f'%{term}%']*4)
                if cat: sql+=" AND c.slug=?"; args.append(cat)
                if shop: sql+=" AND s.slug=?"; args.append(shop)
                sql+=" ORDER BY c.sort_order,s.name,p.name LIMIT 100"
                items=[dict(r) for r in db.execute(sql,args)]
                for x in items: x['unit_price']=x['sale_price'] if x['sale_price'] is not None else x['price']; x['price_display']=cents(x['unit_price']); x['stock_display']='Low stock' if x['stock_milli']<=x['low_stock_milli'] else 'In stock'
                categories=[dict(r) for r in db.execute('SELECT * FROM am_categories WHERE active=1 ORDER BY sort_order')]
                shops=[dict(r) for r in db.execute("SELECT id,name,slug,description,category,area,delivery_fee,is_open FROM shops WHERE status='approved' ORDER BY name")]
                return self.send_json({'items':items,'categories':categories,'shops':shops})
            if path.startswith('/api/am/products/'):
                pid=int(path.rsplit('/',1)[1]); item=db.execute("SELECT p.*,c.name category_name,c.slug category_slug,s.name shop_name,s.slug shop_slug,s.area shop_area,s.delivery_fee FROM am_products p JOIN am_categories c ON c.id=p.category_id JOIN shops s ON s.id=p.shop_id WHERE p.id=? AND p.status='active' AND s.status='approved'",(pid,)).fetchone()
                if not item: raise APIError('Product not found.',404)
                x=dict(item); x['unit_price']=x['sale_price'] if x['sale_price'] is not None else x['price']; x['price_display']=cents(x['unit_price']); return self.send_json(x)
            if path.startswith('/api/am/shops/'):
                slug=path.rsplit('/',1)[1]; shop=db.execute("SELECT id,name,slug,description,category,area,address,delivery_fee,minimum_order,is_open FROM shops WHERE slug=? AND status='approved'",(slug,)).fetchone()
                if not shop: raise APIError('Shop not found.',404)
                items=[dict(r) for r in db.execute("SELECT p.*,c.name category_name,c.slug category_slug FROM am_products p JOIN am_categories c ON c.id=p.category_id WHERE p.shop_id=? AND p.status='active' AND p.stock_milli>0 ORDER BY c.sort_order,p.name",(shop['id'],))]
                for x in items: x['unit_price']=x['sale_price'] if x['sale_price'] is not None else x['price']; x['price_display']=cents(x['unit_price'])
                return self.send_json({'shop':dict(shop),'items':items})
            if path == '/api/am/orders':
                user=self.am_require(db,'customer')
                rows=db.execute('SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 100',(user['id'],)).fetchall()
                return self.send_json({'items':[self.am_order_data(db,r) for r in rows]})
            if path == '/api/am/shop/dashboard' or path == '/api/am/shop/products' or path == '/api/am/shop/orders':
                user=self.am_require(db,'shop'); sid=user.get('shop_id')
                if not sid: raise APIError('This shop account is not linked to an approved shop.',403)
                if path.endswith('dashboard'):
                    return self.send_json({'shop':rowdict(db.execute('SELECT * FROM shops WHERE id=?',(sid,)).fetchone()),'metrics':{'products':db.execute('SELECT COUNT(*) FROM am_products WHERE shop_id=? AND status=\'active\'',(sid,)).fetchone()[0],'orders':db.execute('SELECT COUNT(*) FROM am_orders WHERE shop_id=?',(sid,)).fetchone()[0],'pending':db.execute("SELECT COUNT(*) FROM am_orders WHERE shop_id=? AND status='placed'",(sid,)).fetchone()[0]}})
                if path.endswith('products'):
                    return self.send_json({'items':[dict(r) for r in db.execute('SELECT p.*,c.name category_name FROM am_products p JOIN am_categories c ON c.id=p.category_id WHERE p.shop_id=? ORDER BY p.name',(sid,))]})
                return self.send_json({'items':[self.am_order_data(db,r) for r in db.execute('SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id WHERE o.shop_id=? ORDER BY o.id DESC LIMIT 100',(sid,))]})
            if path == '/api/am/admin/summary':
                self.am_require(db,'admin')
                return self.send_json({'shops':db.execute("SELECT COUNT(*) FROM shops WHERE status='approved'").fetchone()[0],'pending_shops':db.execute("SELECT COUNT(*) FROM shop_applications WHERE status='pending'").fetchone()[0],'products':db.execute("SELECT COUNT(*) FROM am_products WHERE status='active'").fetchone()[0],'orders_today':db.execute('SELECT COUNT(*) FROM am_orders WHERE created_at>=?',(now()[:10],)).fetchone()[0],'active_orders':db.execute("SELECT COUNT(*) FROM am_orders WHERE status NOT IN ('delivered','cancelled')").fetchone()[0],'gross':cents(db.execute("SELECT COALESCE(SUM(total),0) FROM am_orders WHERE status='delivered'").fetchone()[0])})
            if path == '/api/am/admin/shops':
                self.am_require(db,'admin'); return self.send_json({'items':[dict(r) for r in db.execute('SELECT * FROM shops ORDER BY id DESC')]})
            if path == '/api/am/admin/users':
                self.am_require(db,'admin'); role=parse_qs(urlparse(self.path).query).get('role',[''])[0]; rows=db.execute("SELECT id,email,name,role,shop_id,rider_id,active,created_at FROM am_users WHERE (?='' OR role=?) ORDER BY id DESC LIMIT 500",(role,role)).fetchall(); return self.send_json({'items':[dict(r) for r in rows]})
            if path == '/api/am/admin/orders':
                self.am_require(db,'admin'); rows=db.execute('SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id ORDER BY o.id DESC LIMIT 200').fetchall(); return self.send_json({'items':[self.am_order_data(db,r) for r in rows]})
            if path == '/api/am/admin/audit':
                self.am_require(db,'admin'); return self.send_json({'items':[dict(r) for r in db.execute('SELECT * FROM am_audit ORDER BY id DESC LIMIT 200')]})
            if path == '/api/am/customer/orders':
                user=self.am_require(db,'customer'); rows=db.execute('SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id WHERE o.user_id=? ORDER BY o.id DESC',(user['id'],)).fetchall(); return self.send_json({'items':[self.am_order_data(db,r) for r in rows]})
            if path == '/api/am/admin/applications':
                self.am_require(db,'admin'); return self.send_json({'items':[dict(r) for r in db.execute('SELECT * FROM shop_applications ORDER BY id DESC')]})
            if path == '/api/am/rider/assignments':
                user=self.am_require(db,'rider'); rows=db.execute("SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id WHERE o.rider_user_id=? ORDER BY o.id DESC LIMIT 100",(user['id'],)).fetchall(); return self.send_json({'items':[self.am_order_data(db,r) for r in rows]})
        raise APIError('AMNOX endpoint not found.',404)

    def am_order_data(self,db,row):
        o=dict(row); o['items']=[dict(x) for x in db.execute('SELECT product_name,unit,unit_price,quantity_milli,line_total FROM am_order_items WHERE order_id=?',(o['id'],))]; o['total_display']=cents(o['total']); return o

    def am_post(self,path,data):
        if APP_ENV == "demo" and path in {"/api/auth/register", "/api/am/shop-applications", "/api/am/orders"}:
            raise APIError("This DEMO ONLY preview does not accept account sign-ups, shop applications, or orders.", 403)
        if path == '/api/auth/login':
            email=str(data.get('email','')).strip().lower(); password=str(data.get('password',''))
            bucket = login_bucket(self, email)
            if login_is_limited(bucket): raise APIError('Too many sign-in attempts. Try again in 15 minutes.',429)
            with connect() as db:
                user=db.execute('SELECT * FROM am_users WHERE email=? AND active=1',(email,)).fetchone()
                valid=bool(user and hmac.compare_digest(user['password_hash'],hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(user['password_hash'].split('$')[1]),180000).hex()+'$'+user['password_hash'].split('$')[1])) if user else False
                if not valid:
                    login_failed(bucket)
                    raise APIError('Email or password is incorrect.',401)
                login_succeeded(bucket)
                token=secrets.token_urlsafe(36); db.execute('INSERT INTO am_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],int(time.time())+60*60*24*14))
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Set-Cookie',f'am_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1209600'+('; Secure' if secure_env() else '')); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(json.dumps({'user':{k:user[k] for k in ('id','email','name','role','shop_id','rider_id')}}).encode()); return
        if path == '/api/auth/logout':
            with connect() as db:
                u=self.am_user(db)
                if u:
                    token=next((x.strip().split('=',1)[1] for x in self.headers.get('Cookie','').split(';') if x.strip().startswith('am_session=')), '')
                    db.execute('DELETE FROM am_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            cookie='am_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+('; Secure' if secure_env() else '')
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Set-Cookie',cookie); self.end_headers(); self.wfile.write(b'{"ok":true}'); return
        if path == '/api/auth/register':
            email=str(data.get('email','')).strip().lower(); password=str(data.get('password','')); name=str(data.get('name','')).strip()[:100]
            if len(name)<2 or '@' not in email or len(password)<10: raise APIError('Enter your name, a valid email and a password of at least 10 characters.')
            salt=secrets.token_bytes(16); digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,180000).hex()+'$'+salt.hex()
            try:
                with connect() as db: db.execute('INSERT INTO am_users(email,name,role,password_hash,created_at) VALUES(?,?,?,?,?)',(email,name,'customer',digest,now()))
            except sqlite3.IntegrityError: raise APIError('An account with this email already exists.',409)
            return self.am_post('/api/auth/login',{'email':email,'password':password})
        if path == '/api/am/shop-applications':
            required={k:str(data.get(k,'')).strip() for k in ('shop_name','owner_name','email','phone','category','area','address')}
            if any(not v for v in required.values()) or '@' not in required['email']: raise APIError('Complete each shop application field with valid contact details.')
            with connect() as db:
                cur=db.execute('INSERT INTO shop_applications(shop_name,owner_name,email,phone,category,area,address,created_at) VALUES(?,?,?,?,?,?,?,?)',(*required.values(),now()))
                return self.send_json({'id':cur.lastrowid,'status':'pending'},201)
        if path == '/api/am/orders': return self.am_create_order(data)
        with connect() as db:
            if path.startswith('/api/am/shop/orders/'):
                user=self.am_require(db,'shop'); oid=int(path.rsplit('/',1)[1]); order=db.execute('SELECT * FROM am_orders WHERE id=? AND shop_id=?',(oid,user['shop_id'])).fetchone()
                if not order: raise APIError('Order not found for this shop.',404)
                action=data.get('action'); transitions={'accept':('placed','accepted'),'reject':('placed','cancelled'),'packing':('accepted','packing'),'ready':('packing','ready')}
                if action not in transitions: raise APIError('Unsupported shop order action.')
                old,new=transitions[action]
                if order['status']!=old: raise APIError(f"Cannot {action} an order in {order['status']} state.",409)
                if action=='reject' and len(str(data.get('reason','')).strip())<3: raise APIError('Provide a rejection reason.')
                if action=='reject':
                    for line in db.execute('SELECT product_id,quantity_milli FROM am_order_items WHERE order_id=?',(oid,)).fetchall():
                        db.execute('UPDATE am_products SET stock_milli=stock_milli+?,updated_at=? WHERE id=?',(line['quantity_milli'],now(),line['product_id']))
                        db.execute('INSERT INTO am_stock_moves(product_id,user_id,delta_milli,reason,created_at) VALUES(?,?,?,?,?)',(line['product_id'],user['id'],line['quantity_milli'],'restocked after rejected order '+order['public_id'],now()))
                db.execute('UPDATE am_orders SET status=?,updated_at=? WHERE id=?',(new,now(),oid)); db.execute('INSERT INTO am_order_history(order_id,status,actor_user_id,note,created_at) VALUES(?,?,?,?,?)',(oid,new,user['id'],str(data.get('reason',''))[:200],now())); self.am_audit(db,user,'shop_order_'+action,'order',oid)
                return self.send_json({'ok':True,'status':new})
            if path == '/api/am/shop/products':
                user=self.am_require(db,'shop'); sid=user.get('shop_id')
                if not sid: raise APIError('Shop account not linked to a shop.',403)
                name=str(data.get('name','')).strip()[:120]; desc=str(data.get('description','')).strip()[:500]; unit=str(data.get('unit','')).strip()[:30]; price=int(data.get('price',-1)); stock=int(data.get('stock_milli',-1)); cat=db.execute('SELECT id FROM am_categories WHERE slug=? AND active=1',(str(data.get('category','')),)).fetchone()
                if len(name)<2 or not unit or price<0 or stock<0 or not cat: raise APIError('Enter a valid product name, category, unit, price and stock.')
                cur=db.execute("INSERT INTO am_products(shop_id,category_id,name,description,unit,price,sale_price,stock_milli,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(sid,cat['id'],name,desc,unit,price,data.get('sale_price'),stock,now(),now())); self.am_audit(db,user,'product_created','product',cur.lastrowid); return self.send_json({'id':cur.lastrowid},201)
            if path.startswith('/api/am/shop/inventory/'):
                user=self.am_require(db,'shop'); pid=int(path.rsplit('/',1)[1]); delta=int(data.get('delta_milli',0)); reason=str(data.get('reason','')).strip()[:120]
                if delta==0 or not reason: raise APIError('Enter a non-zero stock adjustment and reason.')
                db.execute('BEGIN IMMEDIATE'); product=db.execute('SELECT * FROM am_products WHERE id=?',(pid,)).fetchone()
                if not product or product['shop_id']!=user['shop_id']: raise APIError('Product not found for this shop.',404)
                updated=product['stock_milli']+delta
                if updated<0: raise APIError('Adjustment would make stock negative.',409)
                db.execute('UPDATE am_products SET stock_milli=?,updated_at=? WHERE id=?',(updated,now(),pid)); db.execute('INSERT INTO am_stock_moves(product_id,user_id,delta_milli,reason,created_at) VALUES(?,?,?,?,?)',(pid,user['id'],delta,reason,now())); self.am_audit(db,user,'inventory_adjusted','product',pid,{'delta_milli':delta,'reason':reason}); return self.send_json({'ok':True,'stock_milli':updated})
            if path.startswith('/api/am/admin/applications/'):
                user=self.am_require(db,'admin'); app_id=int(path.rsplit('/',1)[1]); app=db.execute('SELECT * FROM shop_applications WHERE id=?',(app_id,)).fetchone(); action=data.get('action')
                if not app or app['status']!='pending' or action not in ('approve','reject'): raise APIError('Pending shop application not found.',404)
                status='approved' if action=='approve' else 'rejected'; note=str(data.get('note','')).strip()[:300]
                db.execute('UPDATE shop_applications SET status=?,review_note=?,reviewed_at=? WHERE id=?',(status,note,now(),app_id))
                if status=='approved':
                    slug=''.join(ch.lower() if ch.isalnum() else '-' for ch in app['shop_name']).strip('-'); slug='-'.join(x for x in slug.split('-') if x)+f'-{app_id}'
                    cur=db.execute("INSERT INTO shops(name,slug,description,category,area,address,phone,status,created_at) VALUES(?,?,?,?,?,?,?,'approved',?)",(app['shop_name'],slug,'',app['category'],app['area'],app['address'],app['phone'],now()))
                self.am_audit(db,user,'shop_application_'+action,'shop_application',app_id,{'note':note}); return self.send_json({'ok':True,'status':status})
            if path.startswith('/api/am/admin/shops/'):
                user=self.am_require(db,'admin'); shop_id=int(path.rsplit('/',1)[1]); action=data.get('action'); statuses={'suspend':'suspended','reactivate':'approved'}
                if action not in statuses: raise APIError('Choose suspend or reactivate.')
                shop=db.execute('SELECT * FROM shops WHERE id=?',(shop_id,)).fetchone()
                if not shop: raise APIError('Shop not found.',404)
                db.execute('UPDATE shops SET status=? WHERE id=?',(statuses[action],shop_id)); self.am_audit(db,user,'shop_'+action,'shop',shop_id); return self.send_json({'ok':True,'status':statuses[action]})
            if path.startswith('/api/am/admin/orders/'):
                user=self.am_require(db,'admin'); oid=int(path.rsplit('/',1)[1]); rider_id=int(data.get('rider_user_id',0)); order=db.execute('SELECT * FROM am_orders WHERE id=?',(oid,)).fetchone(); rider=db.execute("SELECT * FROM am_users WHERE id=? AND role='rider' AND active=1",(rider_id,)).fetchone()
                if not order or not rider: raise APIError('Order or authorized rider not found.',404)
                if order['status'] not in ('accepted','packing','ready'): raise APIError('This order is not ready for rider assignment.',409)
                db.execute('BEGIN IMMEDIATE')
                busy=db.execute("SELECT COUNT(*) FROM am_orders WHERE rider_user_id=? AND status NOT IN ('delivered','cancelled')",(rider_id,)).fetchone()[0]
                if busy: raise APIError('This rider already has an active delivery.',409)
                db.execute('UPDATE am_orders SET rider_user_id=?,status=\'assigned\',updated_at=? WHERE id=?',(rider_id,now(),oid)); db.execute('INSERT INTO am_deliveries(order_id,rider_user_id,status,assigned_at) VALUES(?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET rider_user_id=excluded.rider_user_id,status=excluded.status,assigned_at=excluded.assigned_at',(oid,rider_id,'assigned',now())); self.am_audit(db,user,'rider_assigned','order',oid,{'rider_user_id':rider_id}); return self.send_json({'ok':True})
            if path == '/api/am/rider/status':
                user=self.am_require(db,'rider'); oid=int(data.get('order_id',0)); action=data.get('action'); order=db.execute('SELECT * FROM am_orders WHERE id=? AND rider_user_id=?',(oid,user['id'])).fetchone(); mapping={'picked_up':('assigned','picked_up'),'en_route':('picked_up','en_route')}
                if not order: raise APIError('Delivery assignment not found.',404)
                if action=='complete':
                    import hashlib as _hashlib
                    if order['status']!='en_route' or int(time.time())>order['otp_expires'] or order['otp_attempts']>=5: raise APIError('Delivery code is expired or locked after five attempts.',403)
                    if not secrets.compare_digest(_hashlib.sha256(str(data.get('otp','')).encode()).hexdigest(),order['delivery_otp_hash']):
                        db.execute('UPDATE am_orders SET otp_attempts=otp_attempts+1 WHERE id=?',(oid,)); db.commit(); raise APIError('Delivery verification code is incorrect.',403)
                    db.execute('UPDATE am_orders SET status=\'delivered\',updated_at=? WHERE id=?',(now(),oid)); db.execute("UPDATE am_deliveries SET status='delivered',delivered_at=? WHERE order_id=?",(now(),oid)); db.execute('INSERT INTO am_order_history(order_id,status,actor_user_id,created_at) VALUES(?,?,?,?)',(oid,'delivered',user['id'],now())); return self.send_json({'ok':True,'status':'delivered'})
                if action not in mapping or order['status']!=mapping[action][0]: raise APIError('Delivery state transition is not allowed.',409)
                new=mapping[action][1]; db.execute('UPDATE am_orders SET status=?,updated_at=? WHERE id=?',(new,now(),oid)); db.execute('UPDATE am_deliveries SET status=?,picked_at=CASE WHEN ?=\'picked_up\' THEN ? ELSE picked_at END WHERE order_id=?',(new,new,now(),oid)); db.execute('INSERT INTO am_order_history(order_id,status,actor_user_id,created_at) VALUES(?,?,?,?)',(oid,new,user['id'],now())); return self.send_json({'ok':True,'status':new})
        raise APIError('AMNOX action not found.',404)

    def am_create_order(self,data):
        if APP_ENV == "demo": raise APIError("This DEMO ONLY preview does not accept orders.", 403)
        key=self.headers.get('Idempotency-Key','')[:100]
        if not key: raise APIError('Checkout needs an idempotency key.')
        name=str(data.get('name','')).strip()[:100]; phone=str(data.get('phone','')).strip()[:24]; address=str(data.get('address','')).strip()[:300]; instructions=str(data.get('instructions','')).strip()[:300]; shop_id=int(data.get('shop_id',0)); items=data.get('items')
        if len(name)<2 or len(phone)<8 or len(address)<8: raise APIError('Enter your name, phone and complete delivery address.')
        if not isinstance(items,list) or not items or len(items)>40: raise APIError('Your basket is empty or too large.')
        try: parsed=[(int(i['id']),int(i['quantity_milli'])) for i in items]
        except (ValueError,KeyError,TypeError): raise APIError('Basket quantities are invalid.')
        if any(q<250 or q>100000 for _,q in parsed) or len({pid for pid,_ in parsed})!=len(parsed): raise APIError('Check product quantities and remove duplicate lines.')
        user=None
        with connect() as db:
            user=self.am_user(db)
            if user and user['role']!='customer': raise APIError('Use the customer account to place an order.',403)
            db.execute('BEGIN IMMEDIATE'); previous=db.execute('SELECT * FROM am_orders WHERE idempotency_key=?',(key,)).fetchone()
            if previous:
                uid=user['id'] if user else None
                if previous['user_id']!=uid: raise APIError('This idempotency key belongs to another checkout.',409)
                return self.send_json(self.am_order_data(db,previous),200)
            shop=db.execute("SELECT * FROM shops WHERE id=? AND status='approved' AND is_open=1",(shop_id,)).fetchone()
            if not shop: raise APIError('This shop is unavailable for checkout.',409)
            subtotal=0; snapshots=[]
            for pid,qty in parsed:
                product=db.execute("SELECT * FROM am_products WHERE id=? AND shop_id=? AND status='active'",(pid,shop_id)).fetchone()
                if not product: raise APIError('A product is no longer available.',409)
                weighted=product['unit'].strip().lower() in ('kg','kilogram','litre','liter','l')
                if (qty%250 if weighted else qty%1000): raise APIError(f"Quantity step is invalid for {product['unit']}.")
                unit_price=product['sale_price'] if product['sale_price'] is not None else product['price']; line=(unit_price*qty+500)//1000
                changed=db.execute('UPDATE am_products SET stock_milli=stock_milli-?,updated_at=? WHERE id=? AND stock_milli>=?',(qty,now(),pid,qty)).rowcount
                if changed!=1: raise APIError(f"Not enough stock for {product['name']}.",409)
                subtotal+=line; snapshots.append((product,qty,unit_price,line))
            if subtotal<shop['minimum_order']: raise APIError('Basket is below this shop\'s minimum order.',409)
            fee=shop['delivery_fee']; method=str(data.get('payment_method','cod'))
            if method!='cod': raise APIError('Only Cash on Delivery is currently configured.')
            otp=f'{secrets.randbelow(1_000_000):06d}'; public='AM-'+secrets.token_hex(4).upper(); uid=user['id'] if user else None
            cur=db.execute("INSERT INTO am_orders(public_id,user_id,shop_id,customer_name,customer_phone,delivery_address,instructions,status,payment_method,payment_status,subtotal,delivery_fee,total,idempotency_key,delivery_otp_hash,otp_expires,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'placed',?,'authorized_cod',?,?,?,?,?,?,?,?)",(public,uid,shop_id,name,phone,address,instructions,method,subtotal,fee,subtotal+fee,key,hashlib.sha256(otp.encode()).hexdigest(),int(time.time())+3600,now(),now())); oid=cur.lastrowid
            for product,qty,price,line in snapshots:
                db.execute('INSERT INTO am_order_items(order_id,product_id,product_name,unit,unit_price,quantity_milli,line_total) VALUES(?,?,?,?,?,?,?)',(oid,product['id'],product['name'],product['unit'],price,qty,line)); db.execute('INSERT INTO am_stock_moves(product_id,delta_milli,reason,created_at) VALUES(?,?,?,?)',(product['id'],-qty,'customer order '+public,now()))
            db.execute('INSERT INTO am_order_history(order_id,status,created_at) VALUES(?,?,?)',(oid,'placed',now())); row=db.execute('SELECT o.*,s.name shop_name FROM am_orders o JOIN shops s ON s.id=o.shop_id WHERE o.id=?',(oid,)).fetchone()
            if APP_ENV=='development': print(f'[AMNOX local development only] Delivery OTP for {public}: {otp}')
            result=self.am_order_data(db,row); result['total']=subtotal+fee; return self.send_json(result,201)


def main():
    initialize()
    if len(sys.argv)>1 and sys.argv[1]=='link-shop-user':
        import argparse
        parser=argparse.ArgumentParser(description='Link a verified account to an approved AMNOX shop')
        parser.add_argument('--email',required=True); parser.add_argument('--shop-id',required=True,type=int)
        args=parser.parse_args(sys.argv[2:])
        with connect() as db:
            shop=db.execute("SELECT id FROM shops WHERE id=? AND status='approved'",(args.shop_id,)).fetchone()
            user=db.execute('SELECT id,role FROM am_users WHERE email=?',(args.email.strip().lower(),)).fetchone()
            if not shop: raise SystemExit('Shop id must refer to an approved shop.')
            if not user: raise SystemExit('Create the account first at /account/register, then link it after verifying the applicant.')
            if user['role'] not in ('customer','shop'): raise SystemExit('Only a customer account can be linked to a shop.')
            db.execute("UPDATE am_users SET role='shop',shop_id=? WHERE id=?",(shop['id'],user['id']))
        print(f'Linked verified account {args.email.strip().lower()} to approved shop {args.shop_id}.')
        return
    if len(sys.argv)>1 and sys.argv[1]=='create-user':
        import argparse
        parser=argparse.ArgumentParser(description='Provision an AMNOX operator account')
        parser.add_argument('--email',required=True); parser.add_argument('--name',required=True); parser.add_argument('--role',required=True,choices=['admin','shop','rider']); parser.add_argument('--shop-id',type=int); parser.add_argument('--rider-id',type=int)
        args=parser.parse_args(sys.argv[2:]); password=getpass.getpass('Set account password (10+ characters): ')
        if len(password)<10: raise SystemExit('Password must be at least 10 characters.')
        if args.role=='shop' and not args.shop_id: raise SystemExit('Shop accounts require --shop-id for an approved shop.')
        salt=secrets.token_bytes(16); digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,180000).hex()+'$'+salt.hex()
        with connect() as db:
            if args.role=='shop' and not db.execute("SELECT 1 FROM shops WHERE id=? AND status='approved'",(args.shop_id,)).fetchone(): raise SystemExit('Shop id must refer to an approved shop.')
            db.execute('INSERT INTO am_users(email,name,role,password_hash,shop_id,rider_id,created_at) VALUES(?,?,?,?,?,?,?)',(args.email.strip().lower(),args.name.strip(),args.role,digest,args.shop_id,args.rider_id,now()))
        print(f'Provisioned {args.role} account for {args.email.strip().lower()}.')
        return
    host = os.environ.get("HOST", "0.0.0.0" if secure_env() else "127.0.0.1")
    port_value = os.environ.get("PORT")
    if secure_env() and not port_value:
        raise RuntimeError("Set PORT to the port assigned by the hosting platform.")
    port = int(port_value or "8000")
    print(f"AMNOX server listening on {host}:{port}")
    if APP_ENV == "development": print("Development data is enabled; panel roles require provisioned accounts. Do not expose this server publicly.")
    if APP_ENV == "demo": print("DEMO ONLY: synthetic catalog enabled; sign-ups, shop applications, and orders are disabled.")
    ThreadingHTTPServer((host,port),Handler).serve_forever()


if __name__ == "__main__": main()
