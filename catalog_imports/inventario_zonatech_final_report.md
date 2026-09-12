# Reporte del CSV final de importación — Zonatech

## Resumen

- Registros totales del CSV final: 488
- Productos importables: 488
- Servicio técnico excluido: SERVICIO TECNICO (1 registro).
- Productos con stock 0: 58
- Productos con precio 0 o vacío: 0
- Categorías vacías: 0
- source_id duplicados: 0
- Nombres duplicados exactos: 0

## Reglas aplicadas

- No se modificaron los nombres comerciales originales.
- No se fusionó ningún producto.
- Se conservaron productos con stock 0.
- No se incluyeron campos de costo.
- marca, modelo e imagen_url se dejaron vacíos porque el Excel no los proporcionaba.
- Todos los productos tienen activo = 1.
- Los source_id se generaron con el prefijo ZT-INV- y un hash SHA-256 truncado del nombre original normalizado (Unicode FormKC, mayúsculas y recorte de extremos). No usan fila, precio, stock ni categoría.
- No se ejecutó import_catalog_csv.py ni se modificó SQLite.

## Categorías asignadas desde PENDIENTE_REVISION

- BASE EPIK NOTEBOOK-LAPTOP → ACCESORIOS COMPUTADORES
- BLUETOOTH CAR X6 → BLUETOOTH
- CABLE HDMI 5 MTS → CABLES
- CARGADOR MOVISUN USB-IPH 2.4 MC-50 → CARGADORES
- HOLDER EPIK OPTIMUN → ACCESORIOS
- SOPORTE LAPTOP STAND → ACCESORIOS COMPUTADORES

## Grupos ambiguos conservados por separado

- CABLE VIDVIE USB C 4024T — ZT-INV-4D5A9C292D6F1B94619A
- CABLE VIDVIE USB-C 4024T — ZT-INV-FF2A8B76CB7CD4DED9C7

- REDMI NOTE 15 PRO 256GB + PARLANTE — ZT-INV-C847A3228130783ACD48
- REDMI NOTE 15 PRO 256GB +PARLANTE — ZT-INV-4E6829024F8F1639AA60

- SAMSUNG A07 128GB /4RAM — ZT-INV-47F62B5039CDEAB3CEE6
- SAMSUNG A07 128GB/4RAM — ZT-INV-5916DCEFED9E256B88B2

## Validaciones

- El CSV contiene exactamente estas columnas: source_id, nombre, categoria, marca, modelo, descripcion, precio, stock, imagen_url, activo
- Cada producto conserva una identidad estable si cambian únicamente precio o stock.
- Las variantes ambiguas reciben IDs distintos porque la puntuación y el espaciado interno del nombre se conservan en la identidad normalizada.
- El archivo está preparado para una importación idempotente por source_id, una vez sea aprobada.
