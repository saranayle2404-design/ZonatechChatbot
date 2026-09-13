"""Importa y normaliza productos desde CSV UTF-8 a SQLite.

Uso:
    python import_catalog_csv.py ruta\\catalogo.csv

Para desactivar productos que no estén en el CSV:
    python import_catalog_csv.py ruta\\catalogo.csv --deactivate-missing
"""

import csv
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from database import get_connection, initialize_database


REQUIRED_COLUMNS = {
    "source_id",
    "nombre",
    "categoria",
    "marca",
    "modelo",
    "descripcion",
    "precio",
    "stock",
    "imagen_url",
    "activo",
}


# Categorías que sí queremos conservar como categorías oficiales.
CATEGORY_ALIASES = {
    "celulares": "Celulares",
    "celular": "Celulares",

    "computadores": "Computadores",
    "computador": "Computadores",
    "computadoras": "Computadores",
    "portatiles": "Computadores",

    "audifonos": "Audífonos",
    "audifono": "Audífonos",
    "diademas": "Audífonos",
    "diadema": "Audífonos",

    "parlantes": "Parlantes",
    "parlante": "Parlantes",

    "cargadores": "Cargadores",
    "cargador": "Cargadores",

    "cables": "Cables",
    "cable": "Cables",

    "accesorios": "Accesorios",
    "accesorio": "Accesorios",
    "accesorios computadores": "Accesorios Computadores",
    "accesorio computadores": "Accesorios Computadores",
    "internet": "Internet",

    "forros": "Forros",
    "forro": "Forros",

    "vidrios": "Vidrios",
    "vidrio": "Vidrios",

    "hidrogel": "Hidrogel",

    "memorias": "Memorias",
    "memoria": "Memorias",

    "smart watch": "Smartwatch",
    "smartwatch": "Smartwatch",

    "adaptadores": "Adaptadores",
    "adaptador": "Adaptadores",

    "mouse": "Mouse",
    "teclados": "Teclados",
    "controles": "Controles",
    "discos": "Discos",
}


# Marcas que podemos identificar de forma razonablemente segura.
BRAND_PATTERNS = [
    ("Samsung", r"\bsamsung\b"),
    ("Xiaomi", r"\bxiaomi\b"),
    ("Redmi", r"\bredmi\b"),
    ("Motorola", r"\bmotorola\b"),
    ("Apple", r"\bapple\b|\biphone\b|\bairpods?\b"),
    ("JBL", r"\bjbl\b"),
    ("Vidvie", r"\bvidvie\b"),
    ("Movisun", r"\bmovisun\b"),
    ("Epik", r"\bepik\b"),
    ("Jedel", r"\bjedel\b"),
    ("GTIDE", r"\bgtide\b"),
    ("Tecno", r"\btecno\b"),
    ("Vivo", r"\bvivo\b"),
    ("Huawei", r"\bhuawei\b"),
    ("Oppo", r"\boppo\b"),
    ("Realme", r"\brealme\b"),
    ("Lenovo", r"\blenovo\b"),
    ("HP", r"\bhp\b"),
    ("Dell", r"\bdell\b"),
    ("Asus", r"\basus\b"),
    ("Acer", r"\bacer\b"),
]


# Correcciones ortográficas que detectamos en el inventario real.
TYPO_CORRECTIONS = {
    "adaprador": "adaptador",
    "bluetoht": "bluetooth",
    "bluetooht": "bluetooth",
    "bluetooot": "bluetooth",
    "bluetoot": "bluetooth",
    "lightihing": "lightning",
    "lightining": "lightning",
    "holdel": "holder",
    "diademar": "diadema",
    "power ban": "power bank",
}


def clean_text(value):
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def strip_accents(value):
    normalized = unicodedata.normalize("NFD", str(value or ""))
    return "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )


def normalize_key(value):
    value = clean_text(value)
    value = strip_accents(value).lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def correct_typos(value):
    text = clean_text(value)

    for wrong, correct in TYPO_CORRECTIONS.items():
        text = re.sub(
            rf"\b{re.escape(wrong)}\b",
            correct,
            text,
            flags=re.IGNORECASE,
        )

    return text
def detect_brand(name, description, current_brand, current_category):
    current_brand = clean_text(current_brand)

    # Si el CSV ya trae una marca válida, la conservamos.
    if current_brand:
        normalized_current = normalize_key(current_brand)

        for brand, _pattern in BRAND_PATTERNS:
            if normalize_key(brand) == normalized_current:
                return brand

        return current_brand

    product_evidence = strip_accents(" ".join((name, description)))
    category_evidence = strip_accents(current_category)

    for brand, pattern in BRAND_PATTERNS:
        if re.search(pattern, product_evidence, re.IGNORECASE):
            return brand

    for brand, pattern in BRAND_PATTERNS:
        if re.search(pattern, category_evidence, re.IGNORECASE):
            return brand

    return ""


def detect_category(name, description, current_category):
    name_key = normalize_key(" ".join((name, description)))
    category_key = normalize_key(current_category)

    # Parlantes deben prevalecer sobre la regla generica de Bluetooth.
    if re.search(
        r"\b(parlante|parlantes|speaker|speakers|altavoz|altavoces)\b",
        name_key,
    ):
        return "Parlantes"

    # ============================================================
    # CELULARES
    # Prioridad alta: evita que productos como
    # "REDMI NOTE 15 PRO + BUDS" sean clasificados como Audífonos.
    # ============================================================
    if (
        not re.search(
            r"\b(cable|cables|lightning|usb[- ]?c|type[- ]?c|vga|hdmi|"
            r"cargador|cargadores|charger|cabeza|forro|forros|funda|fundas|"
            r"case|vidrio|vidrios|templado|protector|hidrogel)\b",
            name_key,
        )
        and re.search(
        r"\b("
        r"iphone\s+(?:\d+|se\b)|samsung\s+(?:a|s|m)\d+|"
        r"galaxy\s+(?:a|s|m|z)\d+|redmi\s+(?:note|\d)|"
        r"motorola|moto\s+(?:g|e|edge)|huawei\s+\w+|"
        r"tecno\s+\w+|vivo\s+\w+|oppo\s+\w+|realme\s+\w+"
        r")\b",
        name_key,
        )
    ):
        return "Celulares"

    # ============================================================
    # AUDÍFONOS
    # ============================================================
    if re.search(
        r"\b("
        r"audifono|audifonos|earbud|earbuds|"
        r"airpods?|buds\d*|diadema|diademas|"
        r"headphone|headphones|headset|manos libres|"
        r"cuellera|neck band|air conduction"
        r")\b",
        name_key,
    ):
        return "Audífonos"

    if "bluetooth" in name_key and not re.search(
        r"\b(?:car|carro|usb)\b",
        name_key,
    ):
        return "Audífonos"

    # Parlantes.
    if re.search(
        r"\b(parlante|parlantes|speaker)\b",
        name_key,
    ):
        return "Parlantes"

    # Cargadores.
    if re.search(
        r"\b(cargador|cargadores|charger|cabeza|car carro)\b",
        name_key,
    ):
        return "Cargadores"

    # ============================================================
    # MOUSE
    # Prioridad antes de Cables porque un mouse puede tener
    # palabras como "cable", "USB" u "optical".
    # ============================================================
    if re.search(
        r"\b(mouse|pad mouse)\b",
        name_key,
    ):
        return "Mouse"

    # Cables.
    if re.search(
        r"\b(cable|cables|lightning|usb[- ]?c|type[- ]?c|vga|hdmi)\b",
        name_key,
    ):
        return "Cables"

    # Forros / fundas.
    if re.search(
        r"\b(forro|forros|funda|fundas|case)\b",
        name_key,
    ):
        return "Forros"

    # Vidrios.
    if re.search(
        r"\b(vidrio|vidrios|templado|protector)\b",
        name_key,
    ):
        return "Vidrios"

    # Hidrogel.
    if "hidrogel" in name_key:
        return "Hidrogel"

    # Memorias.
    if re.search(
        r"\b(memoria|memorias|micro\s*sd|sd card)\b",
        name_key,
    ):
        return "Memorias"

    # Smartwatch.
    if re.search(
        r"\b(smart\s*watch|smartwatch|reloj|smart band|pulso)\b",
        name_key,
    ):
        return "Smartwatch"

    # Adaptadores.
    if re.search(
        r"\b(adaptador|adaptadores|adapter)\b",
        name_key,
    ):
        return "Adaptadores"

    # Teclados.
    if re.search(
        r"\b(teclado|combo teclado)\b",
        name_key,
    ):
        return "Teclados"

    # Controles.
    if re.search(
        r"\b(control|joystick)\b",
        name_key,
    ):
        return "Controles"

    # Discos.
    if re.search(
        r"\b(disco|disco duro)\b",
        name_key,
    ):
        return "Discos"

    # Computadores / accesorios de computador.
    if re.search(
        r"\b(laptop|notebook|portatil|macbook|computador|impresora)\b",
        name_key,
    ):
        return "Accesorios Computadores"

    # Accesorios.
    if re.search(
        r"\b(holder|holdel|soporte|stand|tripode|aro luz|"
        r"palo selfie|camara web|webcam|power bank|power band|"
        r"multipuerto|hub|usb bluetooth)\b",
        name_key,
    ):
        return "Accesorios"

    # Si no podemos determinarla con seguridad,
    # usamos la categoría original.
    if category_key in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[category_key]

    return "Accesorios"


def extract_model(name, brand, current_model):
    current_model = clean_text(current_model)

    # Si el CSV ya trae modelo, no lo destruimos.
    if current_model:
        return current_model

    model = clean_text(name)

    # Quitamos la marca del comienzo del nombre.
    if brand:
        model = re.sub(
            rf"\b{re.escape(brand)}\b",
            "",
            model,
            flags=re.IGNORECASE,
        )

    # Quitamos prefijos de categoría solamente al inicio.
    model = re.sub(
        r"^(audifonos?|diademas?|celulares?|parlantes?|"
        r"cargadores?|cables?|forros?|vidrios?|memorias?|"
        r"adaptadores?)\s+",
        "",
        model,
        flags=re.IGNORECASE,
    )

    model = re.sub(r"\s+", " ", model).strip(" -/")

    return model


def normalize_product(row):
    source_id = clean_text(row.get("source_id"))
    name = correct_typos(clean_text(row.get("nombre")))
    original_category = correct_typos(clean_text(row.get("categoria")))
    original_brand = correct_typos(clean_text(row.get("marca")))
    original_model = correct_typos(clean_text(row.get("modelo")))

    if not source_id:
        raise ValueError("Cada producto necesita source_id.")

    if not name:
        raise ValueError(
            f"El producto {source_id} no tiene nombre."
        )

    brand = detect_brand(
        name=name,
        description=clean_text(row.get("descripcion")),
        current_brand=original_brand,
        current_category=original_category,
    )

    category = detect_category(
        name=name,
        description=clean_text(row.get("descripcion")),
        current_category=original_category,
    )

    model = extract_model(
        name=name,
        brand=brand,
        current_model=original_model,
    )

    precio_raw = clean_text(row.get("precio"))
    stock_raw = clean_text(row.get("stock"))
    activo_raw = clean_text(row.get("activo"))

    try:
        precio = float(precio_raw) if precio_raw else None
    except ValueError as exc:
        raise ValueError(
            f"Precio inválido en {source_id}: {precio_raw}"
        ) from exc

    try:
        stock = int(stock_raw or 0)
    except ValueError as exc:
        raise ValueError(
            f"Stock inválido en {source_id}: {stock_raw}"
        ) from exc

    try:
        activo = int(activo_raw or 1)
    except ValueError as exc:
        raise ValueError(
            f"Activo inválido en {source_id}: {activo_raw}"
        ) from exc

    if precio is not None and precio < 0:
        raise ValueError(
            f"Precio negativo en {source_id}."
        )

    if stock < 0:
        raise ValueError(
            f"Stock negativo en {source_id}."
        )

    if activo not in {0, 1}:
        raise ValueError(
            f"Activo debe ser 0 o 1 en {source_id}."
        )

    return {
        "source_id": source_id,
        "nombre": name,
        "categoria": category,
        "marca": brand,
        "modelo": model,
        "descripcion": clean_text(row.get("descripcion")),
        "precio": precio,
        "stock": stock,
        "imagen_url": clean_text(row.get("imagen_url")),
        "activo": activo,
    }


def backup_database():
    database_path = Path("database") / "zonatech.db"

    if not database_path.exists():
        return None

    backup_path = Path("database") / "zonatech_before_normalization_v2.db"
    shutil.copy2(database_path, backup_path)

    return backup_path


def import_csv(path, deactivate_missing=False):
    csv_path = Path(path)

    if not csv_path.exists():
        raise FileNotFoundError(
            f"No existe el archivo CSV: {csv_path}"
        )

    backup_path = backup_database()
    initialize_database()

    with open(
        csv_path,
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(file)

        missing_columns = REQUIRED_COLUMNS - set(
            reader.fieldnames or []
        )

        if missing_columns:
            raise ValueError(
                "Faltan columnas requeridas: "
                + ", ".join(sorted(missing_columns))
            )

        rows = list(reader)

    seen_ids = set()
    normalized_rows = []

    for row in rows:
        source_id = clean_text(row.get("source_id"))

        if source_id in seen_ids:
            raise ValueError(
                f"source_id duplicado dentro del CSV: {source_id}"
            )

        seen_ids.add(source_id)

        normalized_rows.append(
            normalize_product(row)
        )

    with get_connection() as connection:

        # ========================================================
        # PROTECCIÓN DE --deactivate-missing
        #
        # Se valida ANTES de cualquier UPSERT.
        # Si el CSV está incompleto, no se modifica ningún
        # producto de la base de datos.
        # ========================================================
        if deactivate_missing:

            if not seen_ids:
                raise ValueError(
                    "No se puede desactivar productos con un CSV vacío."
                )

            active_rows = connection.execute(
                """
                SELECT source_id
                FROM productos
                WHERE activo=1
                AND source_id IS NOT NULL
                """
            ).fetchall()

            active_source_ids = {
                row["source_id"]
                for row in active_rows
            }

            missing_active_ids = active_source_ids - seen_ids

            if missing_active_ids:
                sample = sorted(missing_active_ids)[:10]

                raise ValueError(
                    "CSV incompleto: faltan "
                    f"{len(missing_active_ids)} source_id activos. "
                    "No se desactivó ningún producto. "
                    f"Ejemplos: {', '.join(sample)}"
                )

        # ========================================================
        # UPSERT DEL CATÁLOGO
        # ========================================================
        for row in normalized_rows:

            connection.execute(
                """
                INSERT INTO productos(
                    source_id,
                    nombre,
                    categoria,
                    marca,
                    modelo,
                    descripcion,
                    precio,
                    stock,
                    imagen_url,
                    activo
                )
                VALUES(
                    :source_id,
                    :nombre,
                    :categoria,
                    :marca,
                    :modelo,
                    :descripcion,
                    :precio,
                    :stock,
                    :imagen_url,
                    :activo
                )
                ON CONFLICT(source_id) DO UPDATE SET
                    nombre=excluded.nombre,
                    categoria=excluded.categoria,
                    marca=excluded.marca,
                    modelo=excluded.modelo,
                    descripcion=excluded.descripcion,
                    precio=excluded.precio,
                    stock=excluded.stock,
                    imagen_url=excluded.imagen_url,
                    activo=excluded.activo,
                    updated_at=CURRENT_TIMESTAMP
                """,
                row,
            )

        # ========================================================
        # DESACTIVAR SOLO SI EL CSV ESTÁ COMPLETO
        # ========================================================
        if deactivate_missing:

            placeholders = ",".join(
                "?" for _ in seen_ids
            )

            connection.execute(
                f"""
                UPDATE productos
                SET activo=0,
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id IS NOT NULL
                AND source_id NOT IN ({placeholders})
                """,
                list(seen_ids),
            )

    print()
    print("==============================================")
    print(" CATÁLOGO NORMALIZADO CORRECTAMENTE")
    print("==============================================")
    print(f"Productos procesados: {len(normalized_rows)}")

    if backup_path:
        print(f"Backup creado: {backup_path}")

    print()
    return len(normalized_rows)


if __name__ == "__main__":

    valid_arguments = (
        len(sys.argv) in {2, 3}
        and (
            len(sys.argv) == 2
            or sys.argv[2] == "--deactivate-missing"
        )
    )

    if not valid_arguments:
        raise SystemExit(
            "Uso: python import_catalog_csv.py "
            "ruta\\catalogo.csv [--deactivate-missing]"
        )

    count = import_csv(
        sys.argv[1],
        deactivate_missing=len(sys.argv) == 3,
    )

    print(
        f"Catálogo actualizado: {count} producto(s) procesado(s)."
    )
