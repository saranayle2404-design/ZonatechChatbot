"""Herramienta local para que Zonatech actualice reparaciones antes del panel administrativo.

Ejemplos:
  python manage_repairs.py estado ZT-R-20260910-0001 "En reparacion"
  python manage_repairs.py cotizacion ZT-R-20260910-0001 85000 "Incluye repuesto" Aprobada
"""
import sys

from database import initialize_database, set_repair_quote, update_repair_status


def main(args):
    initialize_database()
    if len(args) == 3 and args[0] == "estado":
        update_repair_status(args[1], args[2])
        print("Estado actualizado.")
        return
    if len(args) in {4, 5} and args[0] == "cotizacion":
        decision = args[4] if len(args) == 5 else None
        set_repair_quote(args[1], float(args[2]), args[3], decision)
        print("Cotizacion actualizada.")
        return
    raise SystemExit(
        "Uso:\n"
        "python manage_repairs.py estado CODIGO \"En reparacion\"\n"
        "python manage_repairs.py cotizacion CODIGO VALOR \"Observaciones\" [Aprobada|Rechazada]"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
