"""Vista HTTP che permette di scaricare gli allegati di ClasseViva dal browser.

Gli allegati (bacheca, file dei docenti) richiedono il token Z-Auth-Token, quindi
un semplice <a href> non funzionerebbe. Questa vista, protetta dall'auth di Home
Assistant, chiede a Spaggiari l'URL reale (cdndoc, con token AUTH in query) e ci
reindirizza il browser. Le URL firmate vengono generate nei sensori.
"""
from __future__ import annotations

import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import BASE_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_URL = "/api/classeviva/download"


class ClasseVivaDownloadView(HomeAssistantView):
    """Reindirizza al file reale su cdndoc.spaggiari.eu."""

    url = DOWNLOAD_URL
    name = "api:classeviva:download"
    requires_auth = True  # accetta anche le URL firmate (async_sign_path)

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        entry_id = q.get("entry_id")
        kind = q.get("kind")

        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            return web.Response(status=404, text="Integrazione ClasseViva non trovata")

        api = coordinator.api
        try:
            if kind == "noticeboard":
                real = await api.get_download_url(
                    "noticeboard", evt=q.get("evt"), pub=q.get("pub"), num=q.get("num")
                )
            elif kind == "didactics":
                real = await api.get_download_url("didactics", content_id=q.get("cid"))
            elif kind == "scrutinio":
                return await self._scrutinio(coordinator, q.get("indice"))
            else:
                return web.Response(status=400, text="Tipo allegato non valido")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Download ClasseViva fallito: %s", err)
            return web.Response(status=502, text="Errore ClasseViva")

        if not real:
            return web.Response(status=404, text="Allegato non disponibile")
        # 302 verso il file reale (cdndoc si autentica da solo con la query AUTH)
        raise web.HTTPFound(real)

    async def _scrutinio(self, coordinator, indice) -> web.StreamResponse:
        """Consegna il PDF di una pagella.

        Qui non basta un redirect: l'URL del documento vale solo con il cookie di
        sessione del portale, quindi il file va scaricato e ritrasmesso.
        Scaricarlo NON registra la presa visione, che sul portale è un passaggio
        distinto e volutamente non automatizzato.
        """
        try:
            documenti = (coordinator.data or {}).get("scrutini", [])
            documento = documenti[int(indice)]
        except (TypeError, ValueError, IndexError):
            return web.Response(status=404, text="Documento non trovato")

        contenuto, tipo = await coordinator.web.async_scarica_scrutinio(documento)
        if not contenuto:
            return web.Response(status=502, text="Documento non disponibile da ClasseViva")

        nome = (documento.get("descrizione") or "documento").replace('"', "")
        mime = (tipo or "application/pdf").split(";")[0].strip()

        if mime == "text/html":
            # I pagellini sono pagine, non PDF: senza un <base> i loro CSS e le
            # immagini (indirizzi relativi al portale) non verrebbero caricati.
            host = documento.get("host") or BASE_HOST
            base = f'<base href="https://{host}/sol/app/default/">'.encode()
            testo = contenuto
            punto = testo.lower().find(b"<head")
            if punto >= 0:
                chiusura = testo.find(b">", punto)
                testo = testo[: chiusura + 1] + base + testo[chiusura + 1 :]
            else:
                testo = base + testo
            return web.Response(body=testo, content_type="text/html", charset="utf-8")

        return web.Response(
            body=contenuto,
            content_type=mime,
            headers={"Content-Disposition": f'inline; filename="{nome}.pdf"'},
        )
