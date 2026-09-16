"""Búsqueda semántica local sobre el catálogo, sin ninguna API externa.

Por qué así y no con una API de IA en la nube:
- Corre 100% en el propio servidor: gratis para siempre, sin key, sin
  cuenta, sin facturación de nadie.
- Sin límite de peticiones ajeno: escala con tu propio hardware, no con la
  cuota gratuita de un tercero — con miles de clientes sigue funcionando
  igual de bien (o mal) que con diez.
- Ningún mensaje de un cliente sale nunca de tu servidor hacia un tercero.

Qué hace: precomputa un "embedding" (vector numérico que representa
significado) por cada producto activo del catálogo, y al llegar un mensaje
calcula su propio embedding y lo compara por similitud de coseno contra el
catálogo. Así "algo para hacer ejercicio y no perder llamadas" puede
encontrar audífonos deportivos aunque no comparta ninguna palabra literal
con el nombre del producto — a diferencia de las reglas por regex/palabras
exactas que ya tiene el bot.

Diseño defensivo: si la librería no está instalada, si no hubo internet en
el primer arranque para descargar el modelo, o si cualquier otra cosa falla,
este módulo se desactiva solo (`semantic_product_matches` devuelve `[]`) y
el chatbot sigue funcionando exactamente igual que antes con sus reglas
existentes. Esta capa nunca debe poder tumbar una conversación.
"""
import threading
from datetime import datetime, timedelta

from database import search_products

# Modelo multilingüe pequeño (~470 MB, se descarga una sola vez la primera
# vez que se usa y luego queda en caché local de HuggingFace). Soporta
# español, que es el idioma real de los mensajes de los clientes.
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

MIN_SIMILARITY = 0.35  # por debajo de esto, no se considera un match confiable
MAX_RESULTS = 5
CATALOG_EMBEDDINGS_TTL = timedelta(minutes=5)  # mismo criterio que catalog_terms() en chatbot.py

_model = None
_model_lock = threading.Lock()
_model_load_failed = False

_catalog_cache = {"products": None, "embeddings": None, "expires_at": None}


def _get_model():
    """Carga el modelo una sola vez (perezoso, thread-safe). Si falla, queda
    marcado como no disponible y no se vuelve a intentar en este proceso."""
    global _model, _model_load_failed
    if _model is not None or _model_load_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_load_failed:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
        except Exception:
            _model_load_failed = True
            _model = None
    return _model


def is_available():
    """True si el modelo ya está cargado y listo para usarse."""
    return _get_model() is not None


def _product_text(product):
    parts = [product["nombre"], product["categoria"], product["marca"] or "", product["descripcion"] or ""]
    return " . ".join(part for part in parts if part)


def _catalog_embeddings(model):
    now = datetime.now()
    if _catalog_cache["embeddings"] is not None and _catalog_cache["expires_at"] > now:
        return _catalog_cache["products"], _catalog_cache["embeddings"]
    products, _ = search_products("", available_only=True, page=1, page_size=1000)
    texts = [_product_text(product) for product in products]
    embeddings = model.encode(texts, normalize_embeddings=True) if texts else []
    _catalog_cache["products"] = products
    _catalog_cache["embeddings"] = embeddings
    _catalog_cache["expires_at"] = now + CATALOG_EMBEDDINGS_TTL
    return products, embeddings


def semantic_product_matches(query, min_similarity=MIN_SIMILARITY, max_results=MAX_RESULTS):
    """Devuelve productos activos y disponibles ordenados por similitud
    semántica con `query`, o [] si el modelo no está disponible, la consulta
    está vacía, o no hay ningún match confiable (todos por debajo del
    umbral). Nunca lanza una excepción hacia quien la llama.
    """
    model = _get_model()
    if model is None or not query or not query.strip():
        return []
    try:
        import numpy as np
        products, embeddings = _catalog_embeddings(model)
        if len(products) == 0:
            return []
        query_embedding = model.encode([query], normalize_embeddings=True)[0]
        scores = np.dot(embeddings, query_embedding)
        ranked = sorted(zip(products, scores), key=lambda pair: pair[1], reverse=True)
        return [product for product, score in ranked[:max_results] if score >= min_similarity]
    except Exception:
        return []

def warmup():
    """Pre-carga el modelo y el catálogo antes de encender el servidor web."""
    print("⏳ Iniciando precalentamiento del motor de IA (esto tomará unos segundos)...")
    model = _get_model()
    if model is not None:
        _catalog_embeddings(model)
    print("✅ Motor de IA cargado en RAM. ¡El chat responderá al instante!")