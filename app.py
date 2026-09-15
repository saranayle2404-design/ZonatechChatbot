import os
import secrets
from datetime import timedelta

from flask import Flask, jsonify, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from chatbot import process_message
from database import initialize_database


MAX_MESSAGE_LENGTH = 1000  # el index.html ya limita a 500 en el input, esto es el candado real del servidor


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    # Límite duro de tamaño de request: sin esto, cualquiera puede mandar un
    # body JSON gigante para agotar memoria/CPU antes de que se valide nada.
    # 16 KB es de sobra para {"message": "..."} con un mensaje razonable.
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    # SECRET_KEY firma la cookie de sesión de Flask (itsdangerous). Debe
    # definirse de forma estable vía variable de entorno en producción: si
    # cambia (o se genera al azar en cada arranque, como aquí por defecto),
    # todas las sesiones activas quedan invalidadas al reiniciar el proceso.
    # Con múltiples workers (gunicorn -w N) TODOS deben compartir el mismo
    # SECRET_KEY, o cada worker firmará con una clave distinta.
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # JS del navegador no puede leer la cookie
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # mitiga CSRF básico
    # Detrás de HTTPS en producción, exporta SESSION_COOKIE_SECURE=1 para que
    # la cookie nunca viaje por HTTP sin cifrar.
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

    # Rate limiting por IP. storage_uri="memory://" es solo válido con UN
    # proceso: si despliegas con varios workers (gunicorn -w N) o varias
    # instancias, cada uno llevaría su propio contador y el límite real
    # efectivo se multiplica. Para producción con más de un worker, usa
    # Redis: storage_uri="redis://localhost:6379" (requiere `pip install
    # flask-limiter[redis]` y un Redis corriendo).
    #
    # get_remote_address lee request.remote_addr directamente. Si despliegas
    # detrás de un reverse proxy (nginx, un load balancer, etc.), Flask verá
    # la IP del proxy para todos los clientes a menos que instales
    # werkzeug.middleware.proxy_fix.ProxyFix y confíes explícitamente en el
    # proxy. NO actives eso sin un proxy real de por medio: si el proceso
    # queda expuesto directo a internet y confías en X-Forwarded-For,
    # cualquiera puede falsificar esa cabecera y saltarse el límite por IP.
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri="memory://",
        default_limits=[],
    )

    initialize_database()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/chat")
    @limiter.limit("30 per minute; 8 per 10 seconds")
    def chat():
        data = request.get_json(silent=True) or {}
        message = data.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"error": "Escribe un mensaje para continuar."}), 400
        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({"error": "El mensaje es demasiado largo."}), 400

        # El session_id ya NO se acepta del cliente: antes cualquiera podía
        # enviar el session_id de otra persona y heredar su carrito o su
        # flujo de compra en curso. Ahora el servidor emite y firma el
        # identificador dentro de una cookie httponly; el cliente no puede
        # leerlo ni falsificarlo.
        session_id = session.get("sid")
        if not session_id:
            session_id = secrets.token_urlsafe(32)
            session["sid"] = session_id
            session.permanent = True

        return jsonify(process_message(message, session_id))

    @app.errorhandler(413)
    def payload_too_large(error):
        return jsonify({"error": "El mensaje enviado es demasiado grande."}), 413

    @app.errorhandler(429)
    def rate_limited(error):
        return jsonify({"error": "Demasiados mensajes seguidos. Espera un momento e intenta de nuevo."}), 429

    return app


app = create_app()


if __name__ == "__main__":
    # El modo debug de Flask/Werkzeug expone un depurador interactivo que
    # permite ejecutar código arbitrario en el servidor ante cualquier
    # excepción no controlada. NUNCA debe activarse en producción.
    # Se habilita solo si se define explícitamente FLASK_DEBUG=1, y aun así
    # solo escucha en localhost para no exponer el depurador en la red.
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug_mode, host="127.0.0.1", port=5000)