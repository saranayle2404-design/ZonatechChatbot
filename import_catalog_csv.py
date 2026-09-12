"""Importa o actualiza productos desde CSV UTF-8 a SQLite.

Uso: python import_catalog_csv.py ruta\\catalogo.csv [--deactivate-missing]
"""
import csv
import sys

from database import get_connection, initialize_database

REQUIRED_COLUMNS = {
    "source_id", "nombre", "categoria", "marca", "modelo", "descripcion",
    "precio", "stock", "imagen_url", "activo",
}


def import_csv(path, deactivate_missing=False):
    """Upserts by source_id. Optional deactivation is for complete files only."""
    initialize_database()
    with open(path, encoding="utf-8-sig", newline="") as file, get_connection() as connection:
        reader = csv.DictReader(file)
        missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError("Faltan columnas requeridas: " + ", ".join(sorted(missing_columns)))
        imported_ids = []
        for row in reader:
            if not row.get("source_id") or not row.get("nombre") or not row.get("categoria"):
                raise ValueError("Cada fila necesita source_id, nombre y categoria.")
            row["precio"] = float(row["precio"]) if row.get("precio") else None
            row["stock"] = int(row.get("stock") or 0)
            row["activo"] = int(row.get("activo") or 1)
            imported_ids.append(row["source_id"])
            connection.execute("""INSERT INTO productos(source_id,nombre,categoria,marca,modelo,descripcion,precio,stock,imagen_url,activo)
                VALUES(:source_id,:nombre,:categoria,:marca,:modelo,:descripcion,:precio,:stock,:imagen_url,:activo)
                ON CONFLICT(source_id) DO UPDATE SET nombre=excluded.nombre,categoria=excluded.categoria,marca=excluded.marca,modelo=excluded.modelo,descripcion=excluded.descripcion,precio=excluded.precio,stock=excluded.stock,imagen_url=excluded.imagen_url,activo=excluded.activo,updated_at=CURRENT_TIMESTAMP""", row)
        if deactivate_missing:
            if not imported_ids:
                raise ValueError("No se puede desactivar productos con un CSV vacio.")
            placeholders = ",".join("?" for _ in imported_ids)
            connection.execute(
                f"UPDATE productos SET activo=0,updated_at=CURRENT_TIMESTAMP "
                f"WHERE source_id IS NOT NULL AND source_id NOT IN ({placeholders})",
                imported_ids,
            )
    return len(imported_ids)


if __name__ == "__main__":
    valid_arguments = len(sys.argv) in {2, 3} and (len(sys.argv) == 2 or sys.argv[2] == "--deactivate-missing")
    if not valid_arguments:
        raise SystemExit("Uso: python import_catalog_csv.py ruta\\catalogo.csv [--deactivate-missing]")
    count = import_csv(sys.argv[1], deactivate_missing=len(sys.argv) == 3)
    print(f"Catalogo actualizado: {count} producto(s) procesado(s).")
