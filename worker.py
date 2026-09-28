"""Worker de procesamiento de mensajes de WhatsApp — proceso separado de app.py.

Responsabilidad única: tomar mensajes 'pendientes' de cola_mensajes (que
app.py llenó con el payload crudo del webhook), interpretarlos, pasarlos por
chatbot.process_message, y enviar la respuesta de vuelta por la API de Meta.

Se corre aparte del webhook a propósito (decisión del punto 3c): así un
problema en el procesamiento (ej. process_message tarda, o falla el envío a
Meta) nunca afecta la disponibilidad de app.py, que es lo único que Meta
espera que responda rápido.

Uso:
    python worker.py
"""
import json
import time

import database
import whatsapp_client
from chatbot import process_message

POLL_INTERVAL_SECONDS = 1  # cuánto espera el worker entre revisiones cuando la cola está vacía.

# WhatsApp: listas interactivas permiten 1-10 filas. Cuando quick_replies
# trae más de 10 opciones (ej. catalog_menu() con las ~25 categorías reales
# del catálogo), no truncamos ni mostramos una versión parcial engañosa:
# se envía solo el texto, que ya contiene toda la información, y el usuario
# responde escribiendo — el mismo camino que siempre está disponible
# (decisión A, punto 4).
MAX_LIST_ROWS = 10
LIST_ROW_TITLE_MAX_CHARS = 24  # límite real de WhatsApp para el título de una fila.


def _build_list_rows(quick_replies):
    """Convierte los quick_replies de chatbot.py (strings libres) en filas
    de lista interactiva. El id de cada fila es el mismo texto que el
    usuario escribiría a mano, así el worker no necesita un mapeo aparte:
    tocar la fila "🔧 Reparar mi equipo" hace exactamente lo mismo que
    escribir "🔧 Reparar mi equipo" (process_message no distingue el
    origen)."""
    rows = []
    for option in quick_replies:
        title = option if len(option) <= LIST_ROW_TITLE_MAX_CHARS else option[: LIST_ROW_TITLE_MAX_CHARS - 1] + "…"
        rows.append({"id": option, "title": title})
    return rows


def send_bot_reply(local_phone, reply_payload):
    """Envía la respuesta de process_message por WhatsApp, eligiendo lista
    interactiva o texto plano según cuántas quick_replies haya (regla A)."""
    text = reply_payload.get("reply", "")
    quick_replies = reply_payload.get("quick_replies") or []

    if 1 <= len(quick_replies) <= MAX_LIST_ROWS:
        whatsapp_client.send_list_message(
            local_phone,
            body_text=text,
            button_text="Ver opciones",
            rows=_build_list_rows(quick_replies),
        )
    else:
        # 0 opciones, o más de las que caben en una lista: solo texto. El
        # texto de `reply` ya contiene toda la información (categorías,
        # productos numerados, etc.), así que no se pierde nada.
        whatsapp_client.send_text_message(local_phone, text)


def process_one_message(row):
    """Procesa una fila tomada de cola_mensajes. Deja que cualquier
    excepción se propague: el caller (main loop) decide qué hacer con el
    error (reintento vía mark_message_error)."""
    payload_dict = json.loads(row["payload"])
    incoming = whatsapp_client.extract_incoming_message(payload_dict)

    if incoming is None:
        # No es un mensaje de usuario procesable (ej. un evento de status
        # "entregado"/"leído"). No hay nada que responder; se marca
        # procesado y se sigue, sin reintentar indefinidamente algo que
        # nunca va a convertirse en un mensaje.
        database.mark_message_processed(row["id"])
        return

    wa_id = incoming["wa_id"]
    text = incoming["text"]
    local_phone = whatsapp_client.wa_id_to_local_phone(wa_id)

    if local_phone is None:
        # wa_id que no corresponde a un celular colombiano de 10 dígitos
        # (punto 6.1). No es un fallo transitorio -- reintentar no lo va a
        # arreglar -- así que se marca procesado (no error) para no
        # consumir los MAX_INTENTOS_COLA reintentos en vano, pero sí queda
        # registrado en el log para poder revisarlo manualmente.
        database.log_message(wa_id, "usuario", f"[wa_id no procesable: {text!r}]")
        database.mark_message_processed(row["id"])
        return

    database.log_message(local_phone, "usuario", text)

    reply_payload = process_message(text, session_id=local_phone)

    send_bot_reply(local_phone, reply_payload)
    database.log_message(local_phone, "bot", reply_payload.get("reply", ""))

    database.mark_message_processed(row["id"])


def run_forever():
    print("Worker de WhatsApp iniciado. Esperando mensajes...")
    while True:
        row = database.claim_next_pending_message()
        if row is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        try:
            process_one_message(row)
        except Exception as error:  # noqa: BLE001 — el worker nunca debe morir por un mensaje individual.
            # Cualquier fallo (JSON corrupto, error de red al enviar a Meta,
            # excepción dentro de process_message, etc.) se registra y el
            # mensaje vuelve a 'pendiente' para reintento automático (hasta
            # MAX_INTENTOS_COLA), en vez de tumbar el worker completo.
            print(f"[worker] Error procesando mensaje id={row['id']}: {error}")
            database.mark_message_error(row["id"], error)


if __name__ == "__main__":
    database.initialize_database()
    run_forever()