import math
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

from database import (
    create_order, create_repair, get_product, get_repair_by_code_and_phone,
    list_categories, search_products,
)
from semantic_search import semantic_product_matches

SESSIONS = {}
PAGE_SIZE = 5

# El código de reparación es secuencial y predecible (ZT-R-YYYYMMDD-0001), y
# el teléfono no es un secreto fuerte, así que "código + teléfono" por sí solo
# es una credencial débil. Sin límite de intentos, alguien con el teléfono de
# otra persona podría probar pocos códigos por fecha hasta acertar. Este
# límite hace que ese ataque sea impráctico sin necesidad de rediseñar el
# formato del código, que es visible y usado por el negocio.
REPAIR_LOOKUP_MAX_ATTEMPTS = 5
REPAIR_LOOKUP_LOCKOUT = timedelta(minutes=15)

# Ver docstring de catalog_terms() para el porqué de este caché.
_CATALOG_TERMS_CACHE = {"terms": None, "expires_at": None}
CATALOG_TERMS_TTL = timedelta(minutes=5)
BUSINESS_INFO = {"phone": "313 820 4477", "address": "Carrera 96B #19-19, Bogotá, Colombia", "hours": "9:30 a. m. a 8:00 p. m."}

MAIN_MENU_OPTIONS = [
    "🛍️ Comprar productos",
    "🔧 Reparar mi equipo",
    "📋 Consultar reparación",
    "🛡️ Garantía",
    "💳 Pagos y envíos",
    "📍 Ubicación y horarios",
    "👤 Hablar con un asesor",
]

RETURN_OPTIONS = [
    "🛍️ Comprar productos",
    "🔧 Reparar mi equipo",
    "📋 Consultar reparación",
    "👤 Hablar con un asesor",
]


def normalize(text):
    value = unicodedata.normalize("NFD", text.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return " ".join(value.split())


CONVERSATION_TERMS = {
    "adaptador", "adaptadores", "audifono", "audifonos", "barato", "bateria",
    "camara", "cargador", "celular", "celulares", "disponible", "gaming",
    "llamadas", "mouse", "musica", "rendimiento", "teclado", "telefono",
    "telefonos", "uso", "xiaomi", "cual", "mejor", "otro", "opciones",
    "quiero", "almacenamiento", "tecno",
}

CONTROL_WORDS = {
    "tienes", "otros", "otro", "otras", "otra", "mas", "no", "me", "convencen",
    "recomiendas", "muestrame", "dame", "siguiente", "pagina", "cambiar", "presupuesto",
}

SAFE_TYPO_CORRECTIONS = {
    "kiero": "quiero",
    "alamecenamiento": "almacenamiento",
    "tecnho": "tecno",
}

def edit_distance(left, right):
    """Small deterministic Levenshtein implementation for short input tokens."""
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        current = [index]
        for other_index, right_char in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[other_index] + 1, previous[other_index - 1] + (left_char != right_char)))
        previous = current
    return previous[-1]


def catalog_terms():
    """Returns normalized real catalog words plus a compact conversational vocabulary.

    Se cachea con un TTL corto: antes esto reconstruía el vocabulario completo
    (hasta 1000 productos) desde SQLite en cada mensaje que pasara por
    corrección ortográfica difusa, lo cual con el catálogo real (488
    productos) hacía de cada mensaje "raro" una operación cara y fácil de
    saturar en ráfaga. El costo es que, tras un `import_catalog_csv.py`
    corrido mientras la app está viva, el vocabulario puede tardar hasta
    CATALOG_TERMS_TTL en reflejar productos nuevos — aceptable para
    autocompletado/corrección, no para datos que deban ser exactos al
    instante.
    """
    now = datetime.now()
    if _CATALOG_TERMS_CACHE["terms"] is not None and _CATALOG_TERMS_CACHE["expires_at"] > now:
        return _CATALOG_TERMS_CACHE["terms"]
    terms = set(CONVERSATION_TERMS)
    for row in list_categories():
        terms.update(normalize(row["categoria"]).split())
    products, _ = search_products("", page=1, page_size=1000)
    for product in products:
        for field in ("nombre", "marca", "modelo"):
            terms.update(word for word in normalize(product[field] or "").split() if len(word) >= 4)
    _CATALOG_TERMS_CACHE["terms"] = terms
    _CATALOG_TERMS_CACHE["expires_at"] = now + CATALOG_TERMS_TTL
    return terms


def fuzzy_normalize(text):
    """Corrects only a unique, close known term; ambiguous input remains untouched."""
    terms = catalog_terms()
    corrected, uncertain = [], False
    for word in text.split():
        if word in SAFE_TYPO_CORRECTIONS:
            corrected.append(SAFE_TYPO_CORRECTIONS[word])
            continue
        if len(word) < 4 or word in CONTROL_WORDS or word in terms or word.isdigit():
            corrected.append(word)
            continue
        limit = 1 if len(word) <= 5 else 2
        matches = [(term, edit_distance(word, term)) for term in terms if abs(len(term) - len(word)) <= limit and edit_distance(word, term) <= limit]
        closest_distance = min((distance for _, distance in matches), default=None)
        closest = [term for term, distance in matches if distance == closest_distance]
        if len(closest) == 1:
            corrected.append(closest[0])
        else:
            corrected.append(word)
            uncertain = uncertain or bool(matches)
    return " ".join(corrected), uncertain


def money(value):
    return "Sin precio registrado" if value is None else "$" + f"{value:,.0f}".replace(",", ".")


def response(text, quick_replies=None):
    return {"reply": text, "quick_replies": quick_replies or []}


def session_for(session_id):
    return SESSIONS.setdefault(session_id, {
        "cart": [], "flow": None, "data": {}, "last_search": {}, "last_results": [],
        "last_selected_id": None, "conversation": {},
        "repair_lookup": {"failed_attempts": 0, "locked_until": None},
    })


def repair_lookup_locked(session):
    """Returns the remaining lockout as a timedelta, or None if not locked."""
    locked_until = session.setdefault("repair_lookup", {"failed_attempts": 0, "locked_until": None}).get("locked_until")
    if locked_until and datetime.now() < locked_until:
        return locked_until - datetime.now()
    return None


def register_repair_lookup_result(session, found):
    state = session.setdefault("repair_lookup", {"failed_attempts": 0, "locked_until": None})
    if found:
        state["failed_attempts"] = 0
        state["locked_until"] = None
        return
    state["failed_attempts"] += 1
    if state["failed_attempts"] >= REPAIR_LOOKUP_MAX_ATTEMPTS:
        state["locked_until"] = datetime.now() + REPAIR_LOOKUP_LOCKOUT
        state["failed_attempts"] = 0


def conversation_for(session):
    return session.setdefault("conversation", {})


def clear_conversation(session):
    session["conversation"] = {}


def category_for_text(text):
    categories = {normalize(row["categoria"]): row["categoria"] for row in list_categories()}
    for key, actual in sorted(categories.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            return actual
    for key, actual in categories.items():
        if key.endswith("s") and re.search(rf"(?<!\w){re.escape(key[:-1])}(?!\w)", text):
            return actual
    if re.search(r"(?<!\w)(?:celular|telefono|telefonos)(?!\w)", text):
        return categories.get("celulares")
    if re.search(r"(?<!\w)buds\d*(?!\w)", text):
        return categories.get("audifonos")
    if re.search(r"(?<!\w)smart\s*watch(?!\w)", text):
        return categories.get("smartwatch")
    return None


def conversational_budget(text):
    budget_text = re.sub(r"\bun millon(?:es)?\b", "1 millon", text)
    explicit = re.search(
        r"(?:menos de|hasta|por debajo de|maximo|no mas de|alrededor de|tengo|presupuesto|mas de)\s*\$?\s*(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(millon(?:es)?|mil|k))?",
        budget_text,
    )
    standalone = re.fullmatch(r"\s*\$?\s*(\d+(?:[.,]\d+)?)\s*(millon(?:es)?|mil|k)\s*", budget_text)
    if standalone is None:
        standalone = re.fullmatch(r"\s*\$?\s*(\d{1,3}(?:[.,]\d{3})+)\s*", budget_text)
    amount = explicit or standalone
    if not amount:
        return None
    raw = amount.group(1)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        value = float(re.sub(r"[.,]", "", raw))
    else:
        value = float(raw.replace(",", "."))
    unit = amount.group(2) if amount.lastindex and amount.lastindex >= 2 else ""
    multiplier = 1_000_000 if unit.startswith("millon") else (1000 if unit else 1)
    return value * multiplier


def budget_direction(text):
    return "minimum" if re.search(r"\bmas de\b", text) else "maximum"


def last_search_total(session):
    options = session.get("last_search", {})
    if not options:
        return 0
    _, total = search_products(
        options.get("query", ""), options.get("category"), options.get("max_price"),
        options.get("available_only", False), 1, PAGE_SIZE, options.get("brand"),
    )
    return total


def last_result_products(session):
    return [product for product_id in session.get("last_results", []) if (product := get_product(product_id))]


def ordinal_result_product(products, text):
    match = re.search(
        r"\b(?:el |la )?(primero|primera|segundo|segunda|tercero|tercera|cuarto|cuarta|ultimo|ultima|final)\b",
        text,
    )
    positions = {
        "primero": 0, "primera": 0, "segundo": 1, "segunda": 1,
        "tercero": 2, "tercera": 2, "cuarto": 3, "cuarta": 3,
        "ultimo": -1, "ultima": -1, "final": -1,
    }
    if match:
        index = positions[match.group(1)]
    else:
        numeric = re.search(r"\b(?:numero|no)\s*(\d+)\b", text)
        if not numeric:
            return None, False
        index = int(numeric.group(1)) - 1
        if index < 0:
            return None, True
    if not products or (index >= len(products) and index != -1):
        return None, True
    return products[index], True


def products_list_response(products, heading):
    if not products:
        return response("No hay productos disponibles dentro de los últimos resultados mostrados.", ["Ver catálogo"])
    lines = [heading]
    for index, product in enumerate(products, 1):
        availability = "Disponible" if product["stock"] > 0 else "Agotado"
        lines.append(f"{index}. {product['nombre']} — {money(product['precio'])} · {availability}")
    return response("\n".join(lines), [f"Ver producto #{product['id']}" for product in products] + ["Ver catálogo"])


def last_results_follow_up(session, text):
    """Answers price and availability questions from the products already shown."""
    products = last_result_products(session)
    if not products:
        return None
    ordinal_product, ordinal_requested = ordinal_result_product(products, text)
    if ordinal_requested and not ordinal_product:
        return response("No quiero confundirme 😊 ¿Me dices el nombre del producto que te interesa?")
    if ordinal_product and re.search(r"\b(?:cuanto|precio|vale)\b", text):
        return response(
            f"{ordinal_product['nombre']} cuesta {money(ordinal_product['precio'])}.",
            [f"Ver producto #{ordinal_product['id']}", "Ver catálogo"],
        )
    if ordinal_product and re.search(r"\b(?:cual|cuál)\b", text):
        session["last_selected_id"] = ordinal_product["id"]
        return product_card(ordinal_product)
    if re.search(r"\b(?:cuanto cuestan|cuanto vale|que precio tienen|precios?)\b", text):
        return products_list_response(products, "Estos son los precios de los productos mostrados:")
    if re.search(r"\b(?:cual es el mas barato|cual cuesta menos|el mas economico)\b", text):
        priced = [product for product in products if product["precio"] is not None]
        if not priced:
            return response("Los productos mostrados no tienen precio registrado.", ["Hablar con un asesor"])
        product = min(priced, key=lambda item: item["precio"])
        session["last_selected_id"] = product["id"]
        return product_card(product)
    if re.search(r"\b(?:cual esta disponible|cuales estan disponibles|muestrame (?:los )?disponibles|ver disponibles)\b", text):
        return products_list_response(
            [product for product in products if product["stock"] > 0],
            "Estos son los productos disponibles de los últimos resultados:",
        )
    return None


def specific_product_tokens(text):
    stop_words = {
        "tienes", "tiene", "quiero", "me", "interesa", "el", "la", "los", "las", "un", "una",
        "ese", "esa", "esto", "producto", "por", "favor", "busco", "necesito", "ver", "comprar",
    }
    return [word for word in re.findall(r"[a-z0-9]+", text) if word not in stop_words and len(word) >= 2]


def product_matches_tokens(product, tokens):
    haystack = normalize(" ".join(str(product[field] or "") for field in ("nombre", "marca", "modelo")))
    return all(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", haystack) for token in tokens)


def specific_product_response(session, text):
    """Resolves a clearly named product before falling back to word-by-word catalog search."""
    tokens = specific_product_tokens(text)
    demonstrative = bool(re.search(r"\b(?:ese|esa)\b", text))
    category_words = {
        word
        for row in list_categories()
        for word in normalize(row["categoria"]).split()
    }
    brand_words = set(active_brands())
    distinctive = [word for word in tokens if word not in category_words and word not in brand_words]
    specific = demonstrative or any(re.search(r"\d", token) for token in tokens) or len(distinctive) >= 2
    if not tokens or not specific:
        return None

    selected_id = session.get("last_selected_id")
    if demonstrative and selected_id:
        selected = get_product(selected_id)
        if selected and product_matches_tokens(selected, tokens):
            return product_card(selected)

    previous = [get_product(product_id) for product_id in session.get("last_results", [])]
    matches = [product for product in previous if product and product_matches_tokens(product, tokens)]
    if len(matches) == 1:
        session["last_selected_id"] = matches[0]["id"]
        return product_card(matches[0])

    products, _ = search_products("", page=1, page_size=1000)
    matches = [product for product in products if product_matches_tokens(product, tokens)]
    if len(matches) == 1:
        session["last_selected_id"] = matches[0]["id"]
        return product_card(matches[0])
    return None


def active_brands():
    """Returns only brands that exist on active catalog products."""
    products, _ = search_products("", page=1, page_size=1000)
    return {
        normalize(product["marca"]): product["marca"]
        for product in products
        if product["marca"] and normalize(product["marca"])
    }


def brand_for_text(text):
    for key, label in sorted(active_brands().items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            return key, label
    return None, None


def brand_is_active(brand, category="CELULARES"):
    _, total = search_products("", category, None, False, 1, 1, brand)
    return total > 0


def redmi_buds_search(session, text):
    """Treats Redmi Buds as a brand/model phrase, never as a SQLite category."""
    if not re.search(r"\bredmi\s+buds?\b", text):
        return None
    categories = {normalize(row["categoria"]): row["categoria"] for row in list_categories()}
    category = categories.get("audifonos")
    brand = active_brands().get("redmi")
    if not category or not brand:
        return None
    conversation_for(session).update({"interest": category, "category": category, "brand": brand, "query": "buds", "stage": "brand"})
    return show_products(session, query="buds", category=category, brand=brand)


def repair_intent(text):
    return bool(re.search(
        r"\b(?:reparar|arreglar|dana(?:do|da|ron)?|dano|no funciona|llevar .* a reparar|(?:quiero|necesito|hacer|registrar).*reparacion)\b",
        text,
    ))


def gift_intent(text):
    return bool(re.search(r"\b(?:para regalar|un regalo|es para regalar|puedo regalar)\b", text))


def recommend_from_context(session):
    """Recommends only from actual results or shows the saved real filters."""
    options = session.get("last_search", {})
    if options:
        products, total = search_products(
            options.get("query", ""), options.get("category"), options.get("max_price"),
            options.get("available_only", False), 1, PAGE_SIZE, options.get("brand"),
        )
        if total and products:
            available = [product for product in products if product["stock"] > 0 and product["precio"] is not None]
            product = min(available or products, key=lambda item: item["precio"] if item["precio"] is not None else float("inf"))
            session["last_selected_id"] = product["id"]
            return response(
                f"Con los filtros que elegiste, puedes considerar {product['nombre']} por {money(product['precio'])}. "
                "Es una opción real del catálogo que coincide con tu búsqueda; para comparar características técnicas específicas, un asesor puede orientarte.",
                [f"Ver producto #{product['id']}", "Ver más opciones", "Hablar con un asesor"],
            )
    conversation = conversation_for(session)
    if conversation.get("category") or conversation.get("query"):
        return show_products(
            session, conversation.get("query", ""), conversation.get("category"),
            conversation.get("budget"), True, brand=conversation.get("brand"),
        )
    return response("Para recomendarte una opción real, dime qué producto buscas o un presupuesto aproximado.", ["Ver catálogo", "Hablar con un asesor"])


def result_conversation(session, text):
    """Handles follow-ups about the last real result set without treating them as queries."""
    options = session.get("last_search", {})
    if not options:
        return None
    total = last_search_total(session)
    page = options.get("page", 1)
    pages = math.ceil(total / PAGE_SIZE) if total else 0
    alternatives = bool(re.search(r"\b(?:no me convencen|no me gusta ninguno|ninguno me convence|ninguna me convence|no me gustan|quiero otra cosa|quiero algo diferente|tienes algo mejor)\b", text))
    relative_price = re.search(r"\b(?:unos?|algo|tienes) mas (barato|economico|caro)\b", text)
    asking_more = bool(re.search(r"\b(?:mas|siguiente|siguiente pagina|ver mas|dame mas|muestrame mas|tienes otros?|hay otros?|quiero otros?|muestrame otros?|dame otros?|solo tienes esos?|solo hay esos?|solo esos|no hay mas|hay mas|que mas|tienes otros|muestrame mas|dame mas opciones|dame opciones|muestrame opciones|quiero opciones|otra opcion|hay otro|cuales mas|otras opciones)\b", text))
    if alternatives:
        conversation_for(session)["pending_action"] = "change_filters"
        return response(
            "Entiendo 😊 Mantengo tu búsqueda actual. ¿Qué prefieres cambiar para ver alternativas?",
            ["Ver más opciones", "Cambiar presupuesto", "Cambiar marca", "Ver catálogo"],
        )
    if text == "cambiar presupuesto":
        conversation_for(session)["stage"] = "budget"
        return response("Claro 😊 ¿Cuál es tu presupuesto máximo? Mantendré los demás filtros de tu búsqueda.")
    if text == "cambiar marca":
        conversation_for(session)["stage"] = "brand"
        return response("Claro 😊 ¿Qué marca prefieres? Mantendré la categoría y el presupuesto actuales.")
    if relative_price:
        items, _ = search_products(
            options.get("query", ""), options.get("category"), options.get("max_price"),
            options.get("available_only", False), 1, 1000, options.get("brand"),
        )
        shown_products = [get_product(product_id) for product_id in session.get("last_results", [])]
        shown_prices = [product["precio"] for product in shown_products if product and product["precio"] is not None]
        reference = min(shown_prices) if shown_prices else None
        candidates = [product for product in items if product["precio"] is not None and (reference is None or (product["precio"] < reference if relative_price.group(1) in {"barato", "economico"} else product["precio"] > reference))]
        if candidates:
            product = min(candidates, key=lambda item: item["precio"]) if relative_price.group(1) in {"barato", "economico"} else max(candidates, key=lambda item: item["precio"])
            session["last_selected_id"] = product["id"]
            return product_card(product)
        return response("No encontré una opción con ese precio dentro de los filtros actuales.", ["Cambiar presupuesto", "Ver catálogo"])
    if asking_more:
        if total <= 1:
            return response("Sí 😊 Con los filtros actuales solo encontré 1 opción. Si quieres, podemos ampliar la búsqueda cambiando la marca o el presupuesto.")
        if page < pages:
            options["page"] = page + 1
            return show_products(session, **options)
        return response(
            f"Ya te mostré las {total} opciones que coinciden con los filtros actuales. "
            "Si quieres, podemos ajustar la búsqueda para ver alternativas.",
            ["Cambiar presupuesto", "Cambiar marca", "Ver catálogo"],
        )
    if text in {"si", "sí"} and conversation_for(session).get("pending_action") == "more_results":
        conversation_for(session).pop("pending_action", None)
        if page < pages:
            options["page"] = page + 1
            return show_products(session, **options)
        return response("Ya estás viendo la última página de esos resultados.", ["Ver catálogo"])
    if text in {"otro", "otra", "otro mas", "uno mas", "algo parecido", "algo similar", "uno diferente"}:
        if page < pages:
            options["page"] = page + 1
            return show_products(session, **options)
        return response("No encontré otra opción con los filtros actuales.", ["Ver catálogo", "Hablar con un asesor"])
    if re.search(r"\b(?:otro|uno) mas barato\b", text) or re.search(r"\b(?:otro|uno) mas caro\b", text):
        items, _ = search_products(
            options.get("query", ""), options.get("category"), options.get("max_price"),
            options.get("available_only", False), 1, 1000, options.get("brand"),
        )
        shown = set(session.get("last_results", []))
        candidates = [product for product in items if product["id"] not in shown and product["precio"] is not None]
        if not candidates:
            return response("No encontré otra opción disponible con ese criterio.", ["Ver catálogo"])
        product = min(candidates, key=lambda item: item["precio"]) if "barato" in text else max(candidates, key=lambda item: item["precio"])
        session["last_selected_id"] = product["id"]
        return product_card(product)
    if re.search(r"\b(?:cual me recomiendas|cual es mejor|que me recomiendas|que recomiendas|cual recomiendas|cual elegirias|cual deberia comprar|cual me conviene|que comprarias tu)\b", text):
        return recommend_from_context(session)
    return None


def product_interest(text):
    return category_for_text(text)


def start_discovery(session, text):
    interest = product_interest(text)
    conversation = conversation_for(session)
    budget = conversational_budget(text)
    if budget is None:
        budget = conversation.get("budget")
    category = interest if interest in {row["categoria"] for row in list_categories()} else None
    brand, brand_label = brand_for_text(text)
    query = "buds" if re.search(r"\bbuds?\d*\b", text) else ""
    conversation.update({"interest": interest, "category": category, "query": query, "budget": budget, "stage": "budget"})
    if brand:
        conversation["brand"] = brand_label
        if category and not brand_is_active(brand, category):
            return response(f"No encontré productos {brand_label} activos en el catálogo actual.", ["Ver CELULARES", "Ver catálogo", "Hablar con un asesor"])
        if category:
            return show_products(session, query=query, category=category, max_price=budget, available_only=False, brand=brand_label)
    if category == "CELULARES":
        if budget is None:
            return response("¡Claro! 📱 Te ayudo a encontrar uno. ¿Tienes un presupuesto aproximado o buscas algo en especial?", ["Ver CELULARES", "Hablar con un asesor"])
        conversation["stage"] = "preference"
        return response("Perfecto 👌 ¿Qué te importa más: cámara, batería, rendimiento o almacenamiento?")
    if category == "Audífonos":
        if query:
            return show_products(session, query=query, category=category, max_price=budget, available_only=False, brand=brand_label)
        if budget is None:
            return response("¡Claro! 🎧 ¿Los buscas para música, llamadas, gaming o uso diario? Si tienes un presupuesto, también me sirve.")
        return show_products(session, query=query, category=category, max_price=budget, available_only=True)
    return None


def conversational_search(session, message, text):
    conversation = conversation_for(session)
    if not conversation:
        return None
    if text in {"no se", "nose"}:
        if conversation.get("budget") is not None:
            conversation["pending_action"] = "show_options"
            return response(
                f"No pasa nada 😊 Puedo mostrarte opciones reales dentro de {money(conversation['budget'])} para que compares.",
                ["Ver opciones", "Cámara", "Batería", "Rendimiento", "Almacenamiento"],
            )
        if conversation.get("interest"):
            return response("No pasa nada 😊 ¿Tienes un presupuesto aproximado? Con eso te muestro opciones reales del catálogo.")
        return main_menu()
    if text == "no" and conversation.get("stage") == "budget":
        conversation["budget"] = None
        conversation["pending_action"] = "show_options"
        return response("No hay problema 😊 Puedo mostrarte las opciones disponibles sin aplicar un presupuesto.", ["Sí", "Ver catálogo"])
    if text in {"si", "sí", "mostrar opciones", "ver opciones"} and conversation.get("pending_action") == "show_options":
        conversation.pop("pending_action", None)
        return show_products(
            session, conversation.get("query", ""), conversation.get("category"),
            None, True,
        )
    if conversation.get("budget_direction") == "minimum" and re.search(r"\b(?:opciones|mostrar|muestra|ver|dame)\b", text):
        return show_products(
            session, conversation.get("query", ""), conversation.get("category"),
            None, True,
        )
    budget = conversational_budget(text)
    if budget is not None:
        conversation["budget"] = budget
        conversation["budget_direction"] = budget_direction(text)
        if conversation["budget_direction"] == "minimum":
            return response("Entiendo: buscas opciones desde ese presupuesto. El catálogo actual permite filtrar por precio máximo; si me dices un tope, te muestro opciones reales.")
        if conversation.get("category") == "CELULARES" and conversation.get("stage") == "budget":
            conversation["stage"] = "preference"
            return response("Perfecto 👌 ¿Qué te importa más: cámara, batería, rendimiento o almacenamiento?")
        return show_products(session, conversation.get("query", ""), conversation.get("category"), budget, True)
    preference = re.search(r"\b(?:camara|bateria|rendimiento|almacenamiento)\b", text)
    if preference and conversation.get("category") == "CELULARES":
        conversation["preference"] = preference.group(0)
        conversation["stage"] = "brand"
        result = show_products(
            session, conversation.get("query", ""), conversation.get("category"),
            conversation.get("budget"), True,
        )
        result["reply"] = (
            f"No tengo especificaciones técnicas estructuradas para filtrar por {preference.group(0)}, "
            "así que te muestro las opciones disponibles dentro de tu presupuesto para que compares.\n\n"
            + result["reply"]
        )
        return result
    audio_use = re.search(r"\b(?:musica|llamadas|gaming|uso diario)\b", text)
    if audio_use and normalize(conversation.get("interest", "")) == "audifonos":
        conversation["use"] = audio_use.group(0)
        conversation["stage"] = "budget"
        return response("Perfecto 🎧 ¿Tienes un presupuesto máximo?")
    brand, brand_label = brand_for_text(text)
    if brand and conversation.get("category"):
        conversation["brand"] = brand_label
        conversation["query"] = ""
        conversation["stage"] = "brand"
        category = conversation["category"]
        if not brand_is_active(brand, category):
            return response(f"No encontré productos {brand_label} activos en el catálogo actual.", ["Ver CELULARES", "Ver catálogo", "Hablar con un asesor"])
        return show_products(
            session, "", category, conversation.get("budget"), True, brand=brand_label,
        )
    if conversation.get("category") and re.search(r"\b(?:unos?|algo|tienes) mas (?:barato|economico)\b", text):
        conversation["price_preference"] = "economico"
        return show_products(
            session, conversation.get("query", ""), conversation.get("category"),
            conversation.get("budget"), True,
        )
    if re.search(r"\b(?:barato|economico)\b", text):
        conversation["price_preference"] = "economico"
        conversation["stage"] = "brand"
        return response("Claro 👍 Busquemos algo económico. ¿Te interesa alguna marca o prefieres ver las opciones disponibles?")
    if conversation.get("stage") == "preference":
        query, _, _ = parse_search(text)
        return show_products(session, query, conversation.get("category"), conversation.get("budget"), True)
    if conversation.get("stage") == "brand":
        if re.search(r"\b(?:opciones|mostrar|muestra|ver)\b", text):
            return show_products(session, conversation.get("query", ""), conversation.get("category"), conversation.get("budget"), True)
        query, category, _ = parse_search(text)
        return show_products(session, query, category or conversation.get("category"), conversation.get("budget"), True)
    return None


def main_menu():
    return response(
        "¡Hola! Soy el asistente virtual de Zonatech. ¿En qué puedo ayudarte hoy?",
        MAIN_MENU_OPTIONS,
    )


def warranty_info():
    return response(
        "🛡️ Garantía\n\nLa garantía depende del tipo de reparación o producto. "
        "Un asesor de Zonatech puede revisar tu caso y confirmar las condiciones aplicables.",
        RETURN_OPTIONS,
    )


def payments_and_shipping_info():
    return response(
        "💳 Pagos y envíos\n\n"
        "Métodos de pago: transferencia bancaria, efectivo, tarjetas, Addi y Sistecrédito.\n"
        "Según la configuración actual, Addi y Sistecrédito aplican únicamente para Audífonos y Adaptadores.\n\n"
        "Hacemos domicilios y envíos nacionales. El costo depende de la transportadora. "
        "Para envíos nacionales, el tiempo estimado es de 2 a 3 días.",
        RETURN_OPTIONS,
    )


def payment_info_intent(text):
    """Recognize natural questions about payment methods without matching product searches."""
    normalized = normalize(text)
    if re.search(r"\b(?:como|con\s+que)\b.*\bpagar\b", normalized):
        return True
    return bool(re.search(
        r"\b(?:que|cuales)\b.*\b(?:medios?|formas?|metodos?|opciones?)\b.*\bpago\b",
        normalized,
    ))


def location_and_hours_info():
    return response(
        f"📍 Ubicación y horarios\n\nDirección: {BUSINESS_INFO['address']}\n"
        f"Horario: {BUSINESS_INFO['hours']}",
        RETURN_OPTIONS,
    )


def advisor_info():
    return response(
        f"👤 Atención con asesor\n\nWhatsApp: {BUSINESS_INFO['phone']}\n"
        f"Horario: {BUSINESS_INFO['hours']}",
        ["🛍️ Comprar productos", "🔧 Reparar mi equipo", "📋 Consultar reparación", "Menú principal"],
    )


def catalog_menu():
    categories = list_categories()
    if not categories:
        return response("El catálogo local aún no tiene productos cargados.", ["👤 Hablar con un asesor", "Menú principal"])
    labels = [f"Ver {row['categoria']}" for row in categories]
    return response("📋 Catálogo de Zonatech\nElige una categoría o escribe lo que buscas.", labels + ["Ver todo", "👤 Hablar con un asesor", "Menú principal"])


def product_card(product):
    stock = "Disponible" if product["stock"] > 0 else "Agotado"
    return response(
        f"{product['nombre']}\n\n💰 Precio: {money(product['precio'])}\n📦 Stock: {stock} ({product['stock']} unidad(es))\n\n{product['descripcion'] or 'Sin descripción registrada.'}",
        ([f"Comprar #{product['id']}"] if product["stock"] > 0 and product["precio"] is not None else []) + ["Ver catálogo", "Hablar con un asesor"],
    )


def show_products(session, query="", category=None, max_price=None, available_only=False, page=1, brand=None, raw_message=None):
    filters = {
        "query": query,
        "category": category,
        "max_price": max_price,
        "available_only": available_only,
        "brand": brand,
    }
    previous = session.get("last_search", {})
    if previous and any(previous.get(key) != value for key, value in filters.items()):
        session.pop("pending_result_purchase", None)
    products, total = search_products(query, category, max_price, available_only, page, PAGE_SIZE, brand)
    session["last_search"] = {**filters, "page": page}
    session["last_results"] = [product["id"] for product in products]
    if not total:
        if max_price is not None:
            unfiltered, unfiltered_total = search_products(query, category, None, available_only, 1, 1000, brand)
            over_budget = [product for product in unfiltered if product["precio"] is not None and product["precio"] > max_price]
            if unfiltered_total and over_budget:
                _, brand_label = brand_for_text(query)
                label = brand_label or (category.title() if category else "estos productos")
                cheapest = min(over_budget, key=lambda product: product["precio"])
                return response(
                    f"No encontré {label} dentro de tu presupuesto de {money(max_price)}. "
                    f"En el catálogo hay opciones que superan ese presupuesto; la más económica disponible está en {money(cheapest['precio'])}. "
                    "¿Quieres aumentar el presupuesto o ver otras marcas?",
                    ["Cambiar presupuesto", "Cambiar marca", "Ver catálogo"],
                )
        # La búsqueda exacta por palabras no encontró nada. Antes de rendirse,
        # probamos si el mensaje original se parece semánticamente a algo del
        # catálogo (ej. "algo para hacer ejercicio y no perder llamadas" ->
        # audífonos deportivos), aunque no comparta ninguna palabra literal.
        # Solo puede devolver productos que ya existen en el catálogo real —
        # nunca inventa nada ni genera texto libre — así que no es una
        # superficie de prompt injection.
        semantic_matches = semantic_product_matches(raw_message or query)
        if semantic_matches:
            return show_semantic_matches(session, semantic_matches)
        return response("No encontré productos activos que coincidan con esa búsqueda. No inventaré alternativas; puedes intentar otra búsqueda o hablar con un asesor.", ["Ver catálogo", "Hablar con un asesor"])
    pages = math.ceil(total / PAGE_SIZE)
    title = category or (query.title() if query else "Productos")
    lines = [f"📋 {title}", f"Página {page}/{pages}"]
    for index, product in enumerate(products, 1):
        availability = "Disponible" if product["stock"] > 0 else "Agotado"
        lines.append(f"{index}. {product['nombre']} — {money(product['precio'])} · {availability}")
    buttons = [f"Ver producto #{product['id']}" for product in products]
    if page < pages:
        buttons.append("Siguiente página")
    if page > 1:
        buttons.append("Página anterior")
    buttons += ["Buscar otro producto", "Ver catálogo"]
    return response("\n".join(lines), buttons)


def show_semantic_matches(session, products):
    """Renderiza resultados encontrados por similitud semántica (no por
    coincidencia exacta de palabras). Reusa el mismo mecanismo de
    'last_results' que show_products, así que 'ver producto #2' o
    'comprar #2' funcionan igual sobre estos resultados."""
    session["last_search"] = {}
    session["last_results"] = [product["id"] for product in products]
    lines = ["🔎 Esto encontré relacionado con lo que buscas:"]
    for index, product in enumerate(products, 1):
        availability = "Disponible" if product["stock"] > 0 else "Agotado"
        lines.append(f"{index}. {product['nombre']} — {money(product['precio'])} · {availability}")
    lines.append("\nSi ninguno es lo que buscabas, dime con otras palabras o habla con un asesor.")
    buttons = [f"Ver producto #{product['id']}" for product in products] + ["Ver catálogo", "Hablar con un asesor"]
    return response("\n".join(lines), buttons)


def parse_search(text):
    categories = {normalize(row["categoria"]): row["categoria"] for row in list_categories()}
    category = None
    query_text = text
    for key, actual in sorted(categories.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = rf"(?<!\w){re.escape(key)}(?!\w)"
        if re.search(pattern, query_text):
            category = actual
            query_text = re.sub(pattern, " ", query_text)
            break
    if category is None:
        for alias in ("celular", "telefono", "telefonos"):
            pattern = rf"(?<!\w){alias}(?!\w)"
            if re.search(pattern, query_text) and "celulares" in categories:
                category = categories["celulares"]
                query_text = re.sub(pattern, " ", query_text)
                break
    amount = re.search(r"(?:menos de|hasta|por debajo de|maximo|no mas de|alrededor de)\s*\$?\s*(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(millon(?:es)?|mil|k)?", text)
    max_price = None
    if amount:
        raw_amount = amount.group(1)
        value = float(re.sub(r"[.,]", "", raw_amount)) if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw_amount) else float(raw_amount.replace(",", "."))
        unit = amount.group(2) or ""
        max_price = value * (1_000_000 if unit.startswith("millon") else (1000 if unit else 1))
        query_text = query_text[:amount.start()] + " " + query_text[amount.end():]
    query = re.sub(
        r"\b(?:quiero|quieres|ver|mostrar|muestra|muestrame|que|tienes|tienen|tiene|hay|habra|busco|buscar|necesito|dame|cuanto|cuales|disponibles|disponible|todos|las|los|el|la|de|por|para|cosas|menos|hasta|maximo|mil|catalogo|producto|productos|comprar|un|una|stock|algo|alguna|alguno|cualquier|cualquiera)\b",
        " ",
        query_text,
    )
    query = " ".join(word for word in re.findall(r"[a-z0-9]+", query) if not word.isdigit())
    query = re.sub(r"\badaptadores\b", "adaptador", query)
    return query, category, max_price


def parse_catalog_search(text):
    """Separates an active catalog brand from the free-text product query."""
    query, category, max_price = parse_search(text)
    category = category or category_for_text(text)
    brand_key, brand = brand_for_text(text)
    if brand_key:
        query = re.sub(rf"(?<!\w){re.escape(brand_key)}(?!\w)", " ", query)
        query = " ".join(query.split())
    return query, category, max_price, brand


def explicit_catalog_request(text):
    """True when a message explicitly starts a new catalog search."""
    _, category, _, brand = parse_catalog_search(text)
    category = category or category_for_text(text)
    request = bool(re.search(r"\b(?:muestrame|muestra|ver|quiero|busco|necesito|tienes|tienen|tiene|hay|venden|vende)\b", text))
    bare_structured_filters = bool(category or brand) and not re.search(
        r"\b(?:un|una|unos|unas|por|para|con|de|favor)\b", text,
    )
    return (request or bare_structured_filters) and bool(category or brand)


def clear_purchase_state(session):
    session["cart"], session["flow"], session["data"] = [], None, {}
    session.pop("pending_result_purchase", None)


def purchase_cancel_intent(text):
    return bool(re.fullmatch(
        r"(?:por favor )?(?:cancelar(?: compra)?|cancela(?: compra)?|"
        r"no quiero(?: comprar)?|volver|atras|salir(?: de la compra)?)",
        text,
    ))


def start_purchase(session, product_id):
    product = get_product(product_id)
    if not product:
        return response("Ese producto ya no está activo en el catálogo.", ["Ver catálogo"])
    if product["stock"] <= 0:
        return response(f"{product['nombre']} está agotado en este momento. No puedo agregarlo al carrito.", ["Ver catálogo", "Hablar con un asesor"])
    if product["precio"] is None:
        return response("Ese producto no tiene un precio registrado y requiere cotización.", ["Hablar con un asesor"])
    session["flow"], session["data"] = "quantity", {"product_id": product["id"]}
    return response(f"{product['nombre']} cuesta {money(product['precio'])} y hay {product['stock']} disponible(s). ¿Cuántas unidades deseas?", ["1", "2", "Cancelar compra"])


def payment_methods_for_cart(cart):
    methods = ["Transferencia bancaria", "Efectivo", "Tarjeta"]
    eligible_categories = {"audifonos", "adaptadores"}
    if cart and all(normalize(str(item.get("category", ""))) in eligible_categories for item in cart):
        methods += ["Addi", "Sistecrédito"]
    return methods


def purchase_flow(session, message, text):
    flow = session["flow"]
    if purchase_cancel_intent(text):
        clear_purchase_state(session)
        return response("Compra cancelada. El carrito fue limpiado.", ["Ver catálogo"])
    if flow == "quantity":
        if not text.isdigit() or int(text) < 1:
            return response("Indica una cantidad válida, por ejemplo: 1.")
        product = get_product(session["data"]["product_id"])
        quantity = int(text)
        if not product or quantity > product["stock"]:
            return response("No hay suficiente stock para esa cantidad. Elige una cantidad menor.")
        session["cart"] = [{"product_id": product["id"], "name": product["nombre"], "price": product["precio"], "quantity": quantity, "category": product["categoria"]}]
        session["flow"] = "checkout_confirm"
        return response(f"🛒 Carrito\n{quantity} × {product['nombre']}\nSubtotal: {money(product['precio'] * quantity)}\n\nEl envío se cotiza según la transportadora. ¿Deseas finalizar el pedido?", ["Finalizar compra", "Cancelar compra"])
    if flow == "checkout_confirm":
        if "finalizar" not in text:
            return response("Selecciona “Finalizar compra” o “Cancelar compra”.", ["Finalizar compra", "Cancelar compra"])
        session["flow"] = "customer_name"
        return response("Escribe tu nombre completo para registrar el pedido.")
    if flow == "customer_name":
        session["data"]["name"] = message.strip(); session["flow"] = "customer_phone"
        return response("Ahora escribe tu número de teléfono (10 dígitos).")
    if flow == "customer_phone":
        phone = re.sub(r"\D", "", message)
        if len(phone) != 10:
            return response("El teléfono debe tener 10 dígitos. Inténtalo nuevamente.")
        session["data"]["phone"] = phone; session["flow"] = "address"
        return response("Escribe la dirección de entrega. Para recoger en el local, escribe: Recoger en tienda.")
    if flow == "address":
        session["data"]["address"] = message.strip(); session["flow"] = "payment"
        methods = payment_methods_for_cart(session["cart"])
        payment_note = " Addi y Sistecrédito aplican únicamente para Audífonos y Adaptadores." if "Addi" in methods else ""
        return response("Elige el método de pago." + payment_note, methods)
    if flow == "payment":
        methods = {"transferencia bancaria": "Transferencia bancaria", "efectivo": "Efectivo", "tarjeta": "Tarjeta", "addi": "Addi", "sistecredito": "Sistecrédito"}
        method = methods.get(text)
        if not method:
            return response("Elige uno de los métodos mostrados.", payment_methods_for_cart(session["cart"]))
        if method in {"Addi", "Sistecrédito"} and any(item["category"] not in {"Audífonos", "Adaptadores"} for item in session["cart"]):
            return response(f"{method} solo está disponible para Audífonos y Adaptadores. Elige otro método.", payment_methods_for_cart(session["cart"]))
        try:
            code, subtotal = create_order({"name": session["data"]["name"], "phone": session["data"]["phone"]}, session["cart"], session["data"]["address"], method)
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            session["cart"], session["flow"], session["data"] = [], None, {}
            return response(str(error), ["Ver catálogo", "Hablar con un asesor"])
        session["cart"], session["flow"], session["data"] = [], None, {}
        return response(f"✅ Pedido registrado\nCódigo: {code}\nSubtotal de productos: {money(subtotal)}\nEnvío: por cotizar con la transportadora\nPago: {method}\nEstado: Pendiente\n\nUn asesor confirmará el costo final de envío y el pago.", ["Ver catálogo", "Consultar pedido", "Hablar con un asesor"])


def repair_status_message(repair):
    quote = "Pendiente de cotizacion por Zonatech."
    if repair["cotizacion"] is not None:
        quote = f"Cotizacion: {money(repair['cotizacion'])}."
        if repair["observaciones_cotizacion"]:
            quote += f" Observaciones: {repair['observaciones_cotizacion']}"
        if repair["respuesta_cotizacion"]:
            quote += f" Respuesta: {repair['respuesta_cotizacion']}."
    return response(
        f"REPARACION {repair['codigo']}\n\nEquipo: {repair['marca']} {repair['modelo']} ({repair['tipo_equipo']})\n"
        f"Problema: {repair['problema']}\nEstado: {repair['estado']}\n{quote}\n\n"
        "El diagnostico es gratuito. El tiempo habitual puede ser el mismo dia, segun la reparacion. La garantia depende del tipo de servicio.",
        ["Hablar con un asesor", "Quiero reparar mi equipo"],
    )


def repair_flow(session, message, text):
    flow = session["flow"]
    if "cancelar" in text:
        session["flow"], session["data"] = None, {}
        return response("Registro o consulta de reparacion cancelado.", ["Quiero reparar mi equipo"])
    if flow == "repair_name":
        if len(message.strip()) < 3:
            return response("Escribe tu nombre completo para continuar.")
        session["data"]["name"] = message.strip(); session["flow"] = "repair_phone"
        return response("Escribe tu numero de telefono de 10 digitos.", ["Cancelar reparacion"])
    if flow == "repair_phone":
        phone = re.sub(r"\D", "", message)
        if len(phone) != 10:
            return response("El telefono debe tener 10 digitos. Intentalo nuevamente.")
        session["data"]["phone"] = phone; session["flow"] = "repair_type"
        return response("Que tipo de equipo deseas registrar?", ["Celular", "Computador", "Otro equipo", "Cancelar reparacion"])
    if flow == "repair_type":
        session["data"]["type"] = message.strip(); session["flow"] = "repair_brand"
        return response("Indica la marca del equipo.", ["Samsung", "Xiaomi", "Motorola", "Vivo", "Tecno", "iPhone", "Computadores"])
    if flow == "repair_brand":
        session["data"]["brand"] = message.strip(); session["flow"] = "repair_model"
        return response("Indica el modelo del equipo, por ejemplo: Galaxy A55.")
    if flow == "repair_model":
        session["data"]["model"] = message.strip(); session["flow"] = "repair_problem"
        return response("Cual es el dano o servicio requerido?", ["Cambio de pantalla", "Cambio de bateria", "Cambio de tapa o carcasa", "Puerto de carga", "Microfono", "Auricular", "Memorias RAM", "Hardware de computadores", "Diagnostico"])
    if flow == "repair_problem":
        session["data"]["problem"] = message.strip(); session["flow"] = "repair_description"
        return response("Agrega una descripcion adicional o escribe 'Sin detalles'.", ["Sin detalles", "Cancelar reparacion"])
    if flow == "repair_description":
        session["data"]["description"] = "" if text in {"sin detalles", "ninguno", "no"} else message.strip()
        code = create_repair({"name": session["data"]["name"], "phone": session["data"]["phone"]}, session["data"])
        session["flow"], session["data"] = None, {}
        return response(f"Tu equipo fue registrado correctamente.\n\nCodigo de reparacion: {code}\n\nGuardalo para consultar el estado. El diagnostico es gratuito. No hay cotizacion registrada; Zonatech debe confirmar el precio y la garantia.", ["Consultar mi reparacion", "Hablar con un asesor"])
    if flow == "repair_lookup_code":
        code = message.strip().upper()
        if not re.fullmatch(r"ZT-R-\d{8}-\d{4}", code):
            return response("El codigo debe tener este formato: ZT-R-20260910-0001.", ["Cancelar consulta"])
        session["data"] = {"repair_code": code}; session["flow"] = "repair_lookup_phone"
        return response("Escribe el numero de telefono usado en el registro (10 digitos).", ["Cancelar consulta"])
    if flow == "repair_lookup_phone":
        phone = re.sub(r"\D", "", message)
        if len(phone) != 10:
            return response("El telefono debe tener 10 digitos. Intentalo nuevamente.")
        repair = get_repair_by_code_and_phone(session["data"]["repair_code"], phone)
        session["flow"], session["data"] = None, {}
        register_repair_lookup_result(session, found=bool(repair))
        if not repair:
            return response("No encontramos una reparacion con esos datos. Verifica el codigo y numero de telefono o habla con un asesor.", ["Consultar mi reparacion", "Hablar con un asesor"])
        return repair_status_message(repair)


def start_repair(session):
    session["flow"], session["data"] = "repair_name", {}
    return response("Vamos a registrar tu reparacion. Escribe tu nombre completo.", ["Cancelar reparacion"])


def start_repair_lookup(session):
    remaining = repair_lookup_locked(session)
    if remaining is not None:
        minutes = max(1, math.ceil(remaining.total_seconds() / 60))
        return response(f"Por seguridad, bloqueamos temporalmente la consulta de reparaciones por demasiados intentos fallidos. Intenta de nuevo en {minutes} minuto(s), o habla con un asesor.", ["Hablar con un asesor"])
    session["flow"], session["data"] = "repair_lookup_code", {}
    return response("Escribe tu codigo de reparacion. Ejemplo: ZT-R-20260910-0001.", ["Cancelar consulta"])


def select_last_product(session, text):
    products = last_result_products(session)
    product, ordinal_requested = ordinal_result_product(products, text)
    if not ordinal_requested:
        return None
    if not product:
        return response("No quiero confundirme 😊 ¿Me dices el nombre del producto que te interesa?")
    if re.search(r"\b(?:quiero(?: comprar)?|me llevo|comprar|como lo compro)\b", text):
        return start_purchase(session, product["id"])
    session["last_selected_id"] = product["id"]
    return product_card(product)


def purchase_from_last_results(session, text):
    pending = session.get("pending_result_purchase")
    if pending and re.fullmatch(r"[1-5]", text):
        index = int(text) - 1
        if index < len(pending):
            session.pop("pending_result_purchase", None)
            return start_purchase(session, pending[index])
        return response("Elige un número de los productos mostrados.")
    if not re.search(r"\b(?:quiero comprar uno|quiero uno|comprar uno)\b", text):
        return None
    results = session.get("last_results", [])
    if not results:
        return response("Primero necesito mostrarte productos para que elijas uno.", ["Ver catálogo"])
    if len(results) == 1:
        return start_purchase(session, results[0])
    session["pending_result_purchase"] = results.copy()
    options = [str(index) for index in range(1, len(results) + 1)]
    return response("¿Cuál producto quieres comprar? Responde con el número de la lista mostrada.", options)


def compare_last_products(session):
    results = session.get("last_results", [])
    if len(results) < 2:
        return response("Para comparar necesito mostrarte al menos dos productos. ¿Qué estás buscando?")
    products = [get_product(product_id) for product_id in results[:2]]
    if not all(products):
        return response("No tengo suficiente información para comparar esos productos ahora. Puedes volver al catálogo.", ["Ver catálogo"])
    first, second = products
    return response(
        f"Puedo compararte los datos registrados:\n\n"
        f"1. {first['nombre']}: {money(first['precio'])} · {'Disponible' if first['stock'] > 0 else 'Agotado'}\n"
        f"2. {second['nombre']}: {money(second['precio'])} · {'Disponible' if second['stock'] > 0 else 'Agotado'}\n\n"
        "No tengo suficiente información técnica en el catálogo para compararlos por rendimiento.",
        [f"Ver producto #{first['id']}", f"Ver producto #{second['id']}", "Hablar con un asesor"],
    )


def purchase_selected_product(session, text):
    if not re.search(r"\b(?:quiero (?:ese|esa)|como lo compro)\b", text):
        return None
    product_id = session.get("last_selected_id")
    if not product_id:
        return response("Para no confundirme, abre primero el producto que quieres comprar.", ["Ver catálogo"])
    product = get_product(product_id)
    if not product:
        return response("Ese producto ya no está activo en el catálogo.", ["Ver catálogo"])
    return start_purchase(session, product["id"])


def process_message(message, session_id="default"):
    session = session_for(session_id)
    control_text = normalize(message.strip())
    text, uncertain = fuzzy_normalize(control_text)

    # 1. FLUJOS ACTIVOS (Req 5 y 7: Compras y Reparaciones)
    if session.get("flow"):
        if session["flow"].startswith("repair_"):
            return repair_flow(session, message, text)
        if purchase_cancel_intent(control_text):
            clear_purchase_state(session)
            return response("Compra cancelada. El carrito fue limpiado.", ["Ver catálogo"])
        if explicit_catalog_request(control_text):
            clear_purchase_state(session)
        else:
            return purchase_flow(session, message, text)

    # 2. INFORMACIÓN DIRECTA (Req 1, 6, 9, 10, 11: Pagos, Garantía, Asesor, Menú)
    if payment_info_intent(control_text) or any(phrase in text for phrase in [
        "pagos y envios", "metodo de pago", "metodos de pago", "formas de pago",
        "medios de pago"
    ]):
        return payments_and_shipping_info()
    if any(phrase in text for phrase in ["ubicacion", "donde estan", "donde queda", "direccion", "horario", "horarios"]):
        return location_and_hours_info()
    if "garantia" in text:
        return warranty_info()
    if re.search(r"(?:asesor|persona|humano|humana|hablar con alguien|necesito ayuda|contactar|atencion personalizada)", control_text):
        return advisor_info()
    if any(option in control_text for option in ["menu", "inicio", "volver", "principal"]) or re.fullmatch(r"(?:hola+|hey|buenas?|buenos dias|buenas tardes|buenas noches|que tal)(?:\s+.*)?", text):
        session["flow"], session["data"] = None, {}
        clear_conversation(session)
        return main_menu()
    if "comprar productos" in control_text or text in {"catalogo", "ver catalogo", "muestrame el catalogo", "quiero ver el catalogo", "que tienen disponible", "que productos venden"}:
        return catalog_menu()

    # 3. REPARACIONES DIRECTAS (Req 7 y 8)
    if repair_intent(control_text):
        return start_repair(session)
    if any(phrase in control_text for phrase in ["consultar mi reparacion", "consultar reparacion", "estado de reparacion", "como va mi reparacion"]):
        return start_repair_lookup(session)

    # 4. CONTEXTO ESPECIAL AUDÍFONOS (Req 4: Música, Llamadas...)
    conversation = session.get("conversation", {})
    if re.fullmatch(r"(?:musica|llamadas|gaming|uso diario)", control_text) and normalize(str(conversation.get("interest", ""))) == "audifonos":
        conversational_response = conversational_search(session, message, control_text)
        if conversational_response:
            return conversational_response

    # 5. CONTEXTO POSICIONAL Y RESULTADOS ANTERIORES (Req 2 y 3)
    if text.startswith("ver producto #") or text.startswith("comprar #"):
        product_id = re.search(r"#(\d+)", text)
        if product_id:
            product = get_product(int(product_id.group(1)))
            if not product:
                return response("Ese producto ya no está activo en el catálogo.", ["Ver catálogo"])
            if text.startswith("comprar"):
                return start_purchase(session, product["id"])
            session["last_selected_id"] = product["id"]
            return product_card(product)

    displayed_follow_up = last_results_follow_up(session, control_text)
    if displayed_follow_up: return displayed_follow_up

    result_follow_up = result_conversation(session, control_text)
    if result_follow_up: return result_follow_up

    selected = select_last_product(session, control_text)
    if selected: return selected

    pending_purchase = purchase_from_last_results(session, control_text)
    if pending_purchase: return pending_purchase

    selected_purchase = purchase_selected_product(session, text)
    if selected_purchase: return selected_purchase

    if re.search(r"\b(?:cual es mejor|cual me recomiendas|que me recomiendas|que recomiendas|cual recomiendas|cual elegirias|cual deberia comprar|cual me conviene|que comprarias tu|entre estos)\b", control_text):
        return recommend_from_context(session)

    next_page = bool(re.search(r"\b(?:siguiente(?:s)?(?: pagina)?|pagina siguiente|muestrame(?: los)? siguientes|muestra(?: los)? siguientes|ver mas|muestrame mas|muestra mas)\b", control_text)) or control_text == "mas"
    previous_page = bool(re.search(r"\b(?:pagina anterior|anterior)\b", control_text))
    if next_page or previous_page:
        options = session.get("last_search", {})
        if not options: return catalog_menu()
        total = last_search_total(session)
        pages = math.ceil(total / PAGE_SIZE) if total else 1
        if next_page and options.get("page", 1) >= pages:
            return response("Ya estás viendo la última página de esos resultados.", ["Ver catálogo"])
        options["page"] = max(1, options.get("page", 1) + (1 if next_page else -1))
        return show_products(session, **options)

    # 6. FILTROS ESTRUCTURADOS (Req 3: "Quiero un iPhone", "Quiero audífonos JBL")
    discovery = None
    if re.search(r"\b(?:quiero|necesito|busco|recomiendas?|cambiar)\b", text) and product_interest(text):
        discovery = start_discovery(session, text)
    if discovery:
        return discovery

    explicit_query, explicit_category, explicit_max_price, explicit_brand = parse_catalog_search(text)
    explicit_category = explicit_category or category_for_text(text)

    iphone_phone_request = (
        "iphone" in text
        and (explicit_category is None or normalize(explicit_category) in {"celulares", "iphone"})
        and not re.search(r"\b(?:cable|cables|cargador|cargadores|cabeza|forro|forros|vidrio|vidrios|hidrogel|audifono|audifonos|cosas|accesorios|productos)\b", text)
        and bool(re.search(r"\b(?:quiero|busco|necesito|tienes|tienen|tiene|hay|venden|vende|muestrame|muestra|ver)\b", text))
    )
    if iphone_phone_request:
        categories = {normalize(row["categoria"]): row["categoria"] for row in list_categories()}
        explicit_category = categories.get("celulares")
        explicit_brand = active_brands().get("apple")
        explicit_query = re.sub(r"\biphone\b", " ", explicit_query)
        explicit_query = " ".join(explicit_query.split())

    explicit_request = bool(re.search(r"\b(?:muestrame|muestra|ver|quiero|busco|necesito|tienes|tienen|tiene|hay|venden|vende)\b", text))
    structured_filters = bool(explicit_category or explicit_brand)

    if (explicit_request or explicit_catalog_request(text)) and structured_filters:
        conversation = conversation_for(session)
        conversation.clear()
        conversation.update({
            "interest": explicit_category,
            "category": explicit_category,
            "brand": explicit_brand,
            "query": explicit_query,
            "budget": explicit_max_price,
            "stage": "search",
        })
        return show_products(
            session, explicit_query, explicit_category, explicit_max_price,
            "disponible" in text, brand=explicit_brand, raw_message=message,
        )

    conversational_response = conversational_search(session, message, control_text)
    if conversational_response:
        return conversational_response

    # =====================================================================
    # 7. MOTOR SEMÁNTICO (El núcleo de búsqueda IA libre para lo demás)
    # =====================================================================
    semantic_matches = semantic_product_matches(message)
    if semantic_matches:
        # Esto guarda los resultados en sesión. 
        # ¡Así Req 3 ("Cual es el mas barato") funciona también con la IA!
        return show_semantic_matches(session, semantic_matches)

    if uncertain:
        return response("No estoy seguro de qué quisiste decir 😅 ¿Puedes intentarlo con otras palabras o decirme qué buscas?", ["Ver catálogo", "Hablar con un asesor"])

    return response("No encontré opciones exactas en el catálogo con esas palabras. ¿Podrías darme otro detalle o nombre?", ["Ver catálogo", "Hablar con un asesor"])