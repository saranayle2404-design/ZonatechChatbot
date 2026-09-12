"""Genera una previsualizacion CSV y Markdown desde el Excel, sin usar SQLite.

Uso:
python prepare_inventory_preview.py "C:\ruta\inventario.xlsx"
"""
import csv
import hashlib
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


OUTPUT_DIR = Path("catalog_imports")
SERVICE_NAME = "SERVICIO TECNICO"
PENDING_CATEGORY = "PENDIENTE_REVISION"
BRAND_LIKE_CATEGORIES = {"EPIK", "GTIDE", "JBL", "JEDEL", "MOVISUN", "VIDVIE", "XIAOMI"}
TYPE_LIKE_CATEGORIES = {"BLUETOOTH", "INTERNET", "PULSOS"}
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def normalize(value):
    text = unicodedata.normalize("NFD", value.strip().lower())
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]", "", text)


def column_name(cell_reference):
    return re.sub(r"\d", "", cell_reference)


def read_xlsx(path):
    with zipfile.ZipFile(path) as workbook:
        shared = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root.findall("x:si", NS)]
        root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.findall(".//x:sheetData/x:row", NS):
        values = {}
        for cell in row.findall("x:c", NS):
            value = cell.find("x:v", NS)
            content = "" if value is None else value.text or ""
            if cell.get("t") == "s" and content:
                content = shared[int(content)]
            values[column_name(cell.get("r", ""))] = content
        rows.append((int(row.get("r")), values))
    return rows


def build_preview(rows):
    products = []
    for row_number, row in rows:
        if row_number <= 8 or not row.get("C", "").strip():
            continue
        name = row["C"].strip()
        category = row.get("D", "").strip()
        if name.upper() == SERVICE_NAME or category.upper() == SERVICE_NAME:
            continue
        products.append({
            "excel_row": row_number,
            "nombre": name,
            "categoria_original": category,
            "notas": row.get("E", "").strip(),
            "stock": int(float(row.get("F", "0") or 0)),
            "precio": row.get("H", "").strip(),
        })
    duplicate_groups = defaultdict(list)
    for product in products:
        duplicate_groups[normalize(product["nombre"])].append(product)
    ambiguous_rows = {
        product["excel_row"]
        for group in duplicate_groups.values() if len(group) > 1
        for product in group
    }
    preview = []
    for product in products:
        identity = normalize(product["nombre"])
        preview.append({
            "source_id": "" if product["excel_row"] in ambiguous_rows else "PREVIEW-" + hashlib.sha256(identity.encode()).hexdigest()[:16].upper(),
            "nombre": product["nombre"],
            "categoria": product["categoria_original"] or PENDING_CATEGORY,
            "marca": "",
            "modelo": "",
            "descripcion": product["notas"],
            "precio": product["precio"],
            "stock": product["stock"],
            "imagen_url": "",
            "activo": 1,
        })
    return products, preview, duplicate_groups


def write_report(path, products, duplicate_groups):
    categories = Counter(product["categoria_original"] or PENDING_CATEGORY for product in products)
    examples = defaultdict(list)
    for product in products:
        category = product["categoria_original"] or PENDING_CATEGORY
        if len(examples[category]) < 3:
            examples[category].append(product["nombre"])
    pending = [product for product in products if not product["categoria_original"]]
    duplicates = [group for group in duplicate_groups.values() if len(group) > 1]
    lines = ["# Previsualizacion de inventario Zonatech", "", f"Productos candidatos: {len(products)}", "", "## Categorias"]
    for category, count in sorted(categories.items()):
        lines.append(f"- {category}: {count}. Ejemplos: {'; '.join(examples[category])}")
    lines += ["", "## Categoria pendiente de revision"]
    for product in pending:
        lines.append(f"- Fila {product['excel_row']}: {product['nombre']}")
    lines += ["", "## Posibles duplicados: requieren revision humana"]
    for group in duplicates:
        lines.append("- " + " | ".join(f"Fila {item['excel_row']}: {item['nombre']}" for item in group))
    lines += ["", "## Categorias que parecen marcas", "- " + ", ".join(sorted(BRAND_LIKE_CATEGORIES)), "", "## Categorias que parecen tecnologia/tipo", "- " + ", ".join(sorted(TYPE_LIKE_CATEGORIES))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(source_path):
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(source)
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "inventario_zonatech_preview.csv"
    report_path = OUTPUT_DIR / "inventario_zonatech_preview_report.md"
    if csv_path.exists() or report_path.exists():
        raise FileExistsError("Ya existe una previsualizacion. Eliminala manualmente si quieres regenerarla.")
    products, preview, duplicate_groups = build_preview(read_xlsx(source))
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=preview[0].keys())
        writer.writeheader()
        writer.writerows(preview)
    write_report(report_path, products, duplicate_groups)
    print(f"Preview CSV: {csv_path}")
    print(f"Reporte: {report_path}")
    print("SQLite no fue modificada.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python prepare_inventory_preview.py ruta\\inventario.xlsx")
    main(sys.argv[1])
