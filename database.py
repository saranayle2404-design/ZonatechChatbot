import sqlite3
import secrets
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
            CREATE TABLE IF NOT EXISTS cola_mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente', 'procesando', 'procesado', 'error')),
                intentos INTEGER NOT NULL DEFAULT 0,
                ultimo_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_cola_mensajes_estado ON cola_mensajes(estado, id);
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


def search_products(query="", category=None, max_price=None, available_only=False, page=1, page_size=5, brand=None):
    clauses, values = ["activo=1"], []
    if category:
        clauses.append("LOWER(categoria)=LOWER(?)"); values.append(category)
    if brand:
        clauses.append("LOWER(marca)=LOWER(?)"); values.append(brand)
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
        if not cart:
            raise ValueError("El carrito está vacío.")

        quantities = {}
        for item in cart:
            product_id = item.get("product_id")
            quantity = item.get("quantity")
            if not isinstance(product_id, int) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError("El carrito contiene una cantidad o producto inválido.")
            quantities[product_id] = quantities.get(product_id, 0) + quantity

        order_items = []
        for product_id, quantity in quantities.items():
            product = connection.execute(
                "SELECT * FROM productos WHERE id=? AND activo=1",
                (product_id,),
            ).fetchone()
            if not product or product["precio"] is None:
                raise ValueError("El producto seleccionado ya no está disponible para compra.")
            if payment_method in {"Addi", "Sistecrédito"} and product["categoria"] not in {"Audífonos", "Adaptadores"}:
                raise ValueError(f"{payment_method} solo está disponible para Audífonos y Adaptadores.")

            updated = connection.execute(
                """UPDATE productos
                SET stock=stock-?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND activo=1 AND stock>=?""",
                (quantity, product_id, quantity),
            ).rowcount
            if updated != 1:
                raise ValueError(f"El producto seleccionado ya no tiene stock suficiente: {product['nombre']}.")
            order_items.append((product, quantity))

        subtotal = sum(product["precio"] * quantity for product, quantity in order_items)
        connection.execute(
            "INSERT INTO clientes (nombre,telefono) VALUES (?,?) "
            "ON CONFLICT(telefono) DO UPDATE SET nombre=excluded.nombre",
            (customer["name"], customer["phone"]),
        )
        client = connection.execute(
            "SELECT id FROM clientes WHERE telefono=?",
            (customer["phone"],),
        ).fetchone()

        cursor = None
        code = None
        for _ in range(8):
            # token_hex(2) daba solo 65.536 combinaciones posibles (2 bytes),
            # adivinables por fuerza bruta si el código se usa como parte de
            # una credencial de consulta. token_hex(6) da ~2.8e14 combinaciones.
            candidate = "ZT-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(6).upper()
            try:
                cursor = connection.execute(
                    """INSERT INTO pedidos
                    (codigo,cliente_id,subtotal,costo_envio,total,metodo_pago,direccion_entrega,estado)
                    VALUES (?,?,?,0,?,?,?,'Pendiente')""",
                    (candidate, client["id"], subtotal, subtotal, payment_method, address),
                )
                code = candidate
                break
            except sqlite3.IntegrityError as error:
                if "pedidos.codigo" not in str(error):
                    raise
        if cursor is None or code is None:
            raise RuntimeError("No fue posible generar un código único para el pedido.")

        for product, quantity in order_items:
            item_subtotal = product["precio"] * quantity
            connection.execute(
                """INSERT INTO detalle_pedido
                (pedido_id,producto_id,cantidad,precio_unitario,subtotal)
                VALUES (?,?,?,?,?)""",
                (cursor.lastrowid, product["id"], quantity, product["precio"], item_subtotal),
            )
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


# --- Cola de mensajes entrantes de WhatsApp ---------------------------------
#
# Cola "tonta": guarda el payload crudo del webhook de Meta tal cual llega,
# sin interpretarlo. Quien decide qué significa el mensaje (wa_id, texto,
# tipo) es el worker al momento de procesar, no esta capa. Así, un cambio
# futuro en cómo se interpreta un mensaje nunca deja datos ya parseados e
# inconsistentes guardados en la cola, y el payload original queda completo
# para depurar si algo falla.
#
# Persistida en SQLite (no en memoria) para que ningún mensaje se pierda si
# el proceso se reinicia o se cae a mitad de procesar uno.

MAX_INTENTOS_COLA = 5


def enqueue_message(payload):
    """Guarda el payload crudo (string JSON) del webhook de Meta como
    pendiente de procesar. Devuelve el id de la fila insertada."""
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO cola_mensajes (payload, estado) VALUES (?, 'pendiente')",
            (payload,),
        )
        return cursor.lastrowid


def claim_next_pending_message():
    """Toma el mensaje pendiente más antiguo y lo marca 'procesando' de forma
    atómica, para que dos workers no puedan tomar el mismo mensaje a la vez.
    Devuelve la fila (con su payload) o None si no hay nada pendiente."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, payload, intentos FROM cola_mensajes "
            "WHERE estado='pendiente' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            "UPDATE cola_mensajes SET estado='procesando', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND estado='pendiente'",
            (row["id"],),
        ).rowcount
        if updated != 1:
            # Otro worker se lo llevó primero entre el SELECT y el UPDATE.
            return None
        return row


def mark_message_processed(message_id):
    with get_connection() as connection:
        connection.execute(
            "UPDATE cola_mensajes SET estado='procesado', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (message_id,),
        )


def mark_message_error(message_id, error_text):
    """Registra el fallo. Si aún no se alcanzó MAX_INTENTOS_COLA, vuelve a
    'pendiente' para que el worker lo reintente más adelante; si ya se
    agotaron los intentos, queda en 'error' definitivo."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT intentos FROM cola_mensajes WHERE id=?", (message_id,)
        ).fetchone()
        intentos = (row["intentos"] if row else 0) + 1
        siguiente_estado = "error" if intentos >= MAX_INTENTOS_COLA else "pendiente"
        connection.execute(
            "UPDATE cola_mensajes SET estado=?, intentos=?, ultimo_error=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (siguiente_estado, intentos, str(error_text)[:2000], message_id),
        )