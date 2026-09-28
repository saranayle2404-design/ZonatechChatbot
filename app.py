import json
import os

from flask import Flask, Response, jsonify, request
from flask_limiter import Limiter

import whatsapp_client
from database import enqueue_message, initialize_database
from semantic_search import warmup


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    # Límite duro de tamaño de request. Los payloads de WhatsApp para un
    # mensaje de texto o una interacción de lista/botón son pequeños, pero
    # dejamos margen razonable (64 KB) por la metadata que Meta agrega a
    # cada evento del webhook.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    # Ya no hay sesión de navegador ni cookie que firmar: WhatsApp identifica
    # al remitente por wa_id en cada mensaje, y ese wa_id viene dentro de un
    # payload cuya firma se valida contra WHATSAPP_APP_SECRET (ver
    # whatsapp_client.is_valid_signature). No aplica app.secret_key,
    # SESSION_COOKIE_*, ni PERMANENT_SESSION_LIFETIME.

    # Rate limiting por wa_id (número del remitente), no por IP: todo el
    # tráfico legítimo llega desde las IPs de Meta, así que limitar por IP
    # ya no protege nada (por eso se retira get_remote_address). Esta capa
    # es distinta de la validación de firma (que es autenticación): aquí se
    # protege contra que un número específico sature el procesamiento,
    # aunque sus mensajes sean legítimos.
    #
    # storage_uri="memory://" solo es válido con UN proceso, igual que en la
    # versión web: con varios workers (gunicorn -w N) usar Redis
    # (storage_uri="redis://localhost:6379").
    limiter = Limiter(
        key_func=_rate_limit_key,
        app=app,
        storage_uri="memory://",
        default_limits=[],
    )

    initialize_database()

    # Precarga el modelo de búsqueda semántica y los embeddings del catálogo
    # ANTES de exponer el webhook, para que el primer mensaje real no pague
    # el costo de esa carga inicial.
    warmup()

    @app.get("/webhook")
    def verify_webhook():
        # Handshake de verificación que exige Meta al configurar (o
        # re-verificar) el webhook. Queda siempre activo, no solo durante el
        # setup inicial, por si Meta necesita re-verificar más adelante
        # (ej. cambio de URL).
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        try:
            result = whatsapp_client.verify_webhook_challenge(mode, token, challenge)
        except whatsapp_client.WhatsAppConfigError:
            # Falta configurar WHATSAPP_VERIFY_TOKEN en el entorno: no es
            # culpa de quien llama, pero tampoco se revela el motivo exacto.
            return Response(status=403)

        if result is None:
            return Response(status=403)
        return Response(result, status=200, mimetype="text/plain")

    @app.post("/webhook")
    @limiter.limit("20 per minute")
    def receive_webhook():
        # La firma se valida sobre los BYTES CRUDOS del body, antes de que
        # Flask reinterprete nada como JSON. Si no coincide, no se procesa
        # ni se loggea el contenido del mensaje: solo se rechaza.
        raw_body = request.get_data()
        signature = request.headers.get("X-Hub-Signature-256")

        try:
            valid = whatsapp_client.is_valid_signature(raw_body, signature)
        except whatsapp_client.WhatsAppConfigError:
            return Response(status=403)

        if not valid:
            return Response(status=403)

        # La cola guarda el payload crudo tal cual llega (ver database.py):
        # esta capa no decide qué significa el mensaje, solo lo persiste
        # para que el worker lo procese aparte. Esto es lo que permite
        # responder 200 a Meta de inmediato y evitar reintentos/duplicados
        # por timeout.
        try:
            payload_text = raw_body.decode("utf-8")
            json.loads(payload_text)  # valida que sea JSON antes de encolar
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Body inválido pese a traer firma correcta: no hay nada
            # procesable que encolar, pero tampoco es un fallo del cliente
            # (Meta) en sí. Se responde 200 igual para no generar
            # reintentos infinitos de un payload que nunca será válido.
            return Response(status=200)

        enqueue_message(payload_text)
        return Response(status=200)

    @app.errorhandler(413)
    def payload_too_large(error):
        return jsonify({"error": "Payload demasiado grande."}), 413

    @app.errorhandler(429)
    def rate_limited(error):
        # Nunca se expone que existe un rate limiter ni el motivo exacto.
        # Se responde 200 (no 500, no un error visible) para no generar
        # reintentos de Meta ni exponer detalles internos; el límite es
        # generoso para una conversación humana normal.
        return Response(status=200)

    return app


def _rate_limit_key():
    """Clave de rate limiting: el wa_id del remitente, extraído del cuerpo
    del webhook. Si el payload no trae un wa_id identificable (formato
    inesperado, evento de status en vez de mensaje, etc.), se usa un cubo
    compartido "desconocido" en vez de fallar — nunca debe tumbar el
    request por esto; en el peor caso ese cubo se satura entre varios
    remitentes no identificados, lo cual es una degradación aceptable.
    """
    payload = request.get_json(silent=True) or {}
    extracted = whatsapp_client.extract_incoming_message(payload)
    if extracted and extracted.get("wa_id"):
        return extracted["wa_id"]
    return "desconocido"


app = create_app()


if __name__ == "__main__":
    # El modo debug de Flask/Werkzeug expone un depurador interactivo que
    # permite ejecutar código arbitrario en el servidor ante cualquier
    # excepción no controlada. NUNCA debe activarse en producción.
    # Se habilita solo si se define explícitamente FLASK_DEBUG=1, y aun así
    # solo escucha en localhost para no exponer el depurador en la red.
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug_mode, host="127.0.0.1", port=5000)