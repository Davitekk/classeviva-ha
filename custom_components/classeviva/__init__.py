"""Integrazione ClasseViva Spaggiari per Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import CONF_ANNO, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import ClasseVivaCoordinator
from .download import ClasseVivaDownloadView

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "calendar"]

SERVIZIO_LEGGI = "leggi_comunicazione"
SERVIZIO_TESTI = "carica_testi_bacheca"
SERVIZIO_AGGIORNA = "aggiorna"
SCHEMA_LEGGI = vol.Schema(
    {
        vol.Required("pub_id"): cv.string,
        vol.Optional("entry_id"): cv.string,
    }
)
SCHEMA_ISTANZA = vol.Schema({vol.Optional("entry_id"): cv.string})


def leggi(dati: dict, chiave: str, default=None):
    """Legge un campo della configurazione ignorando maiuscole e minuscole.

    I nomi dei campi sono definiti da CONF_* in const.py e possono cambiare di
    forma ("username" -> "Username"): le configurazioni gia' salvate conservano
    pero' la vecchia scrittura, e senza questo passaggio andrebbero in KeyError.
    """
    if chiave in dati:
        return dati[chiave]
    voluta = chiave.lower()
    for nome, valore in dati.items():
        if nome.lower() == voluta:
            return valore
    return default


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione a partire da una config entry (UI)."""
    coordinator = ClasseVivaCoordinator(
        hass,
        leggi(entry.data, CONF_USERNAME),
        leggi(entry.data, CONF_PASSWORD),
        leggi(entry.data, CONF_ANNO),
    )
    # Primo refresh: se fallisce l'auth, HA mostra l'errore/avvia il reauth.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Registra una sola volta la vista per il download degli allegati.
    if not hass.data[DOMAIN].get("_download_view"):
        hass.http.register_view(ClasseVivaDownloadView(hass))
        hass.data[DOMAIN]["_download_view"] = True

    _registra_servizi(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _coordinatori(hass: HomeAssistant) -> dict[str, ClasseVivaCoordinator]:
    """Le istanze configurate, escluse le chiavi di servizio interne."""
    return {
        k: v for k, v in hass.data.get(DOMAIN, {}).items()
        if isinstance(v, ClasseVivaCoordinator)
    }


def _registra_servizi(hass: HomeAssistant) -> None:
    """Registra i servizi dell'integrazione (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVIZIO_LEGGI):
        return

    def _scelte(call: ServiceCall) -> dict:
        """Istanze su cui agire: quella indicata, o tutte se non specificata."""
        istanze = _coordinatori(hass)
        entry_id = call.data.get("entry_id")
        if entry_id:
            scelte = {entry_id: istanze[entry_id]} if entry_id in istanze else {}
        else:
            scelte = istanze
        if not scelte:
            raise HomeAssistantError("Nessuna configurazione ClasseViva trovata.")
        return scelte

    async def leggi_comunicazione(call: ServiceCall) -> dict:
        """Scarica il testo di una comunicazione e la marca come letta.

        È l'equivalente di aprirla nell'app: il registro la segna come letta.
        Per questo l'aggiornamento automatico non lo fa da solo sulle non lette.
        """
        pub_id = str(call.data["pub_id"])

        for coordinator in _scelte(call).values():
            for c in (coordinator.data or {}).get("noticeboard", []):
                if str(c.get("pubId")) != pub_id:
                    continue
                testo = await coordinator.api.leggi_comunicazione(
                    c.get("evtCode"), c.get("pubId")
                )
                if testo is None:
                    raise HomeAssistantError(
                        f"Comunicazione {pub_id} non leggibile da ClasseViva."
                    )
                # Il testo viene messo nei dati in memoria ma NON si forza un
                # aggiornamento: rinfrescare qui farebbe sparire la comunicazione
                # dall'elenco "da leggere" nell'istante in cui la si apre, prima
                # di poterla leggere. Lo stato si allinea al prossimo giro o
                # premendo "Aggiorna".
                c["text"] = testo
                return {
                    "titolo": c.get("cntTitle"),
                    "data": (c.get("pubDT") or "")[:10],
                    "testo": testo,
                    "allegati": [a.get("fileName") for a in c.get("attachments", [])],
                }

        raise HomeAssistantError(f"Comunicazione {pub_id} non trovata in bacheca.")

    hass.services.async_register(
        DOMAIN,
        SERVIZIO_LEGGI,
        leggi_comunicazione,
        schema=SCHEMA_LEGGI,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def carica_testi_bacheca(call: ServiceCall) -> dict:
        """Scarica dal portale i testi mancanti SENZA segnare nulla come letto."""
        totale = 0
        for coordinator in _scelte(call).values():
            totale += await coordinator.async_testi_bacheca()
        return {"completate": totale}

    hass.services.async_register(
        DOMAIN,
        SERVIZIO_TESTI,
        carica_testi_bacheca,
        schema=SCHEMA_ISTANZA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def aggiorna(call: ServiceCall) -> None:
        """Forza subito un aggiornamento, senza aspettare il ciclo periodico."""
        for coordinator in _scelte(call).values():
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVIZIO_AGGIORNA, aggiorna, schema=SCHEMA_ISTANZA)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Aggiorna le configurazioni salvate da versioni precedenti."""
    if entry.version == 1:
        # La v2 aggiunge la scelta dell'anno scolastico: le vecchie configurazioni
        # seguono l'anno piu' recente, che e' il comportamento che avevano prima.
        # Le chiavi vengono anche riscritte nella forma attuale dei CONF_*.
        nuovi = {
            CONF_USERNAME: leggi(entry.data, CONF_USERNAME),
            CONF_PASSWORD: leggi(entry.data, CONF_PASSWORD),
            CONF_ANNO: leggi(entry.data, CONF_ANNO),
        }
        hass.config_entries.async_update_entry(entry, data=nuovi, version=2)
        _LOGGER.debug("Config entry %s migrata alla versione 2", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Rimuove l'integrazione."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not _coordinatori(hass):
            for servizio in (SERVIZIO_LEGGI, SERVIZIO_TESTI, SERVIZIO_AGGIORNA):
                hass.services.async_remove(DOMAIN, servizio)
    return unload_ok
