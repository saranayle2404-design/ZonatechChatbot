import sqlite3
from datetime import datetime
from pathlib import Path

from catalog_data import INITIAL_CATALOG

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "database" / "zonatech.db"


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    with get_connection() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL, telefono TEXT NOT NULL UNIQUE, correo TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS productos (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT UNIQUE, nombre TEXT NOT NULL, categoria TEXT NOT NULL, marca TEXT, modelo TEXT, descripcion TEXT, precio REAL, stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0), activo INTEGER NOT NULL DEFAULT 1 CHECK(activo IN (0, 1)), imagen_url TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS reparaciones (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE, cliente_id INTEGER NOT NULL, tipo_equipo TEXT NOT NULL, marca TEXT NOT NULL, modelo TEXT NOT NULL, problema TEXT NOT NULL, descripcion_adicional TEXT, fecha_ingreso TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, estado TEXT NOT NULL DEFAULT 'Recibido', FOREIGN KEY (cliente_id) REFERENCES clientes(id));
            CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE, cliente_id INTEGER NOT NULL, subtotal REAL NOT NULL DEFAULT 0, costo_envio REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0, metodo_pago TEXT, direccion_entrega TEXT, estado TEXT NOT NULL DEFAULT 'Pendiente', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (cliente_id) REFERENCES clientes(id));
            CREATE TABLE IF NOT EXISTS detalle_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, producto_id INTEGER NOT NULL, cantidad INTEGER NOT NULL CHECK(cantidad > 0), precio_unitario REAL NOT NULL, subtotal REAL NOT NULL, FOREIGN KEY (pedido_id) REFERENCES pedidos(id), FOREIGN KEY (producto_id) REFERENCES productos(id));
            CREATE TABLE IF NOT EXISTS mensajes (id INTEGER PRIMARY KEY AUTOINCREMENT, telefono TEXT, remitente TEXT NOT NULL CHECK(remitente IN ('usuario', 'bot', 'asesor')), contenido TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(productos)")}
        if "source_id" not in columns:
            connection.execute("ALTER TABLE productos ADD COLUMN source_id TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_productos_source_id ON productos(source_id)")
        repair_columns = {row["name"] for row in connection.execute("PRAGMA table_info(reparaciones)")}
        repair_migrations = {
            "cotizacion": "REAL",
            "observaciones_cotizacion": "TEXT",
            "fecha_cotizacion": "TEXT",
            "respuesta_cotizacion": "TEXT",
        }
        for column, definition in repair_migrations.items():
            if column not in repair_columns:
                connection.execute(f"ALTER TABLE reparaciones ADD COLUMN {column} {definition}")
        seed_catalog(connection)


def seed_catalog(connection):
    for product in INITIAL_CATALOG:
        connection.execute("""
            INSERT INTO productos (source_id,nombre,categoria,marca,modelo,descripcion,precio,stock,activo,imagen_url)
            VALUES (:source_id,:nombre,:categoria,:marca,:modelo,:descripcion,:precio,:stock,1,:imagen_url)
            ON CONFLICT(source_id) DO NOTHING
        """, product)


def list_categories():
    with get_connection() as connection:
        return connection.execute("SELECT categoria,COUNT(*) AS total FROM productos WHERE activo=1 GROUP BY categoria ORDER BY categoria").fetchall()


def search_products(query="", category=None, max_price=None, available_only=False, page=1, page_size=5):
    clauses, values = ["activo=1"], []
    if category:
        clauses.append("LOWER(categoria)=LOWER(?)"); values.append(category)
    for word in [word for word in query.lower().split() if len(word) > 1]:
        clauses.append("(LOWER(nombre) LIKE ? OR LOWER(marca) LIKE ? OR LOWER(modelo) LIKE ? OR LOWER(descripcion) LIKE ?)")
        values.extend([f"%{word}%"] * 4)
    if max_price is not None:
        clauses.append("precio IS NOT NULL AND precio <= ?"); values.append(max_price)
    if available_only:
        clauses.append("stock > 0")
    where = " WHERE " + " AND ".join(clauses)
    with get_connection() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM productos{where}", values).fetchone()[0]
        items = connection.execute(f"SELECT * FROM productos{where} ORDER BY nombre LIMIT ? OFFSET ?", values + [page_size, (page - 1) * page_size]).fetchall()
    return items, total


def get_product(product_id):
    with get_connection() as connection:
        return connection.execute("SELECT * FROM productos WHERE id=? AND activo=1", (product_id,)).fetchone()


def create_order(customer, cart, address, payment_method):
    with get_connection() as connection:
        for item in cart:
            product = connection.execute("SELECT * FROM productos WHERE id=? AND activo=1", (item["product_id"],)).fetchone()
            if not product or product["stock"] < item["quantity"]:
                raise ValueError(f"El producto seleccionado ya no tiene stock suficiente: {item['name']}.")
        connection.execute("INSERT INTO clientes (nombre,telefono) VALUES (?,?) ON CONFLICT(telefono) DO UPDATE SET nombre=excluded.nombre", (customer["name"], customer["phone"]))
        client = connection.execute("SELECT id FROM clientes WHERE telefono=?", (customer["phone"],)).fetchone()
        subtotal = sum(item["price"] * item["quantity"] for item in cart)
        code = "ZT-" + datetime.now().strftime("%Y%m%d%H%M%S")
        cursor = connection.execute("INSERT INTO pedidos (codigo,cliente_id,subtotal,costo_envio,total,metodo_pago,direccion_entrega,estado) VALUES (?,?,?,0,?,?,?,'Pendiente')", (code, client["id"], subtotal, subtotal, payment_method, address))
        for item in cart:
            connection.execute("INSERT INTO detalle_pedido (pedido_id,producto_id,cantidad,precio_unitario,subtotal) VALUES (?,?,?,?,?)", (cursor.lastrowid, item["product_id"], item["quantity"], item["price"], item["price"] * item["quantity"]))
            connection.execute("UPDATE productos SET stock=stock-?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["quantity"], item["product_id"]))
    return code, subtotal


REPAIR_STATUSES = {
    "Recibido", "En diagnostico", "Esperando aprobacion", "En reparacion",
    "Listo para recoger", "Entregado", "Cancelado",
}


def create_repair(customer, equipment):
    """Registers a repair and returns its sequential, date-based public code."""
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO clientes (nombre,telefono) VALUES (?,?) "
            "ON CONFLICT(telefono) DO UPDATE SET nombre=excluded.nombre",
            (customer["name"], customer["phone"]),
        )
        client = connection.execute("SELECT id FROM clientes WHERE telefono=?", (customer["phone"],)).fetchone()
        date_part = datetime.now().strftime("%Y%m%d")
        prefix = f"ZT-R-{date_part}-"
        last_code = connection.execute(
            "SELECT codigo FROM reparaciones WHERE codigo LIKE ? ORDER BY codigo DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        sequence = int(last_code["codigo"].rsplit("-", 1)[1]) + 1 if last_code else 1
        code = f"{prefix}{sequence:04d}"
        connection.execute(
            """INSERT INTO reparaciones
            (codigo,cliente_id,tipo_equipo,marca,modelo,problema,descripcion_adicional,estado)
            VALUES (?,?,?,?,?,?,?,'Recibido')""",
            (code, client["id"], equipment["type"], equipment["brand"], equipment["model"], equipment["problem"], equipment["description"]),
        )
    return code


def get_repair_by_code_and_phone(code, phone):
    with get_connection() as connection:
        return connection.execute(
            """SELECT r.*, c.nombre AS cliente_nombre, c.telefono
            FROM reparaciones r JOIN clientes c ON c.id=r.cliente_id
            WHERE UPPER(r.codigo)=UPPER(?) AND c.telefono=?""",
            (code.strip(), phone),
        ).fetchone()


def update_repair_status(code, status):
    if status not in REPAIR_STATUSES:
        raise ValueError("Estado de reparacion no valido.")
    with get_connection() as connection:
        updated = connection.execute("UPDATE reparaciones SET estado=? WHERE codigo=?", (status, code)).rowcount
    if not updated:
        raise ValueError("No existe una reparacion con ese codigo.")


def set_repair_quote(code, amount, observations, decision=None):
    if decision not in {None, "Aprobada", "Rechazada"}:
        raise ValueError("Respuesta de cotizacion no valida.")
    with get_connection() as connection:
        updated = connection.execute(
            """UPDATE reparaciones SET cotizacion=?,observaciones_cotizacion=?,
            fecha_cotizacion=CURRENT_TIMESTAMP,respuesta_cotizacion=?,
            estado=CASE WHEN ?='Aprobada' THEN 'En reparacion'
                         WHEN ?='Rechazada' THEN 'Cancelado'
                         ELSE 'Esperando aprobacion' END WHERE codigo=?""",
            (amount, observations, decision, decision, decision, code),
        ).rowcount
    if not updated:
        raise ValueError("No existe una reparacion con ese codigo.")
