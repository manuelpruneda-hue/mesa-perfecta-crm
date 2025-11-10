from flask import (
    Flask, render_template, request, redirect,
    url_for, Response, flash, session, jsonify
)
import sqlite3, csv, shutil, smtplib, re
from io import StringIO
from datetime import datetime, timedelta
from email.mime.text import MIMEText
import os, pathlib
from functools import wraps
import json

# ============================================================
# 🔧 CONFIGURACIÓN BÁSICA
# ============================================================
app = Flask(__name__)

# Define la ruta absoluta de la base local
_default_db = pathlib.Path(__file__).with_name("mesa_perfecta.db")

# Usa variable de entorno si existe, o el sqlite local por defecto
app.config['SQLALCHEMY_DATABASE_URI'] = (
    os.environ.get('SQLALCHEMY_DATABASE_URI')
    or os.environ.get('DATABASE_URL')
    or f"sqlite:///{_default_db}"
)

# Evita warnings de SQLAlchemy
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Debug para verificar
print("🧠 DB URI en arranque ->", app.config['SQLALCHEMY_DATABASE_URI'])

app.secret_key = "mesa-perfecta-key"  # mueve a .env cuando quieras
app.config["TEMPLATES_AUTO_RELOAD"] = True

# DB
DATABASE = str(_default_db)

# Backups
BACKUP_DIR = os.getenv("CRM_BACKUP_DIR", r"C:\MesaPerfecta\Backups")

# Seguridad borrado + admin principal
DELETE_PASSWORD = os.getenv("CRM_DELETE_PASSWORD", "borra_con_cuidado")
SUPER_ADMIN_USERNAME = os.getenv("CRM_SUPER_ADMIN", "admin")

# Correo SMTP (Gmail con App Password)
SMTP_FROM = os.getenv("SMTP_FROM", "notificacionesmesaperfecta@gmail.com")
SMTP_USER = os.getenv("SMTP_USER", "notificacionesmesaperfecta@gmail.com")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "xovpktezbepzhrsl")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Negocio
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
MP_FEE_RATE = float(os.getenv("MP_FEE_RATE", "0.0319"))  # 3,19%
IVA_RATE = float(os.getenv("IVA_RATE", "0.19"))          # 19%

# Teléfonos (para WhatsApp después)
TEL_PERSONAL = os.getenv("TEL_PERSONAL", "+56961838833")
TEL_EMPRESA  = os.getenv("TEL_EMPRESA", "+56944618841")
TEL_LUIS     = os.getenv("TEL_LUIS", "+56979806247")

# --- Forzar codificación UTF-8 global (añadido el 10/11/2025) ---
app.config['JSON_AS_ASCII'] = False

@app.after_request
def add_charset(response):
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response
# --- Fin del bloque de codificación ---

# ============================================================
# 🔌 UTILS
# ============================================================
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def hash_pw(plain):
    import hashlib
    salt = "mesa-perfecta-salt"
    return hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()

def check_pw(plain, hashed):
    return hash_pw(plain) == hashed

def send_email(to_addr, subject, body):
    if not SMTP_APP_PASSWORD:
        app.logger.warning("SMTP_APP_PASSWORD vacío. No se envió correo.")
        return False, "App password vacío"
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            server.send_message(msg)
        return True, "OK"
    except Exception as e:
        app.logger.exception("Error enviando correo")
        return False, str(e)

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("🔐 Inicia sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def require_roles(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                flash("🔐 Inicia sesión.", "warning")
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("⛔ No tienes permiso para esta acción.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

def log_change(tabla, registro_id, accion, usuario, cambios_json):
    db = get_db()
    db.execute("""
        INSERT INTO cambios_log (tabla, registro_id, accion, usuario, cambios_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tabla, registro_id, accion, usuario, cambios_json, now_str()))
    db.commit()
    db.close()

# ============================================================
# 🧠 VARIABLES GLOBALES PARA LAS PLANTILLAS
# ============================================================
@app.context_processor
def inject_globals():
    return {
        "hoy": datetime.now().strftime("%Y-%m-%d"),
        "current_user": session.get("user"),
        "IVA_RATE": IVA_RATE,
        "MP_FEE_RATE": MP_FEE_RATE
    }

# ============================================================
# 🗄️ DB INIT / MIGRACIONES
# ============================================================
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # ---- tablas originales ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS ingresos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            cliente TEXT,
            medio_pago TEXT,
            tipo_doc TEXT,
            monto_neto REAL,
            iva_debito REAL,
            total REAL,
            categoria TEXT,
            observaciones TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS egresos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            proveedor TEXT,
            medio_pago TEXT,
            tipo_doc TEXT,
            monto_neto REAL,
            iva_credito REAL,
            total REAL,
            categoria TEXT,
            observaciones TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE,
            rut TEXT,
            giro TEXT,
            telefono TEXT,
            email TEXT,
            comuna TEXT,
            direccion TEXT,
            notas TEXT,
            activo INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS proveedores_master (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE,
            rut TEXT,
            giro TEXT,
            telefono TEXT,
            email TEXT,
            comuna TEXT,
            direccion TEXT,
            notas TEXT,
            activo INTEGER DEFAULT 1
        )
    ''')

    # ---- nuevas tablas ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            passhash TEXT,
            role TEXT CHECK(role IN ('admin','admin_limited','contador','operador')) NOT NULL,
            activo INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cambios_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tabla TEXT, registro_id INTEGER,
            accion TEXT, usuario TEXT,
            cambios_json TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cuentas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE,
            saldo_inicial REAL DEFAULT 0,
            activo INTEGER DEFAULT 1,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS movimientos_cuenta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cuenta_id INTEGER,
            tipo TEXT,
            monto REAL,
            glosa TEXT,
            ref_id INTEGER,
            fecha TEXT,
            created_at TEXT,
            FOREIGN KEY(cuenta_id) REFERENCES cuentas(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS transferencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cuenta_origen_id INTEGER,
            cuenta_destino_id INTEGER,
            monto REAL,
            glosa TEXT,
            fecha TEXT,
            created_at TEXT,
            FOREIGN KEY(cuenta_origen_id) REFERENCES cuentas(id),
            FOREIGN KEY(cuenta_destino_id) REFERENCES cuentas(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cierres_mensuales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anio INTEGER, mes INTEGER,
            cerrado_por TEXT,
            ventas_brutas REAL, iva_debito REAL,
            gastos_brutos REAL, iva_credito REAL,
            utilidad_neta REAL,
            created_at TEXT,
            UNIQUE(anio, mes)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS mp_pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            preference_id TEXT,
            payment_id TEXT,
            status TEXT,
            monto REAL,
            fee_mp REAL,
            neto_recibido REAL,
            cliente_id INTEGER,
            created_at TEXT, updated_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS notificaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, destinatario TEXT,
            plantilla TEXT, payload_json TEXT,
            estado TEXT, created_at TEXT, sent_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS agenda_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT, descripcion TEXT,
            fecha_inicio TEXT, fecha_fin TEXT,
            lugar TEXT, cliente_id INTEGER,
            recordatorio_minutos INTEGER,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT CHECK(tipo IN ('catering','snack','otros')),
            cliente_id INTEGER,
            fecha_evento TEXT, hora_evento TEXT, lugar TEXT,
            estado TEXT CHECK(estado IN ('cotizado','confirmado','entregado','cancelado')) DEFAULT 'cotizado',
            costo_estimado REAL, precio_venta REAL, ganancia_esperada REAL,
            observaciones TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pedido_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id INTEGER, descripcion TEXT,
            cantidad REAL, costo_unitario REAL,
            precio_unitario REAL, subtotal_costo REAL, subtotal_precio REAL,
            FOREIGN KEY(pedido_id) REFERENCES pedidos(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventario_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE,
            unidad TEXT,
            stock REAL DEFAULT 0,
            umbral_min REAL DEFAULT 0,
            costo_unitario REAL DEFAULT 0,
            categoria TEXT,
            usa_lotes INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventario_mov (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            tipo TEXT CHECK(tipo IN ('entrada','salida')),
            cantidad REAL, costo_unit REAL,
            lote TEXT, vence TEXT,
            motivo TEXT, ref TEXT,
            fecha TEXT, created_at TEXT,
            FOREIGN KEY(item_id) REFERENCES inventario_items(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS bom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto TEXT,
            item_id INTEGER,
            cantidad REAL,
            unidad TEXT,
            FOREIGN KEY(item_id) REFERENCES inventario_items(id)
        )
    ''')

    # ---- datos iniciales ----
    c.execute("SELECT COUNT(*) FROM usuarios")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                  (SUPER_ADMIN_USERNAME, hash_pw("admin123"), "admin"))
        c.execute("INSERT OR IGNORE INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                  ("luis", hash_pw("luis123"), "admin_limited"))

    c.execute("SELECT COUNT(*) FROM cuentas")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO cuentas (alias, saldo_inicial, activo, created_at) VALUES (?,?,1,?)",
                  ("BCI Pyme", 0, now_str()))
        c.execute("INSERT INTO cuentas (alias, saldo_inicial, activo, created_at) VALUES (?,?,1,?)",
                  ("Mercado Pago (Caja chica)", 0, now_str()))

    conn.commit()
    conn.close()

@app.before_request
def _before():
    init_db()

# ============================================================
# 👤 AUTENTICACIÓN Y ROLES
# ============================================================
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","")
        db = get_db()
        u = db.execute("SELECT * FROM usuarios WHERE username=? AND activo=1", (username,)).fetchone()
        db.close()
        if u and check_pw(password, u["passhash"]):
            session["user"] = {"id": u["id"], "username": u["username"], "role": u["role"]}
            flash(f"👋 Bienvenido, {u['username']}.", "success")
            return redirect(url_for("index"))
        flash("Credenciales inválidas o usuario inactivo.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("👋 Sesión cerrada.", "info")
    return redirect(url_for("login"))

# ============================================================
# 👥 GESTIÓN DE USUARIOS (solo admin)
# ============================================================
@app.route("/ajustes/usuarios")
@require_roles("admin")
def usuarios_list():
    db = get_db()
    rows = db.execute("SELECT * FROM usuarios ORDER BY username ASC").fetchall()
    db.close()
    return render_template("usuarios.html", usuarios=rows)

@app.route("/ajustes/usuarios/nuevo", methods=["GET","POST"])
@require_roles("admin")
def usuarios_nuevo():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        role = request.form["role"]
        password = request.form["password"]
        db = get_db()
        try:
            db.execute("INSERT INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                       (username, hash_pw(password), role))
            db.commit()
            flash("✅ Usuario creado.", "success")
        except sqlite3.IntegrityError:
            flash("❌ Usuario existente.", "danger")
        finally:
            db.close()
        return redirect(url_for("usuarios_list"))
    return render_template("usuarios_nuevo.html")

@app.route("/ajustes/usuarios/<int:uid>/reset", methods=["POST"])
@require_roles("admin")
def usuarios_reset(uid):
    newpass = request.form.get("newpass","123456")
    db = get_db()
    db.execute("UPDATE usuarios SET passhash=? WHERE id=?", (hash_pw(newpass), uid))
    db.commit()
    db.close()
    flash("🔑 Clave reiniciada.", "info")
    return redirect(url_for("usuarios_list"))

@app.route("/ajustes/usuarios/<int:uid>/toggle", methods=["POST"])
@require_roles("admin")
def usuarios_toggle(uid):
    db = get_db()
    u = db.execute("SELECT activo FROM usuarios WHERE id=?", (uid,)).fetchone()
    newv = 0 if u and u["activo"] else 1
    db.execute("UPDATE usuarios SET activo=? WHERE id=?", (newv, uid))
    db.commit()
    db.close()
    flash("🔁 Estado actualizado.", "success")
    return redirect(url_for("usuarios_list"))

# ============================================================
# 🧰 HELPERS (maestros)
# ============================================================
def ensure_cliente(nombre):
    if not nombre:
        return
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO clientes (nombre, activo) VALUES (?, 1)", (nombre.strip(),))
    conn.commit()
    conn.close()

def ensure_proveedor(nombre):
    if not nombre:
        return
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO proveedores_master (nombre, activo) VALUES (?, 1)", (nombre.strip(),))
    conn.commit()
    conn.close()

# ============================================================
# 🏠 DASHBOARD
# ============================================================
@app.route('/')
@require_login
def index():
    conn = get_db()
    cur = conn.cursor()

    cur.execute('SELECT COALESCE(SUM(total), 0) FROM ingresos')
    total_ingresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(total), 0) FROM egresos')
    total_egresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(iva_debito), 0) FROM ingresos')
    iva_ingresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(iva_credito), 0) FROM egresos')
    iva_egresos = cur.fetchone()[0] or 0.0

    cur.execute("SELECT COUNT(*) FROM mp_pagos WHERE status='pending'")
    pending_mp = cur.fetchone()[0] or 0

    cuentas = conn.execute("SELECT * FROM cuentas WHERE activo=1 ORDER BY alias ASC").fetchall()
    conn.close()

    neto = total_ingresos - total_egresos
    iva_neto = iva_ingresos - iva_egresos

    return render_template(
        'index.html',
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        neto=neto,
        iva_neto=iva_neto,
        cuentas=cuentas,
        pending_mp=pending_mp
    )

# ============================================================
# 💰 INGRESOS  (RUTA CORREGIDA — SIN DUPLICADOS)
# ============================================================
@app.route('/ingresos')
@require_login
def ingresos():
    try:
        q = (request.args.get('q') or '').strip().lower()
        desde = (request.args.get('desde') or '').strip()
        hasta = (request.args.get('hasta') or '').strip()

        base_where = "WHERE 1=1"
        params = []

        if q:
            like = f"%{q}%"
            base_where += """
                AND (
                    LOWER(COALESCE(cliente,'')) LIKE ?
                    OR LOWER(COALESCE(medio_pago,'')) LIKE ?
                    OR LOWER(COALESCE(tipo_doc,'')) LIKE ?
                    OR LOWER(COALESCE(categoria,'')) LIKE ?
                    OR LOWER(COALESCE(observaciones,'')) LIKE ?
                )
            """
            params += [like, like, like, like, like]

        if desde:
            base_where += " AND date(fecha) >= date(?)"
            params.append(desde)
        if hasta:
            base_where += " AND date(fecha) <= date(?)"
            params.append(hasta)

        db = get_db()
        ingresos_rows = db.execute(
            f"SELECT * FROM ingresos {base_where} ORDER BY date(fecha) DESC, id DESC",
            params
        ).fetchall()

        sums = db.execute(
            f"""
            SELECT 
                COALESCE(SUM(monto_neto), 0) AS sum_neto,
                COALESCE(SUM(iva_debito), 0) AS sum_iva,
                COALESCE(SUM(total), 0) AS sum_total
            FROM ingresos
            {base_where}
            """,
            params
        ).fetchone()
        db.close()

        sum_neto = sums["sum_neto"] if sums else 0
        sum_iva = sums["sum_iva"] if sums else 0
        sum_total = sums["sum_total"] if sums else 0

        return render_template(
            'ingresos.html',
            ingresos=ingresos_rows,
            sum_neto=sum_neto, sum_iva=sum_iva, sum_total=sum_total,
            q=q, desde=desde, hasta=hasta
        )
    except Exception as e:
        print("ERROR /ingresos:", repr(e))
        flash("Se nos cayó una olla. Intenta de nuevo.", "danger")
        return redirect(url_for('index'))

@app.route('/ingresos/nuevo', methods=['GET', 'POST'])
@require_login
def nuevo_ingreso():
    if request.method == 'POST':
        fecha = request.form.get('fecha', datetime.now().strftime("%Y-%m-%d"))
        cliente = request.form.get('cliente', '').strip()
        medio_pago = request.form.get('medio_pago')
        tipo_doc = request.form.get('tipo_doc')
        monto_neto = to_float(request.form.get('monto_neto'))
        iva_debito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_debito
        categoria = request.form.get('categoria', '')
        observaciones = request.form.get('observaciones', '')

        ensure_cliente(cliente)

        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO ingresos 
            (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        log_change(
            "ingresos",
            rid,
            "insert",
            session["user"]["username"],
            json.dumps({
                "cliente": cliente,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        return redirect(url_for('ingresos'))

    conn = get_db()
    clientes = conn.execute("SELECT * FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    conn.close()
    return render_template('nuevo_ingreso.html', clientes=clientes)

@app.route("/ingreso/<int:item_id>/editar", methods=["GET", "POST"])
@require_login
def editar_ingreso(item_id):
    db = get_db()
    if request.method == "POST":
        fecha = request.form.get("fecha")
        cliente = request.form.get("cliente", "").strip()
        medio_pago = request.form.get("medio_pago")
        tipo_doc = request.form.get("tipo_doc")
        monto_neto = to_float(request.form.get("monto_neto"))
        iva_debito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_debito
        categoria = request.form.get("categoria", "")
        observaciones = request.form.get("observaciones", "")

        ensure_cliente(cliente)

        db.execute("""
            UPDATE ingresos
            SET fecha=?, cliente=?, medio_pago=?, tipo_doc=?, monto_neto=?, iva_debito=?, total=?, categoria=?, observaciones=?
            WHERE id=?
        """, (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones, item_id))
        db.commit()
        db.close()
        log_change(
            "ingresos",
            item_id,
            "update",
            session["user"]["username"],
            json.dumps({
                "cliente": cliente,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        flash("✅ Ingreso actualizado correctamente.", "success")
        return redirect(url_for("ingresos"))

    ingreso = db.execute("SELECT * FROM ingresos WHERE id=?", (item_id,)).fetchone()
    clientes = db.execute("SELECT * FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    db.close()
    flash("✏️ Estás editando un ingreso. Guarda para aplicar los cambios.", "info")
    return render_template("editar_ingreso.html", i=ingreso, clientes=clientes)

@app.route("/ingreso/<int:item_id>/borrar", methods=["POST"])
@require_login
def borrar_ingreso(item_id):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password", "")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for("ingresos"))

    db = get_db()
    db.execute("DELETE FROM ingresos WHERE id=?", (item_id,))
    db.commit()
    db.close()
    log_change("ingresos", item_id, "delete", session["user"]["username"], "{}")
    flash("🗑️ Ingreso borrado correctamente.", "success")
    return redirect(url_for("ingresos"))

# ============================================================
# 💸 EGRESOS
# ============================================================
@app.route('/egresos')
@require_login
def egresos():
    q = (request.args.get('q') or '').strip().lower()
    desde = (request.args.get('desde') or '').strip()
    hasta = (request.args.get('hasta') or '').strip()

    base_where = 'WHERE 1=1'
    params = []

    if q:
        like = f'%{q}%'
        base_where += ''' AND (
            LOWER(COALESCE(proveedor,'')) LIKE ? OR
            LOWER(COALESCE(medio_pago,'')) LIKE ? OR
            LOWER(COALESCE(tipo_doc,'')) LIKE ? OR
            LOWER(COALESCE(categoria,'')) LIKE ? OR
            LOWER(COALESCE(observaciones,'')) LIKE ?
        )'''
        params += [like, like, like, like, like]

    if desde:
        base_where += ' AND date(fecha) >= date(?)'
        params.append(desde)
    if hasta:
        base_where += ' AND date(fecha) <= date(?)'
        params.append(hasta)

    sql = f'SELECT * FROM egresos {base_where} ORDER BY date(fecha) DESC, id DESC'

    conn = get_db()
    egresos_rows = conn.execute(sql, params).fetchall()
    proveedores = conn.execute(
        "SELECT nombre FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC"
    ).fetchall()
    # Totales
    sums = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(monto_neto), 0) AS sum_neto,
            COALESCE(SUM(iva_credito), 0) AS sum_iva,
            COALESCE(SUM(total), 0) AS sum_total
        FROM egresos
        {base_where}
        """,
        params
    ).fetchone()
    conn.close()

    sum_neto = sums["sum_neto"] if sums else 0
    sum_iva  = sums["sum_iva"] if sums else 0
    sum_total = sums["sum_total"] if sums else 0

    return render_template(
        'egresos.html',
        egresos=egresos_rows,
        sum_neto=sum_neto, sum_iva=sum_iva, sum_total=sum_total,
        proveedores=proveedores,
        q=q, desde=desde, hasta=hasta
    )

@app.route('/egresos/nuevo', methods=['GET', 'POST'])
@require_login
def nuevo_egreso():
    if request.method == 'POST':
        fecha = request.form.get('fecha', datetime.now().strftime("%Y-%m-%d"))
        proveedor = request.form.get('proveedor', '').strip()
        medio_pago = request.form.get('medio_pago')
        tipo_doc = request.form.get('tipo_doc')
        monto_neto = to_float(request.form.get('monto_neto'))
        iva_credito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_credito
        categoria = request.form.get('categoria', '')
        observaciones = request.form.get('observaciones', '')

        ensure_proveedor(proveedor)

        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO egresos 
            (fecha, proveedor, medio_pago, tipo_doc, monto_neto, iva_credito, total, categoria, observaciones)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (fecha, proveedor, medio_pago, tipo_doc, monto_neto, iva_credito, total, categoria, observaciones))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        log_change(
            "egresos",
            rid,
            "insert",
            session["user"]["username"],
            json.dumps({
                "proveedor": proveedor,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        return redirect(url_for('egresos'))
    conn = get_db()
    proveedores = conn.execute("SELECT * FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC").fetchall()
    conn.close()
    return render_template('nuevo_egreso.html', proveedores=proveedores)

@app.route("/egreso/<int:item_id>/editar", methods=["GET", "POST"])
@require_login
def editar_egreso(item_id):
    db = get_db()
    if request.method == "POST":
        fecha = request.form.get("fecha")
        proveedor = request.form.get("proveedor", "").strip()
        medio_pago = request.form.get("medio_pago")
        tipo_doc = request.form.get("tipo_doc")
        monto_neto = to_float(request.form.get("monto_neto"))
        iva_credito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_credito
        categoria = request.form.get("categoria", "")
        observaciones = request.form.get("observaciones", "")

        ensure_proveedor(proveedor)

        db.execute("""
            UPDATE egresos
            SET fecha=?, proveedor=?, medio_pago=?, tipo_doc=?,
                monto_neto=?, iva_credito=?, total=?, categoria=?, observaciones=?
            WHERE id=?
        """, (fecha, proveedor, medio_pago, tipo_doc,
              monto_neto, iva_credito, total, categoria, observaciones, item_id))
        db.commit()
        db.close()
        log_change(
            "egresos",
            item_id,
            "update",
            session["user"]["username"],
            json.dumps({
                "proveedor": proveedor,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        flash("✅ Egreso actualizado correctamente.", "success")
        return redirect(url_for("egresos"))

    e = db.execute("SELECT * FROM egresos WHERE id=?", (item_id,)).fetchone()
    proveedores = db.execute("SELECT * FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC").fetchall()
    db.close()
    if not e:
        flash("❌ Egreso no encontrado.", "danger")
        return redirect(url_for("egresos"))

    flash("✏️ Estás editando un egreso.", "info")
    return render_template("editar_egreso.html", e=e, proveedores=proveedores)

@app.route("/egreso/<int:item_id>/borrar", methods=["POST"])
@require_login
def borrar_egreso(item_id):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password", "")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for("egresos"))

    db = get_db()
    db.execute("DELETE FROM egresos WHERE id=?", (item_id,))
    db.commit()
    db.close()
    log_change("egresos", item_id, "delete", session["user"]["username"], "{}")
    flash("🗑️ Egreso borrado correctamente.", "success")
    return redirect(url_for("egresos"))

# ============================================================
# 📈 CRM / PROVEEDORES
# ============================================================
@app.route('/crm')
@require_login
def crm():
    conn = get_db()
    rows = conn.execute('''
        SELECT cliente, COUNT(*) AS transacciones, SUM(total) AS total_vendido
        FROM ingresos
        WHERE cliente IS NOT NULL AND cliente != ''
        GROUP BY cliente
        ORDER BY total_vendido DESC
    ''').fetchall()
    conn.close()
    return render_template('crm.html', clientes=rows)

@app.route('/proveedores')
@require_login
def proveedores_resumen():
    conn = get_db()
    rows = conn.execute('''
        SELECT proveedor, COUNT(*) AS transacciones, SUM(total) AS total_gastado
        FROM egresos
        WHERE proveedor IS NOT NULL AND TRIM(proveedor) != ''
        GROUP BY proveedor
        ORDER BY total_gastado DESC
    ''').fetchall()
    conn.close()
    return render_template('proveedores.html', proveedores=rows)

# ============================================================
# 🧑‍🤝‍🧑 CLIENTES / PROVEEDORES (maestros)
# ============================================================
@app.route('/clientes')
@require_login
def clientes_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM clientes ORDER BY activo DESC, nombre ASC").fetchall()
    conn.close()
    return render_template('clientes.html', clientes=rows)

@app.route('/clientes/nuevo', methods=['GET', 'POST'])
@require_login
def clientes_nuevo():
    if request.method == 'POST':
        nombre = request.form['nombre'].strip()
        rut = request.form.get('rut', '').strip()
        giro = request.form.get('giro', '').strip()
        telefono = request.form.get('telefono', '').strip()
        email = request.form.get('email', '').strip()
        comuna = request.form.get('comuna', '').strip()
        direccion = request.form.get('direccion', '').strip()
        notas = request.form.get('notas', '').strip()
        if not nombre:
            flash("El nombre es obligatorio.", "warning")
            return redirect(url_for('clientes_nuevo'))
        conn = get_db()
        conn.execute('''
            INSERT OR IGNORE INTO clientes (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas))
        conn.commit()
        conn.close()
        log_change("clientes", 0, "insert", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Cliente guardado.", "success")
        return redirect(url_for('clientes_list'))
    return render_template('nuevo_cliente.html')

@app.route('/clientes/<int:cid>/editar', methods=['GET','POST'])
@require_login
def clientes_editar(cid):
    conn = get_db()
    if request.method == 'POST':
        nombre = request.form['nombre'].strip()
        rut = request.form.get('rut','').strip()
        giro = request.form.get('giro','').strip()
        telefono = request.form.get('telefono','').strip()
        email = request.form.get('email','').strip()
        comuna = request.form.get('comuna','').strip()
        direccion = request.form.get('direccion','').strip()
        notas = request.form.get('notas','').strip()
        activo = 1 if request.form.get('activo') == 'on' else 0
        conn.execute('''
            UPDATE clientes SET nombre=?, rut=?, giro=?, telefono=?, email=?, comuna=?, direccion=?, notas=?, activo=?
            WHERE id=?
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo, cid))
        conn.commit()
        conn.close()
        log_change("clientes", cid, "update", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Cliente actualizado.", "success")
        return redirect(url_for('clientes_list'))
    row = conn.execute("SELECT * FROM clientes WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        flash("Cliente no existe.", "warning")
        return redirect(url_for('clientes_list'))
    return render_template('editar_cliente.html', c=row)

@app.route('/clientes/<int:cid>/borrar', methods=['POST'])
@require_login
def clientes_borrar(cid):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password","")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for('clientes_list'))
    conn = get_db()
    conn.execute("DELETE FROM clientes WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    log_change("clientes", cid, "delete", session["user"]["username"], "{}")
    flash("🗑️ Cliente borrado.", "success")
    return redirect(url_for('clientes_list'))

@app.route('/maestro/proveedores')
@require_login
def proveedores_maestro_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM proveedores_master ORDER BY activo DESC, nombre ASC").fetchall()
    conn.close()
    return render_template('proveedores_maestro.html', proveedores=rows)

@app.route('/maestro/proveedores/nuevo', methods=['GET','POST'])
@require_login
def proveedores_maestro_nuevo():
    if request.method == 'POST':
        nombre = request.form['nombre'].strip()
        rut = request.form.get('rut','').strip()
        giro = request.form.get('giro','').strip()
        telefono = request.form.get('telefono','').strip()
        email = request.form.get('email','').strip()
        comuna = request.form.get('comuna','').strip()
        direccion = request.form.get('direccion','').strip()
        notas = request.form.get('notas','').strip()
        if not nombre:
            flash("El nombre es obligatorio.", "warning")
            return redirect(url_for('proveedores_maestro_nuevo'))
        conn = get_db()
        conn.execute('''
            INSERT OR IGNORE INTO proveedores_master (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas))
        conn.commit()
        conn.close()
        log_change("proveedores_master", 0, "insert", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Proveedor guardado.", "success")
        return redirect(url_for('proveedores_maestro_list'))
    return render_template('nuevo_proveedor.html')

# ======================================================================
#  MESA PERFECTA CRM - app.py (Versión Corregida y Optimizada)
# ======================================================================
#
#  Este archivo ha sido reestructurado para cumplir con los siguientes
#  requisitos:
#  1. (Fix #4) Manejo de DB con 'g' para evitar 'database is locked'.
#  2. (Fix #1, #5) Uso de .get() y helpers para evitar errores 500.
#  3. (Fix #11) Configuración de logging a /logs/error.log.
#  4. (Fix #1, #10) Manejadores de error 404/500/Exception robustos.
#  5. (Fix #12) init_db() se ejecuta una vez al inicio, no en cada request.
#  6. (Fix #6) CRUD de Inventario completado (Editar/Borrar).
#  7. (Fix #7, #8) Funciones de Correo y WhatsApp robustecidas.
#  8. (Fix #2) Rutas faltantes (/usuarios, /transferir) añadidas.
#  9. (Fix Lógica) Corregido bug de totales en /egresos.
#
# ======================================================================

from flask import (
    Flask, render_template, request, redirect,
    url_for, Response, flash, session, jsonify, g
)
import sqlite3
import csv
import shutil
import smtplib
import re
import json
import os
import pathlib
import traceback  # Para logging de errores
import logging    # Para logging de errores
from logging.handlers import RotatingFileHandler
from io import StringIO
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from functools import wraps

# ============================================================
# 🔧 CONFIGURACIÓN BÁSICA Y LOGGING
# ============================================================
app = Flask(__name__)

# --- Configuración de Logging (Fix #11) ---
# Asegura que la carpeta /logs exista
LOG_DIR = 'logs'
os.makedirs(LOG_DIR, exist_ok=True)

# Configura el logger para rotar archivos (max 1MB, 3 backups)
log_file = os.path.join(LOG_DIR, 'error.log')
handler = RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=3)
handler.setLevel(logging.WARNING) # Captura WARNING, ERROR, CRITICAL
formatter = logging.Formatter(
    '[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
app.logger.addHandler(handler) # Lo adjuntamos al logger de Flask
app.logger.setLevel(logging.WARNING)
# --- Fin de Logging ---

# Define la ruta absoluta de la base local
_default_db = pathlib.Path(__file__).with_name("mesa_perfecta.db")
DATABASE = str(_default_db)

# Usa variable de entorno si existe, o el sqlite local por defecto
app.config['SQLALCHEMY_DATABASE_URI'] = (
    os.environ.get('SQLALCHEMY_DATABASE_URI')
    or os.environ.get('DATABASE_URL')
    or f"sqlite:///{_default_db}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

print("🧠 DB URI en arranque ->", app.config['SQLALCHEMY_DATABASE_URI'])

app.secret_key = os.getenv("FLASK_SECRET_KEY", "mesa-perfecta-key-muy-segura")
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Backups
BACKUP_DIR = os.getenv("CRM_BACKUP_DIR", r"C:\MesaPerfecta\Backups")
ensure_dir(BACKUP_DIR) # Aseguramos que exista al inicio

# Seguridad borrado + admin principal
DELETE_PASSWORD = os.getenv("CRM_DELETE_PASSWORD", "borra_con_cuidado")
SUPER_ADMIN_USERNAME = os.getenv("CRM_SUPER_ADMIN", "admin")

# Correo SMTP (Gmail con App Password)
SMTP_FROM = os.getenv("SMTP_FROM", "notificacionesmesaperfecta@gmail.com")
SMTP_USER = os.getenv("SMTP_USER", "notificacionesmesaperfecta@gmail.com")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "xovpktezbepzhrsl") # Contraseña de app de Gmail
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Negocio
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
MP_FEE_RATE = float(os.getenv("MP_FEE_RATE", "0.0319"))  # 3,19%
IVA_RATE = float(os.getenv("IVA_RATE", "0.19"))        # 19%

# Teléfonos (para WhatsApp después)
TEL_PERSONAL = os.getenv("TEL_PERSONAL", "+56961838833")
TEL_EMPRESA  = os.getenv("TEL_EMPRESA", "+56944618841")
TEL_LUIS     = os.getenv("TEL_LUIS", "+56979806247")

# --- Forzar codificación UTF-8 global (Fix #3) ---
app.config['JSON_AS_ASCII'] = False

@app.after_request
def add_charset(response):
    if 'charset' not in response.headers.get('Content-Type', '').lower():
        response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response
# --- Fin del bloque de codificación ---


# ============================================================
# 🔌 UTILS Y HELPERS
# ============================================================

def get_db():
    """
    (Fix #4) Manejador de conexión de base de datos.
    Usa el 'g' de Flask para asegurar una única conexión por request.
    """
    db = getattr(g, '_database', None)
    if db is None:
        try:
            db = g._database = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            app.logger.error(f"Error al conectar con la DB: {e}")
            raise e
    return db

@app.teardown_appcontext
def close_connection(exception):
    """(Fix #4) Cierra la conexión a la DB al finalizar el request."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def now_str():
    """Devuelve la fecha/hora actual como string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_dir(path):
    """Asegura que un directorio exista."""
    os.makedirs(path, exist_ok=True)

def to_float(value, default=0.0):
    """
    (Fix #1, #5) Helper para convertir inputs a float de forma segura.
    """
    if value is None:
        return default
    try:
        # Reemplazar comas por puntos para inputs localizados
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return default

def hash_pw(plain):
    import hashlib
    salt = "mesa-perfecta-salt" # Debería estar en .env
    return hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()

def check_pw(plain, hashed):
    return hash_pw(plain) == hashed

def send_email(to_addr, subject, body):
    """
    (Fix #7) Envía un correo usando SMTP con manejo de errores.
    """
    # Si la contraseña es la de ejemplo, no intentar.
    if not SMTP_APP_PASSWORD or SMTP_APP_PASSWORD == "xovpktezbepzhrsl":
        app.logger.warning("SMTP_APP_PASSWORD es la de ejemplo o está vacía. No se envió correo.")
        return False, "App password no configurada"
    
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        
        app.logger.info(f"Enviando correo a {to_addr}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            server.send_message(msg)
        app.logger.info("Correo enviado exitosamente.")
        return True, "OK"
    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(f"Error de autenticación SMTP: {e}. Revisa SMTP_APP_PASSWORD.")
        return False, f"Error de autenticación: {e}"
    except Exception as e:
        app.logger.exception("Error genérico enviando correo")
        return False, str(e)

def send_whatsapp_placeholder(to, message):
    """
    (Fix #8) Placeholder para la API de WhatsApp.
    No rompe la app, solo loguea el intento.
    """
    try:
        # Aquí iría la lógica de Twilio o WhatsApp Cloud API
        app.logger.info(f"--- SIMULACIÓN DE WHATSAPP ---")
        app.logger.info(f"Destinatario: {to}")
        app.logger.info(f"Mensaje: {message}")
        app.logger.info(f"--- FIN SIMULACIÓN ---")
        return True, "Logueado (placeholder)"
    except Exception as e:
        app.logger.error(f"Error en placeholder de WhatsApp: {e}")
        return False, str(e)

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("🔐 Inicia sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def require_roles(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                flash("🔐 Inicia sesión.", "warning")
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("⛔ No tienes permiso para esta acción.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

def log_change(tabla, registro_id, accion, usuario, cambios_json):
    """
    Registra un cambio en la tabla 'cambios_log'.
    (Fix #4) Ya no abre ni cierra su propia conexión.
    """
    try:
        db = get_db() # Usa la conexión global del request
        db.execute("""
            INSERT INTO cambios_log (tabla, registro_id, accion, usuario, cambios_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (tabla, registro_id, accion, usuario, cambios_json, now_str()))
        db.commit()
    except Exception as e:
        app.logger.error(f"Error al guardar en cambios_log: {e}")
        # No relanzar, el log no debe detener la operación principal


# ============================================================
# 🧠 VARIABLES GLOBALES PARA LAS PLANTILLAS
# ============================================================
@app.context_processor
def inject_globals():
    return {
        "hoy": datetime.now().strftime("%Y-%m-%d"),
        "current_user": session.get("user"),
        "IVA_RATE": IVA_RATE,
        "MP_FEE_RATE": MP_FEE_RATE
    }

# ============================================================
# 🗄️ DB INIT / MIGRACIONES
# ============================================================
def init_db():
    """
    (Fix #12) Inicializa la base de datos con todas las tablas.
    Se ejecuta una vez al inicio o manualmente vía /init.
    """
    app.logger.warning("Ejecutando init_db()...")
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        # ---- tablas originales ----
        c.execute('''
            CREATE TABLE IF NOT EXISTS ingresos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, cliente TEXT, medio_pago TEXT, tipo_doc TEXT,
                monto_neto REAL, iva_debito REAL, total REAL,
                categoria TEXT, observaciones TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS egresos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, proveedor TEXT, medio_pago TEXT, tipo_doc TEXT,
                monto_neto REAL, iva_credito REAL, total REAL,
                categoria TEXT, observaciones TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE, rut TEXT, giro TEXT, telefono TEXT, email TEXT,
                comuna TEXT, direccion TEXT, notas TEXT, activo INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS proveedores_master (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE, rut TEXT, giro TEXT, telefono TEXT, email TEXT,
                comuna TEXT, direccion TEXT, notas TEXT, activo INTEGER DEFAULT 1
            )
        ''')

        # ---- nuevas tablas ----
        c.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE, passhash TEXT,
                role TEXT CHECK(role IN ('admin','admin_limited','contador','operador')) NOT NULL,
                activo INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS cambios_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tabla TEXT, registro_id INTEGER,
                accion TEXT, usuario TEXT,
                cambios_json TEXT, created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS cuentas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT UNIQUE, saldo_inicial REAL DEFAULT 0,
                activo INTEGER DEFAULT 1, created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS movimientos_cuenta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cuenta_id INTEGER, tipo TEXT, monto REAL, glosa TEXT,
                ref_id INTEGER, fecha TEXT, created_at TEXT,
                FOREIGN KEY(cuenta_id) REFERENCES cuentas(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS transferencias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cuenta_origen_id INTEGER, cuenta_destino_id INTEGER,
                monto REAL, glosa TEXT, fecha TEXT, created_at TEXT,
                FOREIGN KEY(cuenta_origen_id) REFERENCES cuentas(id),
                FOREIGN KEY(cuenta_destino_id) REFERENCES cuentas(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS cierres_mensuales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anio INTEGER, mes INTEGER, cerrado_por TEXT,
                ventas_brutas REAL, iva_debito REAL,
                gastos_brutos REAL, iva_credito REAL,
                utilidad_neta REAL, created_at TEXT,
                UNIQUE(anio, mes)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS mp_pagos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preference_id TEXT, payment_id TEXT, status TEXT, monto REAL,
                fee_mp REAL, neto_recibido REAL, cliente_id INTEGER,
                created_at TEXT, updated_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS notificaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, destinatario TEXT, plantilla TEXT, payload_json TEXT,
                estado TEXT, created_at TEXT, sent_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS agenda_eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT, descripcion TEXT, fecha_inicio TEXT, fecha_fin TEXT,
                lugar TEXT, cliente_id INTEGER, recordatorio_minutos INTEGER,
                created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT CHECK(tipo IN ('catering','snack','otros')),
                cliente_id INTEGER, fecha_evento TEXT, hora_evento TEXT, lugar TEXT,
                estado TEXT CHECK(estado IN ('cotizado','confirmado','entregado','cancelado')) DEFAULT 'cotizado',
                costo_estimado REAL, precio_venta REAL, ganancia_esperada REAL,
                observaciones TEXT, created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS pedido_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER, descripcion TEXT, cantidad REAL, costo_unitario REAL,
                precio_unitario REAL, subtotal_costo REAL, subtotal_precio REAL,
                FOREIGN KEY(pedido_id) REFERENCES pedidos(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS inventario_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE, unidad TEXT, stock REAL DEFAULT 0,
                umbral_min REAL DEFAULT 0, costo_unitario REAL DEFAULT 0,
                categoria TEXT, usa_lotes INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS inventario_mov (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER, tipo TEXT CHECK(tipo IN ('entrada','salida')),
                cantidad REAL, costo_unit REAL, lote TEXT, vence TEXT,
                motivo TEXT, ref TEXT, fecha TEXT, created_at TEXT,
                FOREIGN KEY(item_id) REFERENCES inventario_items(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bom (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                producto TEXT, item_id INTEGER,
                cantidad REAL, unidad TEXT,
                FOREIGN KEY(item_id) REFERENCES inventario_items(id)
            )
        ''')

        # ---- datos iniciales ----
        c.execute("SELECT COUNT(*) FROM usuarios")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                      (SUPER_ADMIN_USERNAME, hash_pw("admin123"), "admin"))
            c.execute("INSERT OR IGNORE INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                      ("luis", hash_pw("luis123"), "admin_limited"))
            app.logger.info("Usuarios admin creados.")

        c.execute("SELECT COUNT(*) FROM cuentas")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO cuentas (alias, saldo_inicial, activo, created_at) VALUES (?,?,1,?)",
                      ("BCI Pyme", 0, now_str()))
            c.execute("INSERT INTO cuentas (alias, saldo_inicial, activo, created_at) VALUES (?,?,1,?)",
                      ("Mercado Pago (Caja chica)", 0, now_str()))
            app.logger.info("Cuentas iniciales creadas.")

        conn.commit()
        conn.close()
        app.logger.info("init_db() completado exitosamente.")
    except Exception as e:
        app.logger.critical(f"FALLO CRÍTICO AL INICIAR LA DB: {e}")
        print(f"FALLO CRÍTICO AL INICIAR LA DB: {e}")

# (Fix #12) Eliminado @app.before_request que llamaba a init_db().
# Se ejecuta una vez al inicio, antes de app.run() (ver final del archivo).

@app.route("/init")
@require_roles("admin")
def manual_init():
    """(Fix #12) Ruta manual para (re)inicializar la DB."""
    init_db()
    flash("Base de datos inicializada manualmente.", "info")
    return redirect(url_for('index'))


# ============================================================
# ⚠️ MANEJO DE ERRORES GLOBAL (Fix #1, #10, #11)
# ============================================================

@app.errorhandler(404)
def error_404(e):
    """Manejador para errores 404."""
    app.logger.warning(f"Ruta no encontrada (404): {request.path}")
    flash("🌶️ La ruta que buscas no existe. Te hemos redirigido al inicio.", "warning")
    return redirect(url_for('index'))

@app.errorhandler(500)
def error_500(e):
    """Manejador para errores 500 (internos)."""
    error_trace = traceback.format_exc()
    app.logger.error(f"Error 500 (Interno) en {request.path}:\n{error_trace}")
    # Renderizar una plantilla de error amigable
    return render_template("500.html", error_message=str(e)), 500

@app.errorhandler(Exception)
def handle_all_errors(e):
    """Manejador para todas las demás excepciones no controladas."""
    # Evitar capturar errores 404 o 500 que ya tienen manejador
    error_code = getattr(e, 'code', 500)
    
    if error_code == 404:
         return error_404(e) # Re-dirigir a nuestro 404
    
    # Para todos los demás errores, tratar como 500
    error_trace = traceback.format_exc()
    app.logger.error(f"Error {error_code} (No controlado) en {request.path}:\n{error_trace}")
    
    # Mostrar el mensaje "olla caída" solo en el template 500.html
    # El flash es redundante si ya mostramos la página 500.
    # flash("Se nos cayó una olla. Intenta de nuevo.", "danger")
    
    return render_template("500.html", error_message=str(e)), 500


# ============================================================
# 👤 AUTENTICACIÓN Y ROLES
# ============================================================
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","")
        db = get_db()
        u = db.execute("SELECT * FROM usuarios WHERE username=? AND activo=1", (username,)).fetchone()
        # (Fix #4) No se usa db.close() aquí
        if u and check_pw(password, u["passhash"]):
            session["user"] = {"id": u["id"], "username": u["username"], "role": u["role"]}
            flash(f"👋 Bienvenido, {u['username']}.", "success")
            return redirect(url_for("index"))
        flash("Credenciales inválidas o usuario inactivo.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("👋 Sesión cerrada.", "info")
    return redirect(url_for("login"))

# ============================================================
# 👥 GESTIÓN DE USUARIOS (solo admin)
# ============================================================

@app.route("/usuarios") # (Fix #2) Ruta alias
@require_roles("admin")
def usuarios_redirect():
    return redirect(url_for('usuarios_list'))

@app.route("/ajustes/usuarios")
@require_roles("admin")
def usuarios_list():
    db = get_db()
    rows = db.execute("SELECT * FROM usuarios ORDER BY username ASC").fetchall()
    return render_template("usuarios.html", usuarios=rows)

@app.route("/ajustes/usuarios/nuevo", methods=["GET","POST"])
@require_roles("admin")
def usuarios_nuevo():
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() para evitar KeyErrors
        username = request.form.get("username", "").strip().lower()
        role = request.form.get("role")
        password = request.form.get("password")

        if not username or not role or not password:
             flash("❌ Faltan datos (usuario, rol, contraseña).", "danger")
             return redirect(url_for("usuarios_nuevo"))

        db = get_db()
        try:
            db.execute("INSERT INTO usuarios (username, passhash, role, activo) VALUES (?,?,?,1)",
                       (username, hash_pw(password), role))
            db.commit()
            flash("✅ Usuario creado.", "success")
        except sqlite3.IntegrityError:
            flash("❌ El nombre de usuario ya existe.", "danger")
        # (Fix #4) No se usa db.close() en finally
        return redirect(url_for("usuarios_list"))
    return render_template("usuarios_nuevo.html")

@app.route("/ajustes/usuarios/<int:uid>/reset", methods=["POST"])
@require_roles("admin")
def usuarios_reset(uid):
    newpass = request.form.get("newpass","123456")
    db = get_db()
    db.execute("UPDATE usuarios SET passhash=? WHERE id=?", (hash_pw(newpass), uid))
    db.commit()
    flash("🔑 Clave reiniciada.", "info")
    return redirect(url_for("usuarios_list"))

@app.route("/ajustes/usuarios/<int:uid>/toggle", methods=["POST"])
@require_roles("admin")
def usuarios_toggle(uid):
    db = get_db()
    u = db.execute("SELECT activo FROM usuarios WHERE id=?", (uid,)).fetchone()
    newv = 0 if u and u["activo"] else 1
    db.execute("UPDATE usuarios SET activo=? WHERE id=?", (newv, uid))
    db.commit()
    flash("🔁 Estado actualizado.", "success")
    return redirect(url_for("usuarios_list"))

# ============================================================
# 🧰 HELPERS (maestros)
# ============================================================
def ensure_cliente(nombre):
    if not nombre:
        return
    conn = get_db() # (Fix #4) Usa conexión 'g'
    conn.execute("INSERT OR IGNORE INTO clientes (nombre, activo) VALUES (?, 1)", (nombre.strip(),))
    conn.commit()
    # (Fix #4) No se usa conn.close()

def ensure_proveedor(nombre):
    if not nombre:
        return
    conn = get_db() # (Fix #4) Usa conexión 'g'
    conn.execute("INSERT OR IGNORE INTO proveedores_master (nombre, activo) VALUES (?, 1)", (nombre.strip(),))
    conn.commit()
    # (Fix #4) No se usa conn.close()

# ============================================================
# 🏠 DASHBOARD
# ============================================================
@app.route('/')
@require_login
def index():
    conn = get_db()
    cur = conn.cursor() # Usar cursor para fetchone()

    cur.execute('SELECT COALESCE(SUM(total), 0) FROM ingresos')
    total_ingresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(total), 0) FROM egresos')
    total_egresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(iva_debito), 0) FROM ingresos')
    iva_ingresos = cur.fetchone()[0] or 0.0

    cur.execute('SELECT COALESCE(SUM(iva_credito), 0) FROM egresos')
    iva_egresos = cur.fetchone()[0] or 0.0

    cur.execute("SELECT COUNT(*) FROM mp_pagos WHERE status='pending'")
    pending_mp = cur.fetchone()[0] or 0

    cuentas = conn.execute("SELECT * FROM cuentas WHERE activo=1 ORDER BY alias ASC").fetchall()
    # (Fix #4) No se usa conn.close()

    neto = total_ingresos - total_egresos
    iva_neto = iva_ingresos - iva_egresos

    return render_template(
        'index.html',
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        neto=neto,
        iva_neto=iva_neto,
        cuentas=cuentas,
        pending_mp=pending_mp
    )

# ============================================================
# 💰 INGRESOS
# ============================================================
@app.route('/ingresos')
@require_login
def ingresos():
    # (Fix #1) El try/except global manejará los errores
    q = (request.args.get('q') or '').strip().lower()
    desde = (request.args.get('desde') or '').strip()
    hasta = (request.args.get('hasta') or '').strip()

    base_where = "WHERE 1=1"
    params = []

    if q:
        like = f"%{q}%"
        base_where += """
            AND (
                LOWER(COALESCE(cliente,'')) LIKE ?
                OR LOWER(COALESCE(medio_pago,'')) LIKE ?
                OR LOWER(COALESCE(tipo_doc,'')) LIKE ?
                OR LOWER(COALESCE(categoria,'')) LIKE ?
                OR LOWER(COALESCE(observaciones,'')) LIKE ?
            )
        """
        params += [like, like, like, like, like]

    if desde:
        base_where += " AND date(fecha) >= date(?)"
        params.append(desde)
    if hasta:
        base_where += " AND date(fecha) <= date(?)"
        params.append(hasta)

    db = get_db()
    ingresos_rows = db.execute(
        f"SELECT * FROM ingresos {base_where} ORDER BY date(fecha) DESC, id DESC",
        params
    ).fetchall()

    sums = db.execute(
        f"""
        SELECT 
            COALESCE(SUM(monto_neto), 0) AS sum_neto,
            COALESCE(SUM(iva_debito), 0) AS sum_iva,
            COALESCE(SUM(total), 0) AS sum_total
        FROM ingresos
        {base_where}
        """,
        params
    ).fetchone()
    # (Fix #4) No se usa db.close()

    sum_neto = sums["sum_neto"] if sums else 0
    sum_iva = sums["sum_iva"] if sums else 0
    sum_total = sums["sum_total"] if sums else 0

    return render_template(
        'ingresos.html',
        ingresos=ingresos_rows,
        sum_neto=sum_neto, sum_iva=sum_iva, sum_total=sum_total,
        q=q, desde=desde, hasta=hasta
    )

@app.route('/ingresos/nuevo', methods=['GET', 'POST'])
@require_login
def nuevo_ingreso():
    if request.method == 'POST':
        # (Fix #1, #5) Usar .get() y helper to_float()
        fecha = request.form.get('fecha', datetime.now().strftime("%Y-%m-%d"))
        cliente = request.form.get('cliente', '').strip()
        medio_pago = request.form.get('medio_pago')
        tipo_doc = request.form.get('tipo_doc')
        monto_neto = to_float(request.form.get('monto_neto')) # Fix
        categoria = request.form.get('categoria', '') # Fix
        observaciones = request.form.get('observaciones', '') # Fix

        iva_debito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_debito

        ensure_cliente(cliente) # Usa la conexión 'g'

        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO ingresos 
            (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones))
        conn.commit()
        rid = cur.lastrowid
        # (Fix #4) No se usa conn.close()
        
        log_change(
            "ingresos",
            rid,
            "insert",
            session["user"]["username"],
            json.dumps({
                "cliente": cliente,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        
        # (Fix #9) Redirección ya era correcta
        return redirect(url_for('ingresos'))

    conn = get_db()
    clientes = conn.execute("SELECT * FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('nuevo_ingreso.html', clientes=clientes)

@app.route("/ingreso/<int:item_id>/editar", methods=["GET", "POST"])
@require_login
def editar_ingreso(item_id):
    db = get_db()
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        fecha = request.form.get("fecha")
        cliente = request.form.get("cliente", "").strip()
        medio_pago = request.form.get("medio_pago")
        tipo_doc = request.form.get("tipo_doc")
        monto_neto = to_float(request.form.get("monto_neto")) # Fix
        categoria = request.form.get("categoria", "") # Fix
        observaciones = request.form.get("observaciones", "") # Fix

        iva_debito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_debito

        ensure_cliente(cliente) # Usa la conexión 'g'

        db.execute("""
            UPDATE ingresos 
            SET fecha=?, cliente=?, medio_pago=?, tipo_doc=?, monto_neto=?, iva_debito=?, total=?, categoria=?, observaciones=?
            WHERE id=?
        """, (fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones, item_id))
        db.commit()
        # (Fix #4) No se usa db.close()
        
        log_change(
            "ingresos",
            item_id,
            "update",
            session["user"]["username"],
            json.dumps({
                "cliente": cliente,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        flash("✅ Ingreso actualizado correctamente.", "success")
        
        # (Fix #9) Redirección ya era correcta
        return redirect(url_for("ingresos"))

    ingreso = db.execute("SELECT * FROM ingresos WHERE id=?", (item_id,)).fetchone()
    clientes = db.execute("SELECT * FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa db.close()
    
    if not ingreso:
        flash("❌ El ingreso que intentas editar no existe.", "danger")
        return redirect(url_for("ingresos"))
        
    flash("✏️ Estás editando un ingreso. Guarda para aplicar los cambios.", "info")
    return render_template("editar_ingreso.html", i=ingreso, clientes=clientes)

@app.route("/ingreso/<int:item_id>/borrar", methods=["POST"])
@require_login
def borrar_ingreso(item_id):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password", "")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for("ingresos"))

    db = get_db()
    db.execute("DELETE FROM ingresos WHERE id=?", (item_id,))
    db.commit()
    # (Fix #4) No se usa db.close()
    
    log_change("ingresos", item_id, "delete", session["user"]["username"], "{}")
    flash("🗑️ Ingreso borrado correctamente.", "success")
    return redirect(url_for("ingresos"))

# ============================================================
# 💸 EGRESOS
# ============================================================
@app.route('/egresos')
@require_login
def egresos():
    q = (request.args.get('q') or '').strip().lower()
    desde = (request.args.get('desde') or '').strip()
    hasta = (request.args.get('hasta') or '').strip()

    # (Fix Lógica) Usar el mismo 'base_where' y 'params' para ambas consultas
    base_where = 'WHERE 1=1'
    params = []

    if q:
        like = f'%{q}%'
        base_where += ''' AND (
            LOWER(COALESCE(proveedor,'')) LIKE ? OR
            LOWER(COALESCE(medio_pago,'')) LIKE ? OR
            LOWER(COALESCE(tipo_doc,'')) LIKE ? OR
            LOWER(COALESCE(categoria,'')) LIKE ? OR
            LOWER(COALESCE(observaciones,'')) LIKE ?
        )'''
        params += [like, like, like, like, like]

    if desde:
        base_where += ' AND date(fecha) >= date(?)'
        params.append(desde)
    if hasta:
        base_where += ' AND date(fecha) <= date(?)'
        params.append(hasta)

    sql_orden = ' ORDER BY date(fecha) DESC, id DESC'
    sql_base = f'SELECT * FROM egresos {base_where} {sql_orden}'
    
    conn = get_db()
    egresos_rows = conn.execute(sql_base, params).fetchall()
    
    proveedores = conn.execute(
        "SELECT nombre FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC"
    ).fetchall()

    # (Fix Lógica) Bug corregido. Esta consulta ahora usa 'base_where' y 'params'.
    sql_sums = f"""
        SELECT 
            COALESCE(SUM(monto_neto), 0) AS sum_neto,
            COALESCE(SUM(iva_credito), 0) AS sum_iva,
            COALESCE(SUM(total), 0) AS sum_total
        FROM egresos
        {base_where}
    """
    sums = conn.execute(sql_sums, params).fetchone()
    # (Fix #4) No se usa conn.close()

    sum_neto = sums["sum_neto"] if sums else 0
    sum_iva  = sums["sum_iva"] if sums else 0
    sum_total = sums["sum_total"] if sums else 0

    return render_template(
        'egresos.html',
        egresos=egresos_rows,
        sum_neto=sum_neto, sum_iva=sum_iva, sum_total=sum_total,
        proveedores=proveedores,
        q=q, desde=desde, hasta=hasta
    )

@app.route('/egresos/nuevo', methods=['GET', 'POST'])
@require_login
def nuevo_egreso():
    if request.method == 'POST':
        # (Fix #1, #5) Usar .get() y helper to_float()
        fecha = request.form.get('fecha', datetime.now().strftime("%Y-%m-%d"))
        proveedor = request.form.get('proveedor', '').strip()
        medio_pago = request.form.get('medio_pago')
        tipo_doc = request.form.get('tipo_doc')
        monto_neto = to_float(request.form.get('monto_neto')) # Fix
        categoria = request.form.get('categoria', '') # Fix
        observaciones = request.form.get('observaciones', '') # Fix

        iva_credito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_credito

        ensure_proveedor(proveedor) # Usa la conexión 'g'

        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO egresos 
            (fecha, proveedor, medio_pago, tipo_doc, monto_neto, iva_credito, total, categoria, observaciones)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (fecha, proveedor, medio_pago, tipo_doc, monto_neto, iva_credito, total, categoria, observaciones))
        conn.commit()
        rid = cur.lastrowid
        # (Fix #4) No se usa conn.close()
        
        log_change(
            "egresos",
            rid,
            "insert",
            session["user"]["username"],
            json.dumps({
                "proveedor": proveedor,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        
        # (Fix #9) Redirección ya era correcta
        return redirect(url_for('egresos'))
        
    conn = get_db()
    proveedores = conn.execute("SELECT * FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('nuevo_egreso.html', proveedores=proveedores)

@app.route("/egreso/<int:item_id>/editar", methods=["GET", "POST"])
@require_login
def editar_egreso(item_id):
    db = get_db()
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        fecha = request.form.get("fecha")
        proveedor = request.form.get("proveedor", "").strip()
        medio_pago = request.form.get("medio_pago")
        tipo_doc = request.form.get("tipo_doc")
        monto_neto = to_float(request.form.get("monto_neto")) # Fix
        categoria = request.form.get("categoria", "") # Fix
        observaciones = request.form.get("observaciones", "") # Fix

        iva_credito = round(monto_neto * IVA_RATE, 2)
        total = monto_neto + iva_credito

        ensure_proveedor(proveedor) # Usa la conexión 'g'

        db.execute("""
            UPDATE egresos
            SET fecha=?, proveedor=?, medio_pago=?, tipo_doc=?,
                monto_neto=?, iva_credito=?, total=?, categoria=?, observaciones=?
            WHERE id=?
        """, (fecha, proveedor, medio_pago, tipo_doc,
              monto_neto, iva_credito, total, categoria, observaciones, item_id))
        db.commit()
        # (Fix #4) No se usa db.close()
        
        log_change(
            "egresos",
            item_id,
            "update",
            session["user"]["username"],
            json.dumps({
                "proveedor": proveedor,
                "monto_neto": monto_neto,
                "total": total
            })
        )
        flash("✅ Egreso actualizado correctamente.", "success")
        
        # (Fix #9) Redirección ya era correcta
        return redirect(url_for("egresos"))

    e = db.execute("SELECT * FROM egresos WHERE id=?", (item_id,)).fetchone()
    proveedores = db.execute("SELECT * FROM proveedores_master WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa db.close()
    
    if not e:
        flash("❌ Egreso no encontrado.", "danger")
        return redirect(url_for("egresos"))

    flash("✏️ Estás editando un egreso.", "info")
    return render_template("editar_egreso.html", e=e, proveedores=proveedores)

@app.route("/egreso/<int:item_id>/borrar", methods=["POST"])
@require_login
def borrar_egreso(item_id):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password", "")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for("egresos"))

    db = get_db()
    db.execute("DELETE FROM egresos WHERE id=?", (item_id,))
    db.commit()
    # (Fix #4) No se usa db.close()
    
    log_change("egresos", item_id, "delete", session["user"]["username"], "{}")
    flash("🗑️ Egreso borrado correctamente.", "success")
    return redirect(url_for("egresos"))

# ============================================================
# 📈 CRM / PROVEEDORES
# ============================================================
@app.route('/crm')
@require_login
def crm():
    conn = get_db()
    rows = conn.execute('''
        SELECT cliente, COUNT(*) AS transacciones, SUM(total) AS total_vendido
        FROM ingresos
        WHERE cliente IS NOT NULL AND cliente != ''
        GROUP BY cliente
        ORDER BY total_vendido DESC
    ''').fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('crm.html', clientes=rows)

@app.route('/proveedores')
@require_login
def proveedores_resumen():
    conn = get_db()
    rows = conn.execute('''
        SELECT proveedor, COUNT(*) AS transacciones, SUM(total) AS total_gastado
        FROM egresos
        WHERE proveedor IS NOT NULL AND TRIM(proveedor) != ''
        GROUP BY proveedor
        ORDER BY total_gastado DESC
    ''').fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('proveedores.html', proveedores=rows)

# ============================================================
# 🧑‍🤝‍🧑 CLIENTES / PROVEEDORES (maestros)
# ============================================================
@app.route('/clientes')
@require_login
def clientes_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM clientes ORDER BY activo DESC, nombre ASC").fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('clientes.html', clientes=rows)

@app.route('/clientes/nuevo', methods=['GET', 'POST'])
@require_login
def clientes_nuevo():
    if request.method == 'POST':
        # (Fix #5) Usar .get()
        nombre = request.form.get('nombre', '').strip()
        rut = request.form.get('rut', '').strip()
        giro = request.form.get('giro', '').strip()
        telefono = request.form.get('telefono', '').strip()
        email = request.form.get('email', '').strip()
        comuna = request.form.get('comuna', '').strip()
        direccion = request.form.get('direccion', '').strip()
        notas = request.form.get('notas', '').strip()
        
        if not nombre:
            flash("El nombre es obligatorio.", "warning")
            return redirect(url_for('clientes_nuevo'))
            
        conn = get_db()
        conn.execute('''
            INSERT OR IGNORE INTO clientes (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas))
        conn.commit()
        # (Fix #4) No se usa conn.close()
        
        log_change("clientes", 0, "insert", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Cliente guardado.", "success")
        return redirect(url_for('clientes_list'))
    return render_template('nuevo_cliente.html')

@app.route('/clientes/<int:cid>/editar', methods=['GET','POST'])
@require_login
def clientes_editar(cid):
    conn = get_db()
    if request.method == 'POST':
        # (Fix #5) Usar .get()
        nombre = request.form.get('nombre', '').strip()
        rut = request.form.get('rut','').strip()
        giro = request.form.get('giro','').strip()
        telefono = request.form.get('telefono','').strip()
        email = request.form.get('email','').strip()
        comuna = request.form.get('comuna','').strip()
        direccion = request.form.get('direccion','').strip()
        notas = request.form.get('notas','').strip()
        activo = 1 if request.form.get('activo') == 'on' else 0
        
        conn.execute('''
            UPDATE clientes SET nombre=?, rut=?, giro=?, telefono=?, email=?, comuna=?, direccion=?, notas=?, activo=?
            WHERE id=?
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo, cid))
        conn.commit()
        # (Fix #4) No se usa conn.close()
        
        log_change("clientes", cid, "update", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Cliente actualizado.", "success")
        return redirect(url_for('clientes_list'))
        
    row = conn.execute("SELECT * FROM clientes WHERE id=?", (cid,)).fetchone()
    # (Fix #4) No se usa conn.close()
    if not row:
        flash("Cliente no existe.", "warning")
        return redirect(url_for('clientes_list'))
    return render_template('editar_cliente.html', c=row)

@app.route('/clientes/<int:cid>/borrar', methods=['POST'])
@require_login
def clientes_borrar(cid):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password","")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for('clientes_list'))
            
    conn = get_db()
    conn.execute("DELETE FROM clientes WHERE id=?", (cid,))
    conn.commit()
    # (Fix #4) No se usa conn.close()
    
    log_change("clientes", cid, "delete", session["user"]["username"], "{}")
    flash("🗑️ Cliente borrado.", "success")
    return redirect(url_for('clientes_list'))

@app.route('/maestro/proveedores')
@require_login
def proveedores_maestro_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM proveedores_master ORDER BY activo DESC, nombre ASC").fetchall()
    # (Fix #4) No se usa conn.close()
    return render_template('proveedores_maestro.html', proveedores=rows)

@app.route('/maestro/proveedores/nuevo', methods=['GET','POST'])
@require_login
def proveedores_maestro_nuevo():
    if request.method == 'POST':
        # (Fix #5) Usar .get()
        nombre = request.form.get('nombre', '').strip()
        rut = request.form.get('rut','').strip()
        giro = request.form.get('giro','').strip()
        telefono = request.form.get('telefono','').strip()
        email = request.form.get('email','').strip()
        comuna = request.form.get('comuna','').strip()
        direccion = request.form.get('direccion','').strip()
        notas = request.form.get('notas','').strip()
        
        if not nombre:
            flash("El nombre es obligatorio.", "warning")
            return redirect(url_for('proveedores_maestro_nuevo'))
            
        conn = get_db()
        conn.execute('''
            INSERT OR IGNORE INTO proveedores_master (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas))
        conn.commit()
        # (Fix #4) No se usa conn.close()
        
        log_change("proveedores_master", 0, "insert", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Proveedor guardado.", "success")
        return redirect(url_for('proveedores_maestro_list'))
    return render_template('nuevo_proveedor.html')

@app.route('/maestro/proveedores/<int:pid>/editar', methods=['GET','POST'])
@require_login
def proveedores_maestro_editar(pid):
    conn = get_db()
    if request.method == 'POST':
        # (Fix #5) Usar .get()
        nombre = request.form.get('nombre', '').strip()
        rut = request.form.get('rut','').strip()
        giro = request.form.get('giro','').strip()
        telefono = request.form.get('telefono','').strip()
        email = request.form.get('email','').strip()
        comuna = request.form.get('comuna','').strip()
        direccion = request.form.get('direccion','').strip()
        notas = request.form.get('notas','').strip()
        activo = 1 if request.form.get('activo') == 'on' else 0
        
        conn.execute('''
            UPDATE proveedores_master SET nombre=?, rut=?, giro=?, telefono=?, email=?, comuna=?, direccion=?, notas=?, activo=?
            WHERE id=?
        ''', (nombre, rut, giro, telefono, email, comuna, direccion, notas, activo, pid))
        conn.commit()
        # (Fix #4) No se usa conn.close()
        
        log_change("proveedores_master", pid, "update", session["user"]["username"], json.dumps({"nombre": nombre}))
        flash("✅ Proveedor actualizado.", "success")
        return redirect(url_for('proveedores_maestro_list'))
        
    row = conn.execute("SELECT * FROM proveedores_master WHERE id=?", (pid,)).fetchone()
    # (Fix #4) No se usa conn.close()
    if not row:
        flash("Proveedor no existe.", "warning")
        return redirect(url_for('proveedores_maestro_list'))
    return render_template('editar_proveedor.html', p=row)

@app.route('/maestro/proveedores/<int:pid>/borrar', methods=['POST'])
@require_login
def proveedores_maestro_borrar(pid):
    user = session.get("user")
    if not user or user["username"] != SUPER_ADMIN_USERNAME:
        password = request.form.get("password","")
        if password != DELETE_PASSWORD:
            flash("🔒 Contraseña incorrecta. No se borró nada.", "warning")
            return redirect(url_for('proveedores_maestro_list'))
            
    conn = get_db()
    conn.execute("DELETE FROM proveedores_master WHERE id=?", (pid,))
    conn.commit()
    # (Fix #4) No se usa conn.close()
    
    log_change("proveedores_master", pid, "delete", session["user"]["username"], "{}")
    flash("🗑️ Proveedor borrado.", "success")
    return redirect(url_for('proveedores_maestro_list'))

# ============================================================
# 📊 REPORTE MENSUAL
# ============================================================
@app.route('/reporte')
@require_login
def reporte():
    conn = get_db()

    ingresos_rows = conn.execute('''
        SELECT strftime('%Y', fecha) AS year, strftime('%m', fecha) AS month,
               SUM(monto_neto) AS ventas_compras_netas, SUM(iva_debito) AS iva_neto
        FROM ingresos GROUP BY year, month
    ''').fetchall()

    egresos_rows = conn.execute('''
        SELECT strftime('%Y', fecha) AS year, strftime('%m', fecha) AS month,
               -SUM(monto_neto) AS ventas_compras_netas, -SUM(iva_credito) AS iva_neto
        FROM egresos GROUP BY year, month
    ''').fetchall()
    # (Fix #4) No se usa conn.close()

    result = {}
    for row in list(ingresos_rows) + list(egresos_rows):
        key = (row['year'], row['month'])
        if key not in result:
            result[key] = {'year': row['year'], 'month': row['month'], 'ventas_compras_netas': 0, 'iva_neto': 0}
        result[key]['ventas_compras_netas'] += row['ventas_compras_netas'] or 0
        result[key]['iva_neto'] += row['iva_neto'] or 0

    resumen = sorted(list(result.values()), key=lambda x: (x['year'], x['month']), reverse=True)

    return render_template('reporte.html', resumen=resumen)

# ============================================================
# 💾 BACKUPS
# ============================================================
@app.route("/backups/crear", methods=["POST"])
@require_login
def backups_crear():
    ensure_dir(BACKUP_DIR) # Se asegura que exista
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"backup_{ts}.sqlite")
    
    try:
        shutil.copy2(DATABASE, dst)
        
        db = get_db()
        payload = json.dumps({"file": dst})
        db.execute("""
            INSERT INTO notificaciones (canal, destinatario, plantilla, payload_json, estado, created_at)
            VALUES (?,?,?,?,?,?)
        """, ('sistema', 'local', 'backup', payload, 'enviado', now_str()))
        db.commit()
        # (Fix #4) No se usa db.close()
        
        flash(f"📦 Backup creado: {dst}", "success")
    except Exception as e:
        app.logger.error(f"Error al crear backup: {e}")
        flash(f"❌ Error al crear backup: {e}", "danger")
        
    return redirect(url_for("index"))

# ============================================================
# 🧾 EXPORTAR CSV
# ============================================================
def _csv_response(filename_base, headers, rows_matrix):
    sio = StringIO()
    writer = csv.writer(sio)
    writer.writerow(headers)
    for row in rows_matrix:
        writer.writerow(row)
    
    # (Fix #3) Añadir BOM (ufeff) y codificar a utf-8
    data = ("\ufeff" + sio.getvalue()).encode("utf-8")
    filename = f"{filename_base}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Content-Type": "text/csv; charset=utf-8"}) # (Fix #3) Header explícito

@app.route('/ingresos/exportar')
@require_login
def exportar_ingresos():
    # (Fix Lógica) Re-usar la lógica de filtrado de /ingresos
    q = (request.args.get('q') or '').strip().lower()
    desde = (request.args.get('desde') or '').strip()
    hasta = (request.args.get('hasta') or '').strip()

    base_where = "WHERE 1=1"
    params = []

    if q:
        like = f"%{q}%"
        base_where += """ AND (
                LOWER(COALESCE(cliente,'')) LIKE ?
                OR LOWER(COALESCE(medio_pago,'')) LIKE ?
                OR LOWER(COALESCE(tipo_doc,'')) LIKE ?
                OR LOWER(COALESCE(categoria,'')) LIKE ?
                OR LOWER(COALESCE(observaciones,'')) LIKE ?
            )"""
        params += [like, like, like, like, like]
    if desde:
        base_where += " AND date(fecha) >= date(?)"
        params.append(desde)
    if hasta:
        base_where += " AND date(fecha) <= date(?)"
        params.append(hasta)

    sql = f'''
        SELECT fecha, cliente, medio_pago, tipo_doc, monto_neto, iva_debito, total, categoria, observaciones
        FROM ingresos
        {base_where}
        ORDER BY date(fecha) DESC, id DESC
    '''

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    # (Fix #4) No se usa conn.close()

    headers = ["Fecha", "Cliente", "Medio de pago", "Tipo doc", "Monto neto", "IVA débito", "Total", "Categoría", "Observaciones"]
    matrix = [[r["fecha"], r["cliente"], r["medio_pago"], r["tipo_doc"], r["monto_neto"], r["iva_debito"], r["total"], r["categoria"], r["observaciones"]] for r in rows]
    return _csv_response("ingresos_filtrado", headers, matrix)

@app.route('/egresos/exportar')
@require_login
def exportar_egresos():
    # (Fix Lógica) Re-usar la lógica de filtrado de /egresos
    q = (request.args.get('q') or '').strip().lower()
    desde = (request.args.get('desde') or '').strip()
    hasta = (request.args.get('hasta') or '').strip()

    base_where = 'WHERE 1=1'
    params = []

    if q:
        like = f'%{q}%'
        base_where += ''' AND (
            LOWER(COALESCE(proveedor,'')) LIKE ? OR
            LOWER(COALESCE(medio_pago,'')) LIKE ? OR
            LOWER(COALESCE(tipo_doc,'')) LIKE ? OR
            LOWER(COALESCE(categoria,'')) LIKE ? OR
            LOWER(COALESCE(observaciones,'')) LIKE ?
        )'''
        params += [like, like, like, like, like]
    if desde:
        base_where += ' AND date(fecha) >= date(?)'
        params.append(desde)
    if hasta:
        base_where += ' AND date(fecha) <= date(?)'
        params.append(hasta)

    sql = f'''
        SELECT fecha, proveedor, medio_pago, tipo_doc, monto_neto, iva_credito, total, categoria, observaciones
        FROM egresos
        {base_where}
        ORDER BY date(fecha) DESC, id DESC
    '''
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    # (Fix #4) No se usa conn.close()

    headers = ["Fecha", "Proveedor", "Medio de pago", "Tipo doc", "Monto neto", "IVA crédito", "Total", "Categoría", "Observaciones"]
    matrix = [[r["fecha"], r["proveedor"], r["medio_pago"], r["tipo_doc"], r["monto_neto"], r["iva_credito"], r["total"], r["categoria"], r["observaciones"]] for r in rows]
    return _csv_response("egresos_filtrado", headers, matrix)

# ============================================================
# 💳 CUENTAS Y TRANSFERENCIAS
# ============================================================
@app.route("/cuentas")
@require_login
def cuentas_list():
    db = get_db()
    cuentas = db.execute("SELECT * FROM cuentas WHERE activo=1 ORDER BY alias ASC").fetchall()
    saldos = []
    
    # Calcular saldos
    # NOTA: Esto es ineficiente (N+1 queries). Sería mejor un JOIN/GROUP BY,
    # pero para pocos movimientos y cuentas, funciona.
    for cta in cuentas:
        movs = db.execute("SELECT tipo, SUM(monto) as m FROM movimientos_cuenta WHERE cuenta_id=? GROUP BY tipo", (cta["id"],)).fetchall()
        saldo = cta["saldo_inicial"] or 0
        for m in movs:
            t = m["tipo"]; mto = m["m"] or 0
            if t in ("ingreso","transfer_in"):
                saldo += mto
            elif t in ("egreso","transfer_out"):
                saldo -= mto
        saldos.append((cta, saldo))
    # (Fix #4) No se usa db.close()
    
    return render_template("cuentas.html", saldos=saldos)

@app.route("/transferir") # (Fix #2) Ruta creada
@require_login
def transferir():
    """Página de resumen de transferencias (placeholder)."""
    db = get_db()
    transferencias = db.execute("""
        SELECT 
            t.*,
            o.alias as origen_alias,
            d.alias as destino_alias
        FROM transferencias t
        JOIN cuentas o ON t.cuenta_origen_id = o.id
        JOIN cuentas d ON t.cuenta_destino_id = d.id
        ORDER BY t.fecha DESC, t.id DESC
    """).fetchall()
    return render_template("transferencias.html", transferencias=transferencias)


@app.route("/transferencias/nueva", methods=["GET","POST"])
@require_roles("admin","admin_limited","contador")
def transferencia_nueva():
    db = get_db()
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        try:
            origen = int(request.form.get("origen", 0))
            destino = int(request.form.get("destino", 0))
        except ValueError:
            flash("❌ Cuentas de origen y destino inválidas.", "danger")
            return redirect(url_for('transferencia_nueva'))

        monto = to_float(request.form.get("monto"))
        glosa = request.form.get("glosa","Transferencia interna")
        fecha = request.form.get("fecha") or datetime.now().strftime("%Y-%m-%d")
        
        if origen == destino:
            flash("❌ La cuenta de origen y destino no pueden ser la misma.", "warning")
            return redirect(url_for('transferencia_nueva'))
        if monto <= 0:
            flash("❌ El monto debe ser mayor a cero.", "warning")
            return redirect(url_for('transferencia_nueva'))
            
        cur = db.cursor()
        cur.execute(
            "INSERT INTO transferencias (cuenta_origen_id, cuenta_destino_id, monto, glosa, fecha, created_at) VALUES (?,?,?,?,?,?)",
            (origen, destino, monto, glosa, fecha, now_str())
        )
        transfer_id = cur.lastrowid
        
        # Asiento doble
        cur.execute(
            "INSERT INTO movimientos_cuenta (cuenta_id, tipo, monto, glosa, ref_id, fecha, created_at) VALUES (?,?,?,?,?,?,?)",
            (origen, "transfer_out", monto, glosa, transfer_id, fecha, now_str())
        )
        cur.execute(
            "INSERT INTO movimientos_cuenta (cuenta_id, tipo, monto, glosa, ref_id, fecha, created_at) VALUES (?,?,?,?,?,?,?)",
            (destino, "transfer_in",  monto, glosa, transfer_id, fecha, now_str())
        )
        db.commit()
        # (Fix #4) No se usa db.close()
        
        flash("🔁 Transferencia registrada.", "success")
        return redirect(url_for("cuentas_list"))
        
    cuentas = db.execute("SELECT * FROM cuentas WHERE activo=1 ORDER BY alias ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("transferencia_nueva.html", cuentas=cuentas)

# ============================================================
# 📅 AGENDA Y PEDIDOS
# ============================================================
@app.route("/agenda")
@require_login
def agenda():
    db = get_db()
    eventos = db.execute("SELECT * FROM agenda_eventos ORDER BY fecha_inicio ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("agenda.html", eventos=eventos)

@app.route("/agenda/nuevo", methods=["GET","POST"])
@require_roles("admin","admin_limited","operador","contador")
def agenda_nuevo():
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        titulo = request.form.get("titulo", "").strip()
        if not titulo:
            flash("❌ El título del evento es obligatorio.", "danger")
            return redirect(url_for('agenda_nuevo'))
            
        descripcion = request.form.get("descripcion","")
        fecha_inicio = request.form.get("fecha_inicio")
        fecha_fin = request.form.get("fecha_fin", fecha_inicio)
        lugar = request.form.get("lugar","")
        cliente_id = request.form.get("cliente_id") or None
        recordatorio_min = to_float(request.form.get("recordatorio_minutos","1440"), 1440)
        
        db = get_db()
        db.execute("""
            INSERT INTO agenda_eventos (titulo, descripcion, fecha_inicio, fecha_fin, lugar, cliente_id, recordatorio_minutos, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """,(titulo, descripcion, fecha_inicio, fecha_fin, lugar, cliente_id, recordatorio_min, now_str()))
        db.commit()
        # (Fix #4) No se usa db.close()
        
        flash("🗓️ Evento agendado.", "success")
        return redirect(url_for("agenda"))
        
    db = get_db()
    clientes = db.execute("SELECT id, nombre FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("agenda_nuevo.html", clientes=clientes)

@app.route("/pedidos")
@require_login
def pedidos():
    db = get_db()
    rows = db.execute("""
        SELECT p.*, COALESCE(c.nombre,'(s/cliente)') as cliente_nombre
        FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id
        ORDER BY p.fecha_evento ASC, p.hora_evento ASC
    """).fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("pedidos.html", pedidos=rows)

@app.route("/pedidos/nuevo", methods=["GET","POST"])
@require_roles("admin","admin_limited","operador","contador")
def pedidos_nuevo():
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        tipo = request.form.get("tipo", "otros")
        cliente_id = request.form.get("cliente_id") or None
        fecha_evento = request.form.get("fecha_evento")
        hora_evento = request.form.get("hora_evento")
        lugar = request.form.get("lugar","")
        estado = request.form.get("estado","cotizado")
        costo_estimado = to_float(request.form.get("costo_estimado"))
        precio_venta   = to_float(request.form.get("precio_venta"))
        ganancia_esp   = precio_venta - costo_estimado
        obs = request.form.get("observaciones","")
        
        db = get_db()
        db.execute("""
            INSERT INTO pedidos (tipo, cliente_id, fecha_evento, hora_evento, lugar, estado, costo_estimado, precio_venta, ganancia_esperada, observaciones, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,(tipo, cliente_id, fecha_evento, hora_evento, lugar, estado, costo_estimado, precio_venta, ganancia_esp, obs, now_str()))
        db.commit()
        # (Fix #4) No se usa db.close()
        
        flash("🧾 Pedido creado.", "success")
        return redirect(url_for("pedidos"))
        
    db = get_db()
    clientes = db.execute("SELECT id, nombre FROM clientes WHERE activo=1 ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("pedidos_nuevo.html", clientes=clientes)

# ============================================================
# 📦 INVENTARIO + BOM
# ============================================================
@app.route("/inventario")
@require_login
def inventario():
    db = get_db()
    items = db.execute("SELECT * FROM inventario_items ORDER BY nombre ASC").fetchall()
    alertas = [it for it in items if it["umbral_min"] and (it["stock"] or 0) <= it["umbral_min"]]
    # (Fix #4) No se usa db.close()
    return render_template("inventario.html", items=items, alertas=alertas)

@app.route("/inventario/nuevo_item", methods=["GET","POST"])
@require_roles("admin","admin_limited","contador")
def inventario_nuevo_item():
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        nombre = request.form.get("nombre", "").strip()
        if not nombre:
            flash("❌ El nombre del ítem es obligatorio.", "danger")
            return redirect(url_for('inventario_nuevo_item'))
            
        unidad = request.form.get("unidad","un").strip()
        stock = to_float(request.form.get("stock"))
        umbral = to_float(request.form.get("umbral_min"))
        costo_u = to_float(request.form.get("costo_unitario"))
        categoria = request.form.get("categoria","Insumo")
        usa_lotes = 1 if request.form.get("usa_lotes") == "on" else 0
        
        db = get_db()
        try:
            db.execute("""
                INSERT INTO inventario_items (nombre, unidad, stock, umbral_min, costo_unitario, categoria, usa_lotes)
                VALUES (?,?,?,?,?,?,?)
            """,(nombre, unidad, stock, umbral, costo_u, categoria, usa_lotes))
            db.commit()
            flash("📦 Ítem creado.", "success")
        except sqlite3.IntegrityError:
            flash("❌ Ya existe un ítem con ese nombre.", "danger")
        finally:
            # (Fix #4) No se usa db.close()
            pass
        return redirect(url_for("inventario"))
    return render_template("inventario_nuevo_item.html")

@app.route('/inventario/<int:item_id>/editar', methods=['GET', 'POST']) # (Fix #6) Ruta creada
@require_login
@require_roles("admin", "admin_limited")
def inventario_editar(item_id):
    """Edita un ítem de inventario existente."""
    db = get_db()
    if request.method == 'POST':
        # (Fix #1, #5) Usar .get() y helper to_float()
        nombre = request.form.get('nombre', '').strip()
        if not nombre:
            flash("❌ El nombre del ítem es obligatorio.", "danger")
            return redirect(url_for('inventario_editar', item_id=item_id))
            
        unidad = request.form.get('unidad', 'un')
        categoria = request.form.get('categoria', 'General')
        # Nota: Editar el stock aquí es un "ajuste manual".
        # Idealmente el stock solo se mueve con 'inventario_ajuste'.
        stock = to_float(request.form.get('stock'))
        costo_unitario = to_float(request.form.get('costo_unitario'))
        umbral_min = to_float(request.form.get('umbral_min'))
        usa_lotes = 1 if request.form.get("usa_lotes") == "on" else 0

        try:
            db.execute("""
                UPDATE inventario_items 
                SET nombre=?, unidad=?, categoria=?, stock=?, costo_unitario=?, umbral_min=?, usa_lotes=?
                WHERE id=?
            """, (nombre, unidad, categoria, stock, costo_unitario, umbral_min, usa_lotes, item_id))
            db.commit()
            
            log_change("inventario_items", item_id, "update", session["user"]["username"], json.dumps({"nombre": nombre, "stock": stock}))
            flash(f"✅ Ítem '{nombre}' actualizado.", "success")
            return redirect(url_for('inventario'))
            
        except sqlite3.IntegrityError:
            flash(f"❌ Error de integridad. ¿Quizás el nombre ya existe?", "danger")
        except Exception as e:
            app.logger.exception("Error en inventario_editar")
            flash(f"Error al actualizar: {e}", "danger")
            
        return redirect(url_for('inventario_editar', item_id=item_id))

    # Para método GET
    item = db.execute("SELECT * FROM inventario_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("❌ El ítem no existe.", "danger")
        return redirect(url_for('inventario'))
        
    return render_template('inventario_editar_item.html', item=item) # Asume este template

@app.route('/inventario/<int:item_id>/borrar', methods=['POST']) # (Fix #6) Ruta creada
@require_login
@require_roles("admin")
def inventario_borrar(item_id):
    """Borra un ítem de inventario (solo admin)."""
    db = get_db()
    try:
        # Primero, verificar que no esté en uso en BOM
        en_bom = db.execute("SELECT COUNT(*) FROM bom WHERE item_id=?", (item_id,)).fetchone()[0]
        if en_bom > 0:
            flash(f"❌ No se puede borrar. El ítem está en uso en {en_bom} receta(s) (BOM).", "danger")
            return redirect(url_for('inventario'))

        # Si no está en BOM, borrar movimientos y luego el ítem
        db.execute("DELETE FROM inventario_mov WHERE item_id=?", (item_id,))
        db.execute("DELETE FROM inventario_items WHERE id=?", (item_id,))
        db.commit()
        
        log_change("inventario_items", item_id, "delete", session["user"]["username"], "{}")
        flash("🗑️ Ítem de inventario y sus movimientos han sido borrados.", "success")
    except sqlite3.IntegrityError as e:
        flash(f"❌ No se pudo borrar el ítem, puede estar en uso. Error: {e}", "danger")
    except Exception as e:
        app.logger.exception("Error en inventario_borrar")
        flash(f"Error al borrar: {e}", "danger")
        
    return redirect(url_for('inventario'))


@app.route("/inventario/mov/<int:item_id>/ajuste", methods=["POST"])
@require_roles("admin","admin_limited","contador")
def inventario_ajuste(item_id):
    # (Fix #1, #5) Usar .get() y helper to_float()
    tipo = request.form.get("tipo") # entrada/salida
    if tipo not in ('entrada', 'salida'):
        flash("❌ Tipo de movimiento inválido.", "danger")
        return redirect(url_for("inventario"))
        
    cantidad = to_float(request.form.get("cantidad"))
    if cantidad <= 0:
        flash("❌ La cantidad debe ser mayor a cero.", "danger")
        return redirect(url_for("inventario"))

    costo_unit = to_float(request.form.get("costo_unit", "0"))
    lote = request.form.get("lote","")
    vence = request.form.get("vence","")
    motivo = request.form.get("motivo","ajuste")
    
    db = get_db()
    
    current = db.execute("SELECT stock, costo_unitario FROM inventario_items WHERE id=?", (item_id,)).fetchone()
    if not current:
        db.close()
        flash("Ítem inexistente.", "danger")
        return redirect(url_for("inventario"))
        
    db.execute("""
        INSERT INTO inventario_mov (item_id, tipo, cantidad, costo_unit, lote, vence, motivo, ref, fecha, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """,(item_id, tipo, cantidad, costo_unit, lote, vence, motivo, "ajuste", datetime.now().strftime("%Y-%m-%d"), now_str()))
    
    stock = (current["stock"] or 0)
    
    if tipo == "entrada":
        nuevo_stock = stock + cantidad
        # Recalcular costo promedio ponderado solo si hay entrada
        if nuevo_stock > 0:
            nuevo_costo = ((stock * (current["costo_unitario"] or 0)) + (cantidad * costo_unit)) / nuevo_stock
        else:
            nuevo_costo = current["costo_unitario"] or 0
        db.execute("UPDATE inventario_items SET stock=?, costo_unitario=? WHERE id=?",
                   (nuevo_stock, nuevo_costo, item_id))
    else: # Salida
        nuevo_stock = stock - cantidad
        if nuevo_stock < 0:
             app.logger.warning(f"Stock negativo para item {item_id} después de ajuste.")
        # El costo unitario no cambia en una salida
        db.execute("UPDATE inventario_items SET stock=? WHERE id=?", (nuevo_stock, item_id))
        
    db.commit()
    # (Fix #4) No se usa db.close()
    
    flash("✅ Movimiento aplicado.", "success")
    return redirect(url_for("inventario"))

@app.route("/bom")
@require_login
def bom_list():
    db = get_db()
    productos = db.execute("SELECT DISTINCT producto FROM bom ORDER BY producto ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("bom.html", productos=productos)

@app.route("/bom/editar", methods=["GET","POST"])
@require_roles("admin","admin_limited","contador")
def bom_editar():
    db = get_db()
    if request.method == "POST":
        # (Fix #1, #5) Usar .get() y helper to_float()
        producto = request.form.get("producto", "").strip()
        if not producto:
            flash("❌ El nombre del producto (receta) es obligatorio.", "danger")
            return redirect(url_for("bom_editar"))

        db.execute("DELETE FROM bom WHERE producto=?", (producto,))
        
        # Lógica para procesar múltiples ítems
        indices = [k.split("_")[-1] for k in request.form.keys() if k.startswith("item_id_")]
        
        items_agregados = 0
        for idx in indices:
            try:
                item_id = int(request.form.get(f"item_id_{idx}"))
                cantidad = to_float(request.form.get(f"cantidad_{idx}", "0"))
                unidad = request.form.get(f"unidad_{idx}", "")
                
                if item_id and cantidad > 0:
                    db.execute("INSERT INTO bom (producto, item_id, cantidad, unidad) VALUES (?,?,?,?)",
                               (producto, item_id, cantidad, unidad))
                    items_agregados += 1
            except (ValueError, TypeError):
                app.logger.warning(f"Error procesando item {idx} del BOM para {producto}.")
                continue
                
        db.commit()
        # (Fix #4) No se usa db.close()
        
        flash(f"🧪 Receta (BOM) '{producto}' actualizada con {items_agregados} ítems.", "success")
        return redirect(url_for("bom_list"))
        
    items = db.execute("SELECT id, nombre, unidad FROM inventario_items ORDER BY nombre ASC").fetchall()
    # (Fix #4) No se usa db.close()
    return render_template("bom_editar.html", items=items)

# ============================================================
# 🔔 NOTIFICACIONES Y PRUEBAS
# ============================================================
@app.route("/notificaciones/test", methods=["POST"])
@require_roles("admin","admin_limited","contador")
def notificaciones_test():
    to_addr = request.form.get("to", "mesaperfectactering@gmail.com")
    ok, msg = send_email(to_addr, "Prueba CRM Mesa Perfecta", "Hola, esta es una prueba automática del CRM.\n\n(ñ, á, é, í, ó, ú)")
    if ok:
        flash("📨 Correo de prueba enviado.", "success")
    else:
        flash(f"❌ No se pudo enviar: {msg}", "danger")
    return redirect(url_for("index"))

@app.route("/test/whatsapp") # (Fix #8) Ruta de prueba creada
@require_roles("admin")
def test_whatsapp():
    """Ruta de prueba para placeholder de WhatsApp."""
    to = request.args.get('to', TEL_PERSONAL)
    message = f"Test de WhatsApp (Placeholder) - {now_str()}"
    
    success, message_log = send_whatsapp_placeholder(to, message)
    
    if success:
        flash("✅ (Placeholder) Intento de WhatsApp registrado en logs.", "info")
    else:
        flash(f"❌ Error en placeholder de WhatsApp: {message_log}", "warning")
        
    return redirect(url_for('index'))

# ============================================================
# 🔒 CIERRES MENSUALES
# ============================================================
@app.route("/cierres/cerrar_mes", methods=["POST"])
@require_roles("admin","contador")
def cerrar_mes():
    try:
        anio = int(request.form.get("anio"))
        mes  = int(request.form.get("mes"))
    except (ValueError, TypeError):
        flash("❌ Año o mes inválido.", "danger")
        return redirect(url_for('index'))
        
    ini = datetime(anio, mes, 1)
    fin = (ini + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    
    db = get_db()
    ing = db.execute("""
        SELECT COALESCE(SUM(monto_neto),0) as v, COALESCE(SUM(iva_debito),0) as iva
        FROM ingresos WHERE date(fecha) BETWEEN date(?) AND date(?)
    """,(ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"))).fetchone()
    
    eg = db.execute("""
        SELECT COALESCE(SUM(monto_neto),0) as v, COALESCE(SUM(iva_credito),0) as iva
        FROM egresos WHERE date(fecha) BETWEEN date(?) AND date(?)
    """,(ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"))).fetchone()

    ventas_brutas = ing["v"] or 0
    iva_debito    = ing["iva"] or 0
    gastos_brutos = eg["v"] or 0
    iva_credito   = eg["iva"] or 0
    utilidad_neta = (ventas_brutas - gastos_brutos)

    try:
        db.execute("""
            INSERT INTO cierres_mensuales (anio, mes, cerrado_por, ventas_brutas, iva_debito, gastos_brutos, iva_credito, utilidad_neta, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """,(anio, mes, session["user"]["username"], ventas_brutas, iva_debito, gastos_brutos, iva_credito, utilidad_neta, now_str()))
        db.commit()
        flash(f"🔏 Mes {anio}-{mes:02d} cerrado.", "success")
    except sqlite3.IntegrityError:
        flash("⚠️ Ese mes ya estaba cerrado.", "warning")
    finally:
        # (Fix #4) No se usa db.close()
        pass
    return redirect(url_for("index"))

# ============================================================
# 🏥 SALUD
# ============================================================
@app.route("/health")
def health():
    try:
        db = get_db()
        db.execute("SELECT 1")
        # (Fix #4) No se usa db.close()
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        app.logger.error(f"Error en /health: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ============================================================
# 🚀 RUN
# ============================================================
if __name__ == '__main__':
    # (Fix #12) Ejecutar init_db() una vez al inicio
    with app.app_context():
        init_db()
    
    # Usar '0.0.0.0' para que sea accesible en la red local
    app.run(host='0.0.0.0', port=5000, debug=True)