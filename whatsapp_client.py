"""Integración con la WhatsApp Cloud API (Meta), directa, sin intermediarios.

Responsabilidades de este módulo, y solo estas:
- Normalizar el número entre el formato que entrega Meta (wa_id, E.164 sin
  '+') y el formato de 10 dígitos que ya usa el resto del sistema
  (clientes.telefono). Colombia únicamente: prefijo de país fijo '57'.
- Verificar la firma HMAC de cada request entrante del webhook, para
  confirmar que realmente viene de Meta.
- Enviar mensajes salientes (texto y listas interactivas) a la API de Meta.

Lo que este módulo NO hace: no decide qué responder (eso es chatbot.py), no
toca la cola ni la base de datos (eso es database.py / el worker). Mantiene
la integración con Meta aislada del resto de la lógica de negocio.
"""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

COUNTRY_CODE = "57"  # Colombia. Si algún día la tienda opera en más países,
                      # esta es la única constante que dejaría de ser válida.
GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class WhatsAppConfigError(RuntimeError):
    """Falta una variable de entorno requerida para hablar con Meta."""


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise WhatsAppConfigError(f"Falta la variable de entorno {name}.")
    return value


# --- Normalización de teléfono (Colombia) -----------------------------------

def wa_id_to_local_phone(wa_id):
    """Convierte un wa_id de Meta ('573001234567') al teléfono local de 10
    dígitos ('3001234567') tal como lo espera clientes.telefono. Devuelve
    None si el wa_id no tiene la forma esperada para Colombia, en vez de
    lanzar una excepción: un número mal formado no debe tumbar el webhook."""
    if not wa_id or not wa_id.isdigit():
        return None
    if not wa_id.startswith(COUNTRY_CODE):
        return None
    local = wa_id[len(COUNTRY_CODE):]
    if len(local) != 10:
        return None
    return local


def local_phone_to_wa_id(local_phone):
    """Reconstruye el wa_id con prefijo de país, únicamente para enviar
    mensajes salientes. Nunca se guarda este valor con prefijo en la base
    de datos: clientes.telefono se queda siempre en formato de 10 dígitos."""
    if not local_phone or not local_phone.isdigit() or len(local_phone) != 10:
        raise ValueError("El teléfono local debe tener exactamente 10 dígitos.")
    return COUNTRY_CODE + local_phone


# --- Verificación del webhook (GET) -----------------------------------------

def verify_webhook_challenge(mode, token, challenge):
    """Implementa el handshake de verificación que pide Meta. Devuelve el
    challenge (texto) si el modo y el token coinciden con lo configurado,
    o None si no debe verificarse (el caller responde 403 en ese caso)."""
    expected_token = _env("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and token == expected_token:
        return challenge
    return None


# --- Autenticación de mensajes entrantes (POST) -----------------------------

def is_valid_signature(raw_body, signature_header):
    """Valida X-Hub-Signature-256 contra WHATSAPP_APP_SECRET.

    raw_body: bytes exactos del body del request (no el dict ya parseado:
    la firma se calcula sobre los bytes crudos, así que hay que validarla
    ANTES de que Flask/json.loads reinterprete nada).
    signature_header: el valor del header tal como llega, forma
    'sha256=<hexdigest>'.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    app_secret = _env("WHATSAPP_APP_SECRET")
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    received = signature_header.split("=", 1)[1]
    # compare_digest evita timing attacks al comparar la firma.
    return hmac.compare_digest(expected, received)


# --- Envío de mensajes salientes ---------------------------------------------

def _post_to_graph_api(payload):
    phone_number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    access_token = _env("WHATSAPP_ACCESS_TOKEN")
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Meta respondió {error.code} al enviar mensaje: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"No se pudo contactar la API de Meta: {error.reason}") from error


def send_text_message(to_local_phone, text):
    """Envía un mensaje de texto libre. Usado para prácticamente todo lo que
    no sea el menú principal, catálogo paginado, u opciones de audífonos."""
    payload = {
        "messaging_product": "whatsapp",
        "to": local_phone_to_wa_id(to_local_phone),
        "type": "text",
        "text": {"body": text},
    }
    return _post_to_graph_api(payload)


def send_list_message(to_local_phone, body_text, button_text, rows, header_text=None, footer_text=None):
    """Envía una lista interactiva (hasta 10 filas). Usado para el menú
    principal, catálogo paginado (10 productos por página) y las 4 opciones
    de audífonos.

    rows: lista de dicts {"id": str, "title": str, "description": str?}.
    El "id" es lo que Meta te devuelve cuando el usuario toca una fila; el
    chatbot debe poder interpretar ese id igual que interpretaría el texto
    libre equivalente (regla del punto 4: todo interactivo tiene un
    equivalente en texto libre, nunca al revés).
    """
    if not rows:
        raise ValueError("Una lista interactiva necesita al menos una fila.")
    if len(rows) > 10:
        raise ValueError("WhatsApp permite máximo 10 filas por lista.")

    interactive = {
        "type": "list",
        "body": {"text": body_text},
        "action": {
            "button": button_text,
            "sections": [{"rows": rows}],
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "to": local_phone_to_wa_id(to_local_phone),
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_to_graph_api(payload)


def send_reply_buttons(to_local_phone, body_text, buttons):
    """Envía hasta 3 botones de respuesta rápida. buttons: lista de dicts
    {"id": str, "title": str} (title máx. 20 caracteres, límite de Meta)."""
    if not buttons:
        raise ValueError("Un mensaje de botones necesita al menos uno.")
    if len(buttons) > 3:
        raise ValueError("WhatsApp permite máximo 3 botones de respuesta rápida.")

    payload = {
        "messaging_product": "whatsapp",
        "to": local_phone_to_wa_id(to_local_phone),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button["id"], "title": button["title"]}}
                    for button in buttons
                ]
            },
        },
    }
    return _post_to_graph_api(payload)


# --- Extracción de datos del payload entrante --------------------------------

def extract_incoming_message(payload_dict):
    """Recibe el JSON del webhook ya deserializado (esto sí ocurre en el
    worker, no en la cola: la cola guarda el string crudo tal cual) y
    extrae lo mínimo necesario: wa_id del remitente, texto del mensaje (ya
    sea texto libre o el id de una fila/botón tocado), y el id de mensaje
    de Meta (útil para idempotencia/depuración).

    Devuelve None si el payload no contiene un mensaje de usuario procesable
    (ej. es una notificación de estado de entrega, no un mensaje entrante).
    """
    try:
        entry = payload_dict["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None  # ej. eventos de "status" (entregado/leído), no mensajes.
        message = messages[0]
        wa_id = message.get("from")
        message_id = message.get("id")

        message_type = message.get("type")
        if message_type == "text":
            text = message["text"]["body"]
        elif message_type == "interactive":
            interactive = message["interactive"]
            if interactive.get("type") == "list_reply":
                text = interactive["list_reply"]["id"]
            elif interactive.get("type") == "button_reply":
                text = interactive["button_reply"]["id"]
            else:
                text = ""
        else:
            # Audio, imagen, ubicación, etc. — fuera de alcance; se trata
            # como texto vacío para que el bot responda con su mensaje
            # estándar de "no entendí", sin romper nada.
            text = ""

        return {"wa_id": wa_id, "message_id": message_id, "text": text}
    except (KeyError, IndexError, TypeError):
        return None