from flask import Flask, jsonify, render_template, request

from chatbot import process_message
from database import initialize_database


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    initialize_database()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/chat")
    def chat():
        data = request.get_json(silent=True) or {}
        message = data.get("message", "")
        session_id = data.get("session_id", "default")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"error": "Escribe un mensaje para continuar."}), 400
        return jsonify(process_message(message, session_id))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
