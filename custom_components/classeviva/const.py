"""Costanti per l'integrazione ClasseViva."""

DOMAIN = "classeviva"

CONF_USERNAME = "Username"
CONF_PASSWORD = "Password"
CONF_ANNO = "Anno"

# Valore che significa "usa l'anno piu' recente che l'account puo' vedere".
ANNO_ULTIMO = "Ultimo"

# Header estratti dall'app ufficiale Android (mitmproxy, ago 2026).
# ATTENZIONE: la chiave è "Tg1NWEwNGIgIC0K" e lo User-Agent "cvvs/5.12.02/android".
# Questi vengono cambiati periodicamente da Spaggiari: se il login inizia a dare
# errore "apikey.3" vanno ri-catturati da una nuova versione dell'app.
API_KEY = "Tg1NWEwNGIgIC0K"
USER_AGENT = "cvvs/5.12.02/android"

BASE_HOST = "web.spaggiari.eu"

# Il portale WEB legacy (voti) rifiuta gli header dell'app: serve un UA da browser.
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0"
)

# Ogni quanto aggiornare i dati (i token durano 90 minuti, quindi 30 è comodo).
UPDATE_MINUTES = 30
