# Plantilla de inventario Zonatech

Usa `zonatech_inventory_template.csv` como base. Todos los registros de ejemplo comienzan con `DEMO-`; eliminalos o reemplazalos antes de importar productos reales.

| Columna | Uso |
| --- | --- |
| `source_id` | Identificador unico y estable del producto. Nunca debe cambiar para el mismo producto. |
| `nombre` | Nombre publico del producto. |
| `categoria` | Categoria del chatbot. |
| `marca` | Marca o `Sin marca` si no existe el dato. |
| `modelo` | Modelo o `No especificado` si no existe el dato. |
| `descripcion` | Descripcion real; puede estar vacia. |
| `precio` | Numero sin `$` ni separadores; vacio si requiere cotizacion. |
| `stock` | Entero mayor o igual a cero. |
| `imagen_url` | URL de imagen o vacio. |
| `activo` | `1` para publicar o `0` para desactivar sin borrar. |

Actualiza/agrega solo los productos presentes, sin desactivar otros:

```powershell
python import_catalog_csv.py catalog_templates\zonatech_inventory_template.csv
```

Para un archivo completo que representa todo el inventario vigente, permite desactivar de forma reversible los productos ausentes:

```powershell
python import_catalog_csv.py ruta\inventario_completo.csv --deactivate-missing
```

No uses `--deactivate-missing` con archivos parciales.
