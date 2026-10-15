import os
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()
GRAPH = "https://graph.facebook.com/v26.0"
APP_ID = "1883954939449044"
WABA_ID = "1763956511537199"


def post(url, data, headers=None):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers=headers or {}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


callback = "https://squabble-fool-obscurity.ngrok-free.dev/webhook"

print("1) Registrar URL y campo messages:")
print(post(f"{GRAPH}/{APP_ID}/subscriptions", {
    "object": "whatsapp_business_account",
    "callback_url": callback,
    "verify_token": os.environ["WHATSAPP_VERIFY_TOKEN"],
    "fields": "messages",
    "access_token": f"{APP_ID}|{os.environ['WHATSAPP_APP_SECRET']}",
}))

print("2) Suscribir la app a la cuenta de WhatsApp Business:")
print(post(f"{GRAPH}/{WABA_ID}/subscribed_apps", {},
           {"Authorization": f"Bearer {os.environ['WHATSAPP_ACCESS_TOKEN']}"}))