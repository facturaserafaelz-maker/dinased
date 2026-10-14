"""
DINASED - Control de asistencia con geocerca y firma de dispositivo.

Dependencias:  pip install flask "qrcode[pil]" cryptography gunicorn

Variables de entorno (todas opcionales):
  DINASED_SECRET_KEY      Clave de sesión. Si no existe, se genera y se guarda en .dinased_secret
  DINASED_ADMIN_PASSWORD  Contraseña inicial del administrador "admin" (solo al crear la base).
                          Si no se define, se genera una aleatoria y se muestra UNA vez en consola.
  DINASED_HTTPS=1         Marca la cookie de sesión como Secure (usar solo si se sirve por HTTPS).
  DINASED_CERT/DINASED_KEY  Rutas a certificado y llave para que Flask sirva HTTPS directamente.
  DINASED_BEHIND_PROXY=1  Confiar en las cabeceras X-Forwarded-* de un proxy inverso.
  DINASED_PUBLIC_URL      URL pública base para el código QR (ej. https://asistencia.midominio.ec).
  DINASED_DB              Ruta alternativa del archivo SQLite.

IMPORTANTE: la geolocalización y Web Crypto del navegador SOLO funcionan en HTTPS
(o localhost). Para usarlo desde celulares en la red local necesita HTTPS.
"""
import os, io, re, json, math, time, sqlite3, secrets, base64, threading
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, request, redirect, url_for, session, render_template,
                   render_template_string, flash, get_flashed_messages, jsonify,
                   send_file, abort)
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("DINASED_DB") or os.path.join(APP_DIR, "dinased.db")
DEFAULT_ADMIN_PASSWORD = "Cambiar123!"  # solo para detectar si sigue en uso


def load_secret_key():
    """Clave de sesión estable entre reinicios y entre varios workers."""
    env = os.environ.get("DINASED_SECRET_KEY")
    if env:
        return env
    path = os.path.join(APP_DIR, ".dinased_secret")
    try:
        with open(path) as f:
            k = f.read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    except OSError:
        return secrets.token_hex(32)
    k = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k)
        return k
    except FileExistsError:
        # Otro worker la creó al mismo tiempo: leerla.
        for _ in range(20):
            try:
                with open(path) as f:
                    v = f.read().strip()
                if v:
                    return v
            except OSError:
                pass
            time.sleep(0.1)
    except OSError:
        pass
    return k


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("DINASED_HTTPS") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)
if os.environ.get("DINASED_BEHIND_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------
def db():
    # Espera hasta 30 segundos si otra solicitud está usando SQLite.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def get_db():
    """Garantiza que la conexión se cierre aunque ocurra una excepción."""
    con = db()
    try:
        yield con
    finally:
        con.close()


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_db():
    with get_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'sub',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            cedula TEXT UNIQUE NOT NULL,
            department TEXT NOT NULL,
            public_key TEXT,
            device_fingerprint TEXT,
            approved INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy REAL,
            result TEXT NOT NULL DEFAULT 'ACEPTADO',
            companion TEXT,
            vehicle TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS challenges (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_username TEXT,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        # Migración: bases antiguas no tienen la columna "role".
        cols = [r["name"] for r in con.execute("PRAGMA table_info(admins)").fetchall()]
        if "role" not in cols:
            con.execute("ALTER TABLE admins ADD COLUMN role TEXT NOT NULL DEFAULT 'sub'")
        # Migración: bases antiguas no tienen la columna "companion" (con quién sale).
        ev_cols = [r["name"] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        if "companion" not in ev_cols:
            con.execute("ALTER TABLE events ADD COLUMN companion TEXT")
        if "vehicle" not in ev_cols:
            con.execute("ALTER TABLE events ADD COLUMN vehicle TEXT")
        # Debe existir siempre un administrador principal: "admin" o, si no, el más antiguo.
        con.execute("UPDATE admins SET role='admin' WHERE username='admin'")
        if not con.execute("SELECT id FROM admins WHERE role='admin' LIMIT 1").fetchone():
            con.execute("UPDATE admins SET role='admin' WHERE id=(SELECT MIN(id) FROM admins)")
        defaults = {
            "place_name": "Recinto DINASED",
            "latitude": "-2.170998",
            "longitude": "-79.922359",
            "radius_m": "100",
            "max_accuracy_m": "80",
            "registration_open": "1",
            "qr_url": ""
        }
        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

        # Administrador inicial (sin contraseña fija conocida).
        if not con.execute("SELECT id FROM admins LIMIT 1").fetchone():
            env_pwd = os.environ.get("DINASED_ADMIN_PASSWORD")
            pwd = env_pwd or secrets.token_urlsafe(12)
            cur = con.execute("INSERT OR IGNORE INTO admins(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                              ("admin", generate_password_hash(pwd), "admin", now()))
            if cur.rowcount == 1 and not env_pwd:
                print("=" * 70)
                print(" ADMINISTRADOR INICIAL CREADO")
                print("   usuario:    admin")
                print(f"   contraseña: {pwd}")
                print(" Guárdela ahora y cámbiela en /admin/password. No se volverá a mostrar.")
                print("=" * 70)
        con.commit()

        # Aviso si una base antigua todavía usa la contraseña por defecto.
        row = con.execute("SELECT password_hash FROM admins WHERE username='admin'").fetchone()
        if row and check_password_hash(row["password_hash"], DEFAULT_ADMIN_PASSWORD):
            print("*** ADVERTENCIA: el usuario 'admin' usa la contraseña por defecto. "
                  "Cámbiela en /admin/password. ***")


def get_settings():
    with get_db() as con:
        rows = con.execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def audit(action, details, con=None):
    # Usa la conexión de la operación en curso cuando se proporciona,
    # evitando que otra conexión intente escribir mientras SQLite ya está bloqueado.
    owns_connection = con is None
    if owns_connection:
        con = db()
    try:
        con.execute("INSERT INTO audit(admin_username,action,details,created_at) VALUES(?,?,?,?)",
                    (session.get("admin"), action, details, now()))
        if owns_connection:
            con.commit()
    finally:
        if owns_connection:
            con.close()


# ----------------------------------------------------------------------------
# Utilidades de seguridad y validación
# ----------------------------------------------------------------------------
_rate = {}
_rate_lock = threading.Lock()


def too_many(key, limit, window):
    """Limitador simple en memoria (por proceso). True si se superó el límite."""
    t = time.monotonic()
    with _rate_lock:
        q = [x for x in _rate.get(key, []) if t - x < window]
        if len(q) >= limit:
            _rate[key] = q
            return True
        q.append(t)
        _rate[key] = q
        if len(_rate) > 5000:  # limpieza ocasional
            for k in [k for k, v in _rate.items() if not v or t - v[-1] > 3600]:
                _rate.pop(k, None)
    return False


def client_ip():
    return request.remote_addr or "?"


def current_admin():
    """Fila del administrador en sesión, o None. Se consulta en cada petición para que
    un subadministrador eliminado pierda el acceso de inmediato."""
    name = session.get("admin")
    if not name:
        return None
    with get_db() as con:
        return con.execute("SELECT id,username,role FROM admins WHERE username=?", (name,)).fetchone()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_admin() is None:
            session.clear()
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def principal_required(fn):
    """Solo el administrador principal (role='admin')."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        me = current_admin()
        if me is None:
            session.clear()
            return redirect(url_for("login"))
        if me["role"] != "admin":
            flash("Solo el administrador principal puede realizar esta acción.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def csrf_origin_check():
    """Protección CSRF sin tocar las plantillas: en peticiones que modifican datos
    (salvo /api/*, que se autentican con firma criptográfica) el Origin/Referer
    debe coincidir con el host del servidor."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api/"):
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source or urlparse(source).netloc != request.host:
            abort(403)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_p256_public_key(public_key_b64):
    """Devuelve la clave pública si es una clave EC P-256 válida; si no, None."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        key = load_der_public_key(b64u_decode(public_key_b64))
        if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
            return key
    except Exception:
        pass
    return None


def verify_signature(public_key_b64, message, signature_b64):
    """Verifica una firma ECDSA P-256/SHA-256 en formato r||s (WebCrypto)."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        pub = load_p256_public_key(public_key_b64)
        if pub is None:
            return False
        raw_sig = b64u_decode(signature_b64)
        if len(raw_sig) != 64:
            return False
        r = int.from_bytes(raw_sig[:32], "big")
        ss = int.from_bytes(raw_sig[32:], "big")
        pub.verify(encode_dss_signature(r, ss), message.encode("utf-8"),
                   ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def cedula_valida(c):
    """Cédula ecuatoriana: 10 dígitos, provincia válida y dígito verificador (módulo 10)."""
    if not (c.isdigit() and len(c) == 10):
        return False
    prov = int(c[:2])
    if not (1 <= prov <= 24 or prov == 30) or int(c[2]) > 5:
        return False
    total = 0
    for i in range(9):
        d = int(c[i]) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return (10 - total % 10) % 10 == int(c[9])


def mask_name(full_name):
    """Oculta parte del nombre (p. ej. 'PEREZ LOPEZ JUAN' -> 'PE*** LO*** JU***')."""
    return " ".join(w[:2] + "*" * max(len(w) - 2, 1) for w in full_name.split())


def valid_location(lat, lon, accuracy):
    return (all(math.isfinite(v) for v in (lat, lon, accuracy))
            and -90 <= lat <= 90 and -180 <= lon <= 180 and accuracy >= 0)


@app.context_processor
def inject_globals():
    me = current_admin() if session.get("admin") else None
    return {"brand": "DINASED", "settings": get_settings(),
            "is_principal": bool(me and me["role"] == "admin")}


# ----------------------------------------------------------------------------
# Páginas públicas y sesión de administrador
# ----------------------------------------------------------------------------
@app.route("/")
def home():
    if session.get("admin"):
        return redirect(url_for("dashboard"))
    return render_template("home.html")


# Hash falso para que el tiempo de respuesta no revele si el usuario existe.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if too_many(f"login:{client_ip()}", 10, 300):
            flash("Demasiados intentos. Espere unos minutos e intente de nuevo.", "error")
            return render_template("login.html"), 429
        username = request.form.get("username", "").strip()[:100]
        password = request.form.get("password", "")[:200]
        with get_db() as con:
            row = con.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
        if row and ok:
            session.clear()
            session["admin"] = username
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin/password", methods=["GET", "POST"])
@admin_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        with get_db() as con:
            row = con.execute("SELECT * FROM admins WHERE username=?", (session["admin"],)).fetchone()
            if not row or not check_password_hash(row["password_hash"], current):
                flash("La contraseña actual no es correcta.", "error")
            elif len(new) < 12:
                flash("La nueva contraseña debe tener al menos 12 caracteres.", "error")
            elif new != confirm:
                flash("La confirmación no coincide.", "error")
            elif new == current:
                flash("La nueva contraseña debe ser distinta de la actual.", "error")
            else:
                con.execute("UPDATE admins SET password_hash=? WHERE id=?",
                            (generate_password_hash(new), row["id"]))
                audit("CAMBIAR_CONTRASENA", session["admin"], con=con)
                con.commit()
                flash("Contraseña actualizada.", "success")
                return redirect(url_for("dashboard"))
    return render_template("password.html")


@app.route("/dashboard")
@admin_required
def dashboard():
    with get_db() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        pending = con.execute("SELECT COUNT(*) n FROM users WHERE approved=0 AND active=1").fetchone()["n"]
        # substr evita que SQLite convierta a UTC la fecha con zona horaria (-05:00).
        events_today = con.execute(
            "SELECT COUNT(*) n FROM events WHERE substr(event_time,1,10)=date('now','localtime')"
        ).fetchone()["n"]
        # "Dentro" = último movimiento aceptado es una ENTRADA de hoy y el usuario sigue activo.
        inside = con.execute("""
            SELECT COUNT(*) n FROM events e
            JOIN (SELECT user_id, MAX(id) maxid FROM events WHERE result='ACEPTADO' GROUP BY user_id) x
              ON e.id=x.maxid
            JOIN users u ON u.id=e.user_id
            WHERE e.event_type='ENTRADA' AND u.active=1
              AND substr(e.event_time,1,10)=date('now','localtime')
        """).fetchone()["n"]
        latest = con.execute("""SELECT e.*,u.full_name,u.cedula,u.department FROM events e
                                JOIN users u ON u.id=e.user_id ORDER BY e.id DESC LIMIT 12""").fetchall()
    return render_template("dashboard.html", total=total, pending=pending,
                           events_today=events_today, inside=inside, latest=latest)


# ----------------------------------------------------------------------------
# Inscripción de usuarios
# ----------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    s = get_settings()
    if s.get("registration_open", "1") != "1":
        flash("La inscripción está cerrada temporalmente. Contacte al administrador.", "error")
        return render_template("register.html", closed=True)
    if request.method == "POST":
        if too_many(f"register:{client_ip()}", 30, 600):
            flash("Demasiados intentos. Espere unos minutos e intente de nuevo.", "error")
            return render_template("register.html", closed=False), 429
        full_name = " ".join(request.form.get("full_name", "").split()).upper()
        phone = request.form.get("phone", "").strip()
        cedula = request.form.get("cedula", "").strip()
        department = request.form.get("department", "").strip()
        public_key = request.form.get("public_key", "").strip()
        if not all([full_name, phone, cedula, department, public_key]):
            flash("Complete todos los campos y vincule el dispositivo.", "error")
            return render_template("register.html", closed=False)
        if len(full_name) > 100:
            flash("El nombre es demasiado largo (máximo 100 caracteres).", "error")
            return render_template("register.html", closed=False)
        if not re.fullmatch(r"[0-9+()\- ]{7,20}", phone):
            flash("Ingrese un teléfono válido (solo números, +, espacios o guiones).", "error")
            return render_template("register.html", closed=False)
        if department not in ("Desaparición Forzada", "Desaparición Involuntaria"):
            flash("Seleccione un departamento válido.", "error")
            return render_template("register.html", closed=False)
        if not cedula_valida(cedula):
            flash("La cédula no es válida. Verifique que tenga 10 dígitos correctos.", "error")
            return render_template("register.html", closed=False)
        if len(public_key) > 300 or load_p256_public_key(public_key) is None:
            flash("No se pudo validar la llave del dispositivo. Vincule el dispositivo de nuevo.", "error")
            return render_template("register.html", closed=False)
        saved = False
        with get_db() as con:
            existing = con.execute("SELECT id, public_key FROM users WHERE cedula=?", (cedula,)).fetchone()
            if existing is None:
                try:
                    con.execute("""INSERT INTO users(full_name,phone,cedula,department,public_key,approved,active,created_at)
                                   VALUES(?,?,?,?,?,0,1,?)""",
                                (full_name, phone, cedula, department, public_key, now()))
                    con.commit()
                    saved = True
                    flash("Solicitud recibida. El administrador debe aprobarla antes de que pueda registrar movimientos.", "success")
                except sqlite3.IntegrityError:
                    flash("Esta cédula ya está registrada. Contacte al administrador si necesita corregir sus datos.", "error")
            elif existing["public_key"] is None:
                # Dispositivo revocado por el administrador: permite vincular uno nuevo,
                # pero exige una nueva aprobación.
                con.execute("UPDATE users SET public_key=?, phone=?, approved=0 WHERE id=?",
                            (public_key, phone, existing["id"]))
                audit("SOLICITUD_NUEVO_DISPOSITIVO", f"ID {existing['id']}", con=con)
                con.commit()
                saved = True
                flash("Nuevo dispositivo recibido. El administrador debe aprobarlo de nuevo antes de que pueda registrar movimientos.", "success")
            else:
                flash("Esta cédula ya está registrada. Contacte al administrador si necesita corregir sus datos.", "error")
        return redirect(url_for("register", ok=1) if saved else url_for("register"))
    return render_template("register.html", closed=False)


# ----------------------------------------------------------------------------
# Administración
# ----------------------------------------------------------------------------
@app.route("/admin/users")
@admin_required
def users():
    with get_db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_template("users.html", users=rows)


@app.route("/admin/users/<int:user_id>/action", methods=["POST"])
@admin_required
def user_action(user_id):
    action = request.form.get("action")
    if action not in ("approve", "block", "activate", "revoke", "delete"):
        flash("Acción no reconocida.", "error")
        return redirect(url_for("users"))
    with get_db() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            abort(404)
        if action == "approve":
            con.execute("UPDATE users SET approved=1 WHERE id=?", (user_id,))
            audit("APROBAR_USUARIO", f"ID {user_id}", con=con)
        elif action == "block":
            con.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
            audit("BLOQUEAR_USUARIO", f"ID {user_id}", con=con)
        elif action == "activate":
            con.execute("UPDATE users SET active=1 WHERE id=?", (user_id,))
            audit("ACTIVAR_USUARIO", f"ID {user_id}", con=con)
        elif action == "revoke":
            con.execute("UPDATE users SET public_key=NULL, approved=0 WHERE id=?", (user_id,))
            con.execute("DELETE FROM challenges WHERE user_id=?", (user_id,))
            audit("REVOCAR_DISPOSITIVO", f"ID {user_id}", con=con)
        elif action == "delete":
            con.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM challenges WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM users WHERE id=?", (user_id,))
            audit("ELIMINAR_USUARIO", f"ID {user_id}", con=con)
        con.commit()
    msgs = {
        "approve": "Usuario aprobado.",
        "block": "Usuario bloqueado.",
        "activate": "Usuario activado.",
        "revoke": "Dispositivo revocado. El usuario debe inscribirse de nuevo desde su celular.",
        "delete": f"Usuario «{row['full_name']}» eliminado junto con sus movimientos.",
    }
    flash(msgs[action], "success")
    return redirect(url_for("users"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        try:
            lat = float(request.form.get("latitude", ""))
            lon = float(request.form.get("longitude", ""))
            radius = float(request.form.get("radius_m", ""))
            accuracy = float(request.form.get("max_accuracy_m", ""))
            # Las comparaciones encadenadas también rechazan NaN e infinito.
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("coordenadas")
            if not (10 <= radius <= 5000 and 10 <= accuracy <= 500):
                raise ValueError("radio/precisión")
        except ValueError:
            flash("Revise las coordenadas y los valores del radio/precisión.", "error")
        else:
            values = {
                "place_name": request.form.get("place_name", "Recinto DINASED").strip()[:100],
                "latitude": lat, "longitude": lon, "radius_m": radius,
                "max_accuracy_m": accuracy,
                "registration_open": "1" if request.form.get("registration_open") == "on" else "0"
            }
            with get_db() as con:  # una sola transacción para todos los valores
                for k, v in values.items():
                    con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
                audit("MODIFICAR_CONFIGURACION", json.dumps(values, ensure_ascii=False), con=con)
                con.commit()
            flash("Configuración guardada.", "success")
            return redirect(url_for("admin_settings"))
    return render_template("settings.html", s=get_settings())


@app.route("/admin/admins", methods=["GET", "POST"])
@principal_required
def admins():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(username) > 50 or len(password) < 12:
            flash("El usuario debe tener entre 3 y 50 caracteres y la contraseña al menos 12.", "error")
        else:
            with get_db() as con:
                try:
                    con.execute("INSERT INTO admins(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                                (username, generate_password_hash(password), "sub", now()))
                    audit("CREAR_SUBADMINISTRADOR", username, con=con)
                    con.commit()
                    flash("Subadministrador creado.", "success")
                except sqlite3.IntegrityError:
                    flash("Ese nombre de administrador ya existe.", "error")
        return redirect(url_for("admins"))
    with get_db() as con:
        rows = con.execute("SELECT id,username,role,created_at FROM admins ORDER BY id").fetchall()
    return render_template("admins.html", admins=rows)


@app.route("/admin/admins/<int:admin_id>/delete", methods=["POST"])
@principal_required
def delete_admin(admin_id):
    with get_db() as con:
        row = con.execute("SELECT id,username,role FROM admins WHERE id=?", (admin_id,)).fetchone()
        if not row:
            flash("El subadministrador no existe.", "error")
        elif row["role"] == "admin" or row["username"] == session.get("admin"):
            flash("No se puede eliminar al administrador principal ni a su propia cuenta.", "error")
        else:
            con.execute("DELETE FROM admins WHERE id=?", (admin_id,))
            audit("ELIMINAR_SUBADMINISTRADOR", row["username"], con=con)
            con.commit()
            flash("Subadministrador eliminado.", "success")
    return redirect(url_for("admins"))


def event_filters():
    """Lee y valida los filtros del historial (GET) para la vista y las exportaciones."""
    def date_arg(name):
        v = request.args.get(name, "").strip()
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            return ""
    tipo = request.args.get("tipo", "").strip().upper()
    if tipo not in ("ENTRADA", "SALIDA"):
        tipo = ""
    return {
        "q": request.args.get("q", "").strip()[:100],
        "desde": date_arg("desde"),
        "hasta": date_arg("hasta"),
        "tipo": tipo,
        "depto": request.args.get("depto", "").strip()[:100],
    }


def query_events(f, limit=None):
    q = f["q"]
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = """SELECT e.*,u.full_name,u.cedula,u.department FROM events e
            JOIN users u ON u.id=e.user_id
            WHERE (?='' OR u.full_name LIKE ? ESCAPE '\\' OR u.cedula LIKE ? ESCAPE '\\'
                   OR u.department LIKE ? ESCAPE '\\'
                   OR e.companion LIKE ? ESCAPE '\\' OR e.vehicle LIKE ? ESCAPE '\\')"""
    params = [q, like, like, like, like, like]
    if f["desde"]:
        sql += " AND substr(e.event_time,1,10) >= ?"
        params.append(f["desde"])
    if f["hasta"]:
        sql += " AND substr(e.event_time,1,10) <= ?"
        params.append(f["hasta"])
    if f["tipo"]:
        sql += " AND e.event_type = ?"
        params.append(f["tipo"])
    if f["depto"]:
        sql += " AND u.department = ?"
        params.append(f["depto"])
    sql += " ORDER BY e.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db() as con:
        return con.execute(sql, params).fetchall()


def event_departments():
    with get_db() as con:
        return [r["department"] for r in con.execute(
            "SELECT DISTINCT department FROM users ORDER BY department").fetchall()]


@app.route("/admin/events")
@admin_required
def events():
    f = event_filters()
    rows = query_events(f, 501)
    truncated = len(rows) > 500
    return render_template("events.html", events=rows[:500], f=f, truncated=truncated,
                           departments=event_departments(),
                           qs={k: v for k, v in f.items() if v})


def event_export_rows(f):
    rows = []
    for e in query_events(f):
        try:
            when = datetime.fromisoformat(e["event_time"]).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            when = e["event_time"]
        rows.append([when, e["full_name"], e["cedula"], e["department"], e["event_type"],
                     e["companion"], e["vehicle"]])
    return rows


EVENT_HEADERS = ["Fecha/hora", "Nombre", "Cédula", "Departamento", "Movimiento",
                 "Sale con", "Vehículo"]


def filter_summary(f):
    return "; ".join(f"{k}='{v}'" for k, v in f.items() if v) or "sin filtros"


@app.route("/admin/events/export.csv")
@admin_required
def events_export_csv():
    """Exporta a CSV (UTF-8 con BOM, separador ';' para Excel en español)."""
    import csv
    f = event_filters()
    rows = event_export_rows(f)

    def safe(v):
        v = "" if v is None else str(v)
        # Evita inyección de fórmulas al abrir en Excel
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(EVENT_HEADERS)
    for r in rows:
        w.writerow([safe(x) if isinstance(x, str) or x is None else x for x in r])
    data = io.BytesIO(("\ufeff" + buf.getvalue()).encode("utf-8"))
    audit("EXPORTAR_MOVIMIENTOS", f"{len(rows)} filas csv; filtro: {filter_summary(f)}")
    fname = "Movimientos_DINASED_" + datetime.now().strftime("%Y%m%d_%H%M") + ".csv"
    return send_file(data, as_attachment=True, download_name=fname, mimetype="text/csv")


@app.route("/qr.png")
@admin_required
def qr_png():
    # QR apunta a la URL pública del servidor (DINASED_PUBLIC_URL) o, si no existe, a la actual.
    host = os.environ.get("DINASED_PUBLIC_URL", "").rstrip("/") or request.host_url.rstrip("/")
    img = qrcode.make(host + url_for("scan"))
    mem = io.BytesIO()
    img.save(mem, format="PNG")
    mem.seek(0)
    return send_file(mem, mimetype="image/png", as_attachment=True, download_name="DINASED_QR.png")


# ----------------------------------------------------------------------------
# API de marcación
# ----------------------------------------------------------------------------
def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.route("/api/challenge", methods=["POST"])
def challenge():
    data = json_body()
    cedula = str(data.get("cedula") or "").strip()[:20]
    if too_many(f"chal-ip:{client_ip()}", 120, 60) or too_many(f"chal-ced:{cedula}", 10, 60):
        return jsonify(ok=False, message="Demasiados intentos. Espere un minuto."), 429
    with get_db() as con:
        user = con.execute("SELECT * FROM users WHERE cedula=? AND active=1 AND approved=1", (cedula,)).fetchone()
        if not user or not user["public_key"]:
            return jsonify(ok=False, message="Usuario no autorizado o dispositivo no vinculado."), 403
        wait = cooldown_remaining(con, user["id"])
        if wait > 0:
            return jsonify(ok=False, message=cooldown_message(wait), code="cooldown", retry_after=wait), 429
        ts = time.time()
        token = secrets.token_urlsafe(32)
        expires = ts + 120
        # Limpieza: desafíos vencidos y máximo 4 vigentes previos por usuario.
        con.execute("DELETE FROM challenges WHERE expires_at < ?", (ts,))
        con.execute("""DELETE FROM challenges WHERE user_id=? AND token NOT IN
                       (SELECT token FROM challenges WHERE user_id=? ORDER BY expires_at DESC LIMIT 4)""",
                    (user["id"], user["id"]))
        con.execute("INSERT INTO challenges(token,user_id,expires_at) VALUES(?,?,?)", (token, user["id"], expires))
        con.commit()
    # Una vez inscrito, el usuario siempre elige ENTRADA o SALIDA.
    return jsonify(ok=True, token=token, user_id=user["id"], expires=expires, can_choose=True)


EVENT_TYPES = ("ENTRADA", "SALIDA")

# Tiempo mínimo entre dos marcaciones del mismo celular/usuario (5 minutos por defecto).
try:
    COOLDOWN_SECONDS = int(os.environ.get("DINASED_COOLDOWN_SECONDS", "300"))
except ValueError:
    COOLDOWN_SECONDS = 300


def cooldown_remaining(con, user_id):
    """Segundos que faltan para que este usuario pueda volver a marcar (0 si ya puede)."""
    row = con.execute("""SELECT event_time FROM events
                          WHERE user_id=? AND result='ACEPTADO' ORDER BY id DESC LIMIT 1""",
                      (user_id,)).fetchone()
    if not row:
        return 0
    try:
        last = datetime.fromisoformat(row["event_time"])
        elapsed = (datetime.now().astimezone() - last).total_seconds()
    except (ValueError, TypeError):
        return 0
    return min(COOLDOWN_SECONDS, max(0, int(math.ceil(COOLDOWN_SECONDS - elapsed))))


def cooldown_message(remaining):
    m, sec = divmod(remaining, 60)
    return f"Este celular ya registró un movimiento. Podrá volver a marcar en {m} min {sec:02d} s."


def reject_mark(con, user_id, reason, status, message, code=None):
    """Registra el intento rechazado en la auditoría y responde."""
    audit("MARCACION_RECHAZADA", f"user_id={user_id}; {reason}", con=con)
    con.commit()
    return jsonify(ok=False, message=message, code=code), status


@app.route("/api/mark", methods=["POST"])
def mark():
    if too_many(f"mark:{client_ip()}", 120, 60):
        return jsonify(ok=False, message="Demasiados intentos. Espere un minuto."), 429
    data = json_body()
    token = str(data.get("token", ""))[:200]
    signature = str(data.get("signature", ""))[:200]
    try:
        lat, lon, accuracy = (float(data.get(k)) for k in ("latitude", "longitude", "accuracy"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="No se recibió una ubicación válida."), 400
    # Rechaza NaN/infinito y valores fuera de rango (NaN evadiría las comparaciones).
    if not valid_location(lat, lon, accuracy):
        return jsonify(ok=False, message="No se recibió una ubicación válida."), 400
    # Tipo elegido por el usuario (opcional). Se valida antes de consumir el desafío.
    raw_type = data.get("event_type")
    requested = None
    if raw_type not in (None, ""):
        requested = str(raw_type).strip().upper()
        if requested not in EVENT_TYPES:
            return jsonify(ok=False, message="Tipo de marcación no válido."), 400
    # "Con quién sale": obligatorio al elegir SALIDA; se ignora en ENTRADA.
    companion = " ".join(str(data.get("companion") or "").split())[:150]
    if requested == "SALIDA" and not companion:
        return jsonify(ok=False, message="Indique con quién sale (escriba «Solo» si sale sin acompañante)."), 400
    vehicle = " ".join(str(data.get("vehicle") or "").split())[:100]
    if requested == "SALIDA" and not vehicle:
        return jsonify(ok=False, message="Indique el vehículo (escriba «Ninguno» si no usa vehículo)."), 400
    if requested != "SALIDA":
        companion = ""
        vehicle = ""

    with get_db() as con:
        # Bloqueo de escritura desde el inicio: evita que dos solicitudes con el mismo
        # desafío registren dos eventos a la vez.
        con.execute("BEGIN IMMEDIATE")
        ch = con.execute("SELECT * FROM challenges WHERE token=?", (token,)).fetchone()
        if not ch:
            return jsonify(ok=False, message="La validación venció. Vuelva a intentarlo."), 403
        if ch["expires_at"] < time.time():
            con.execute("DELETE FROM challenges WHERE token=?", (token,))
            con.commit()
            return jsonify(ok=False, message="La validación venció. Vuelva a intentarlo."), 403
        user = con.execute("SELECT * FROM users WHERE id=? AND active=1 AND approved=1", (ch["user_id"],)).fetchone()
        if not user or not user["public_key"]:
            return jsonify(ok=False, message="Usuario o dispositivo no autorizado."), 403
        wait = cooldown_remaining(con, user["id"])
        if wait > 0:
            return jsonify(ok=False, message=cooldown_message(wait), code="cooldown", retry_after=wait), 429
        message = f"{token}|{user['id']}|DINASED-MARCACION-V1"
        if not verify_signature(user["public_key"], message, signature):
            return reject_mark(con, user["id"], "firma inválida", 403,
                               "La credencial de este celular no coincide con la registrada. "
                               "Pulse «Restablecer este celular» y pida al administrador que "
                               "revoque o elimine su registro para inscribirse de nuevo.",
                               code="bad_signature")
        s = get_settings()
        try:
            max_acc = float(s["max_accuracy_m"])
            radius = float(s["radius_m"])
            center_lat, center_lon = float(s["latitude"]), float(s["longitude"])
        except (KeyError, ValueError):
            return jsonify(ok=False, message="Configuración del recinto inválida. Contacte al administrador."), 500
        # Condiciones escritas en positivo: cualquier valor anómalo se rechaza.
        if not (accuracy <= max_acc):
            return reject_mark(con, user["id"], f"precisión {accuracy:.0f} m", 400,
                               f"GPS demasiado impreciso ({accuracy:.0f} m). Active ubicación precisa y vuelva a intentar.")
        dist = distance_m(lat, lon, center_lat, center_lon)
        if not (dist <= radius):
            return reject_mark(con, user["id"], f"fuera de radio ({dist:.0f} m)", 403,
                               f"Fuera del radio autorizado. Distancia aproximada: {dist:.0f} m.")
        # Consumir el desafío (un solo uso).
        if con.execute("DELETE FROM challenges WHERE token=?", (token,)).rowcount != 1:
            return jsonify(ok=False, message="La validación venció. Vuelva a intentarlo."), 403
        ts_now = now()
        if requested:
            # El usuario elige libremente ENTRADA o SALIDA; no se valida repetición.
            event_type = requested
        else:
            # Cliente sin selector: alterna según la última marcación de hoy.
            last = con.execute("""SELECT event_type FROM events
                                  WHERE user_id=? AND result='ACEPTADO' ORDER BY id DESC LIMIT 1""",
                               (user["id"],)).fetchone()
            event_type = "SALIDA" if last and last["event_type"] == "ENTRADA" else "ENTRADA"
        if event_type != "SALIDA":
            companion = ""
            vehicle = ""
        con.execute("""INSERT INTO events(user_id,event_type,event_time,latitude,longitude,accuracy,result,companion,vehicle)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (user["id"], event_type, ts_now, lat, lon, accuracy, "ACEPTADO", companion or None, vehicle or None))
        con.commit()
    return jsonify(ok=True, event_type=event_type, message=f"{event_type} registrada correctamente.")


@app.route("/scan")
def scan():
    return render_template("scan.html")


@app.route("/api/lookup", methods=["POST"])
def lookup_user():
    if too_many(f"lookup:{client_ip()}", 120, 60):
        return jsonify(ok=False, message="Demasiados intentos. Espere un minuto."), 429
    data = json_body()
    cedula = str(data.get("cedula") or "").strip()
    if not cedula.isdigit() or len(cedula) != 10:
        return jsonify(ok=False, message="Ingrese una cédula de 10 dígitos."), 400
    with get_db() as con:
        user = con.execute("SELECT approved,active,public_key,full_name FROM users WHERE cedula=?", (cedula,)).fetchone()
    if not user:
        return jsonify(ok=True, next="register")
    if not user["active"]:
        return jsonify(ok=False, message="Usuario bloqueado. Contacte al administrador."), 403
    if not user["public_key"]:
        # Dispositivo revocado: debe vincular uno nuevo desde el formulario de inscripción.
        return jsonify(ok=True, next="register")
    if not user["approved"]:
        return jsonify(ok=False, message="Usuario pendiente de aprobación. Contacte al administrador."), 403
    return jsonify(ok=True, next="mark", name=mask_name(user["full_name"]))


@app.route("/mark")
def mark_page():
    return render_template("mark.html", cedula=request.args.get("cedula", ""))


@app.route("/health")
def health():
    return jsonify(status="ok", app="DINASED", database=os.path.exists(DB_PATH))


init_db()

if __name__ == "__main__":
    # Solo para pruebas. Para producción use un servidor WSGI (gunicorn/waitress) tras HTTPS.
    cert, key = os.environ.get("DINASED_CERT"), os.environ.get("DINASED_KEY")
    app.run(host="0.0.0.0", port=8080, debug=False,
            ssl_context=(cert, key) if cert and key else None)
