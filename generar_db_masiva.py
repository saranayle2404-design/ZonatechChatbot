import sqlite3
import random
import uuid
import os
import shutil
from pathlib import Path

# Configuración de rutas
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "database" / "zonatech.db"
BACKUP_PATH = BASE_DIR / "database" / "zonatech_backup.db"

# Diccionarios de datos para generar productos realistas
CATEGORIAS = {
    "Celulares": {"marcas": ["Apple", "Samsung", "Xiaomi", "Motorola", "Tecno", "Infinix", "Vivo", "Oppo"], "precio_min": 300000, "precio_max": 6000000},
    "Computadores": {"marcas": ["HP", "Lenovo", "Dell", "Asus", "Acer", "Apple", "MSI"], "precio_min": 1000000, "precio_max": 12000000},
    "Audífonos": {"marcas": ["JBL", "Sony", "Bose", "Apple", "Xiaomi", "Movisun", "Vidvie", "Logitech", "Skullcandy"], "precio_min": 40000, "precio_max": 1500000},
    "Smartwatch": {"marcas": ["Apple", "Samsung", "Huawei", "Xiaomi", "Garmin", "Amazfit"], "precio_min": 150000, "precio_max": 4000000},
    "Tablets": {"marcas": ["Apple", "Samsung", "Lenovo", "Xiaomi"], "precio_min": 500000, "precio_max": 5000000},
    "Monitores": {"marcas": ["LG", "Samsung", "Dell", "Asus", "Acer", "AOC"], "precio_min": 350000, "precio_max": 3500000},
    "Consolas": {"marcas": ["Sony", "Microsoft", "Nintendo"], "precio_min": 1200000, "precio_max": 3800000},
    "Accesorios": {"marcas": ["Logitech", "Razer", "Corsair", "Redragon", "Genius"], "precio_min": 25000, "precio_max": 500000},
    "Cargadores": {"marcas": ["Apple", "Samsung", "Anker", "Ugreen", "Movisun", "Vidvie"], "precio_min": 25000, "precio_max": 250000},
    "Cables": {"marcas": ["Anker", "Ugreen", "Vidvie", "Movisun", "Belkin", "Generico"], "precio_min": 10000, "precio_max": 120000}
}

SUFIJOS = ["Pro", "Max", "Ultra", "Lite", "Plus", "FE", "5G", "Gaming", "Slim", "Wireless", "TWS", "Series", "Edition"]
COLORES = ["Negro", "Blanco", "Azul", "Rojo", "Gris", "Plata", "Oro"]
ALMACENAMIENTOS = ["64GB", "128GB", "256GB", "512GB", "1TB"]

def crear_esquema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL, telefono TEXT NOT NULL UNIQUE, correo TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS productos (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT UNIQUE, nombre TEXT NOT NULL, categoria TEXT NOT NULL, marca TEXT, modelo TEXT, descripcion TEXT, precio REAL, stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0), activo INTEGER NOT NULL DEFAULT 1 CHECK(activo IN (0, 1)), imagen_url TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reparaciones (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE, cliente_id INTEGER NOT NULL, tipo_equipo TEXT NOT NULL, marca TEXT NOT NULL, modelo TEXT NOT NULL, problema TEXT NOT NULL, descripcion_adicional TEXT, fecha_ingreso TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, estado TEXT NOT NULL DEFAULT 'Recibido', cotizacion REAL, observaciones_cotizacion TEXT, fecha_cotizacion TEXT, respuesta_cotizacion TEXT, FOREIGN KEY (cliente_id) REFERENCES clientes(id));
        CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE, cliente_id INTEGER NOT NULL, subtotal REAL NOT NULL DEFAULT 0, costo_envio REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0, metodo_pago TEXT, direccion_entrega TEXT, estado TEXT NOT NULL DEFAULT 'Pendiente', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (cliente_id) REFERENCES clientes(id));
        CREATE TABLE IF NOT EXISTS detalle_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, producto_id INTEGER NOT NULL, cantidad INTEGER NOT NULL CHECK(cantidad > 0), precio_unitario REAL NOT NULL, subtotal REAL NOT NULL, FOREIGN KEY (pedido_id) REFERENCES pedidos(id), FOREIGN KEY (producto_id) REFERENCES productos(id));
        CREATE TABLE IF NOT EXISTS mensajes (id INTEGER PRIMARY KEY AUTOINCREMENT, telefono TEXT, remitente TEXT NOT NULL CHECK(remitente IN ('usuario', 'bot', 'asesor')), contenido TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_productos_source_id ON productos(source_id);
    """)

def generar_productos(cantidad=5000):
    productos = []
    for i in range(cantidad):
        categoria = random.choice(list(CATEGORIAS.keys()))
        data_cat = CATEGORIAS[categoria]
        marca = random.choice(data_cat["marcas"])
        
        # Armar un nombre realista
        serie = random.randint(1, 99)
        sufijo = random.choice(SUFIJOS) if random.random() > 0.5 else ""
        almacenamiento = f" {random.choice(ALMACENAMIENTOS)}" if categoria in ["Celulares", "Tablets", "Computadores"] else ""
        color = f" {random.choice(COLORES)}" if random.random() > 0.3 else ""
        
        modelo = f"{serie} {sufijo}".strip()
        nombre = f"{categoria} {marca} {modelo}{almacenamiento}{color}".strip().upper()
        descripcion = f"Excelente {categoria.lower()} de marca {marca}. Rendimiento superior y diseño moderno."
        
        # Redondear el precio a miles
        precio_base = random.uniform(data_cat["precio_min"], data_cat["precio_max"])
        precio = round(precio_base / 1000) * 1000
        
        # Simular stock realista (15% agotado, 85% con stock)
        stock = 0 if random.random() < 0.15 else random.randint(1, 50)
        source_id = f"TEST-{uuid.uuid4().hex[:16].upper()}"

        productos.append((source_id, nombre, categoria, marca, modelo, descripcion, precio, stock, 1))
    
    return productos

def main():
    if DATABASE_PATH.exists():
        print(f"Respaldando base de datos original en {BACKUP_PATH}...")
        shutil.copy2(DATABASE_PATH, BACKUP_PATH)
        DATABASE_PATH.unlink() # Borrar la actual para crearla de cero
        
    print("Creando base de datos masiva de prueba...")
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    
    with sqlite3.connect(DATABASE_PATH) as conn:
        crear_esquema(conn)
        productos = generar_productos(5000)
        
        print("Insertando 5,000 productos...")
        conn.executemany("""
            INSERT INTO productos (source_id, nombre, categoria, marca, modelo, descripcion, precio, stock, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, productos)
        
    print("¡Base de datos masiva generada con éxito!")
    print("Recuerda reiniciar tu servidor (python app.py) para que la IA lea el nuevo catálogo.")

if __name__ == "__main__":
    main()