"""DataUpdateCoordinator per ClasseViva."""
from __future__ import annotations

import logging
from datetime import timedelta

import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ClasseVivaApi, ClasseVivaAuthError, ClasseVivaApiError
from .const import DOMAIN, UPDATE_MINUTES
from .web import ClasseVivaWebApi, ClasseVivaWebError, chiave_titolo

_LOGGER = logging.getLogger(__name__)


class ClasseVivaCoordinator(DataUpdateCoordinator):
    """Coordina il download periodico dei dati da ClasseViva."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        anno: int | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_MINUTES),
        )
        self.anno = anno
        self.api = ClasseVivaApi(async_get_clientsession(hass), username, password, anno)
        # Il portale web va su una sessione DEDICATA: usa i cookie di sessione PHP
        # e non vanno mescolati con quelli condivisi del resto di Home Assistant.
        self.web = ClasseVivaWebApi(
            async_create_clientsession(hass), username, password
        )

    @property
    def first_name(self) -> str:
        return self.api.first_name

    @property
    def etichetta_anno(self) -> str:
        """Anno scolastico scelto, come "25/26"; vuoto se si segue il piu' recente."""
        if self.anno is None:
            return ""
        return f"{self.anno % 100:02d}/{(self.anno + 1) % 100:02d}"

    @property
    def nome_dispositivo(self) -> str:
        """Nome del dispositivo: include l'anno solo se e' stato fissato."""
        etichetta = self.etichetta_anno
        return f"ClasseViva {self.first_name}" + (f" {etichetta}" if etichetta else "")

    @property
    def id_unico(self) -> str:
        """Radice degli unique_id: distingue le istanze con anni diversi."""
        base = self.api.student_id or "unknown"
        return f"{base}_{self.anno}" if self.anno is not None else base

    async def _async_update_data(self) -> dict:
        try:
            async with async_timeout.timeout(30):
                data = await self.api.async_fetch()
        except ClasseVivaAuthError as err:
            # Solo credenziali rifiutate: qui la riautenticazione ci sta, perche'
            # senza intervento dell'utente non si va avanti. Il token scaduto
            # NON passa di qui: lo gestisce api.py rifacendo il login da solo.
            self.api.token = None
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClasseVivaApiError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Errore imprevisto ClasseViva: {err}") from err

        await self._aggiungi_voti_web(data)
        await self._aggiungi_agenda_web(data)
        await self._aggiungi_note_web(data)
        await self._aggiungi_scrutini_web(data)
        # NB: i testi delle comunicazioni non lette NON si scaricano qui.
        # Vedi async_testi_bacheca: si prendono solo su richiesta esplicita.
        self._conserva_precedenti(data)
        return data

    # sezioni che possono restare vuote se una delle due fonti non risponde
    _SEZIONI = (
        "voti", "medie_materia", "medie_periodo", "periodi", "media", "grades_year",
        "agenda", "note_web", "scrutini", "noticeboard", "didactics", "notes",
    )

    def _conserva_precedenti(self, data: dict) -> None:
        """Tiene i dati dell'ultimo aggiornamento riuscito per le sezioni vuote.

        Ogni ciclo ricostruisce tutto da zero: se il portale (o il REST) non
        risponde, quella sezione resterebbe vuota e in dashboard i dati
        sparirebbero, per poi ricomparire solo al giro dopo. Meglio mostrare
        il dato precedente, che e' comunque quello vero.
        """
        vecchi = self.data or {}
        for chiave in self._SEZIONI:
            if not data.get(chiave) and vecchi.get(chiave):
                data[chiave] = vecchi[chiave]
                _LOGGER.debug("Sezione '%s' non aggiornata: tengo il dato precedente", chiave)

    async def _aggiungi_note_web(self, data: dict) -> None:
        """Note disciplinari e annotazioni: il REST le espone sempre vuote."""
        data.setdefault("note_fonte", "rest")
        web = await self._chiedi_al_portale("note", self.web.async_fetch_note)
        if not web or not web.get("note"):
            return
        data["note_web"] = web["note"]
        data["note_anno"] = web.get("anno")
        data["note_fonte"] = web.get("fonte") or "web"

    async def _aggiungi_scrutini_web(self, data: dict) -> None:
        """Pagelle ed esiti di scrutinio pubblicati per lo studente."""
        data.setdefault("scrutini", [])
        web = await self._chiedi_al_portale("scrutini", self.web.async_fetch_scrutini)
        if not web or not web.get("scrutini"):
            return
        data["scrutini"] = web["scrutini"]
        data["scrutini_anno"] = web.get("anno")

    async def async_testi_bacheca(self) -> int:
        """Completa la bacheca con i testi presi dal portale, su richiesta.

        Il portale restituisce il testo insieme all'elenco, senza passare
        dall'endpoint REST che segnerebbe la comunicazione come letta. NON viene
        chiamato a ogni aggiornamento: durante le prove una comunicazione non
        letta è comunque passata a letta, e finché non è chiaro se dipenda da
        questa chiamata conviene che sia l'utente a decidere quando farla.

        Ritorna quante comunicazioni sono state completate.
        """
        comunicazioni = (self.data or {}).get("noticeboard") or []
        mancanti = [c for c in comunicazioni if not c.get("text")]
        if not mancanti:
            return 0
        try:
            async with async_timeout.timeout(30):
                testi = await self.web.async_fetch_testi_bacheca()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Testi bacheca dal portale non disponibili: %s", err)
            return 0

        completate = 0
        for c in mancanti:
            testo = testi.get(chiave_titolo(c.get("cntTitle")))
            if testo:
                c["text"] = testo
                c["testo_da_portale"] = True
                completate += 1
        if completate:
            self.async_set_updated_data(self.data)
        return completate

    async def _aggiungi_voti_web(self, data: dict) -> None:
        """Integra i voti presi dal portale web (l'API REST non li espone).

        Se lo scraping fallisce l'aggiornamento NON deve saltare: bacheca, file dei
        docenti e agenda restano validi, i voti semplicemente restano quelli REST.
        """
        data.setdefault("voti_fonte", "rest")
        web = await self._chiedi_al_portale("voti", self.web.async_fetch_voti)
        if not web or not web.get("voti"):
            return

        data["voti"] = web["voti"]
        data["medie_materia"] = web["medie_materia"]
        data["medie_periodo"] = web["medie_periodo"]
        data["periodi"] = web["periodi"]
        data["media"] = web["media"]
        data["grades_year"] = web.get("anno") or data.get("grades_year")
        data["voti_fonte"] = web.get("fonte") or "web"

    async def _aggiungi_agenda_web(self, data: dict) -> None:
        """Integra compiti, annotazioni e prenotazioni d'aula presi dal portale web.

        Il portale e' la fonte preferita anche ad anno iniziato: e' l'unica che
        distingue i tipi di evento (serve ai calendari separati per colore) e
        l'unica raggiungibile per l'anno concluso, visto che sul server storico
        gli endpoint REST a intervallo di date rifiutano le date di quell'anno.
        Se non risponde restano i dati REST, gia' nello stesso schema.
        """
        data.setdefault("agenda_fonte", "rest")
        web = await self._chiedi_al_portale("agenda", self.web.async_fetch_agenda)
        if not web or not web.get("agenda"):
            return

        data["agenda"] = web["agenda"]
        data["agenda_anno"] = web.get("anno")
        data["agenda_fonte"] = web.get("fonte") or "web"

    async def _chiedi_al_portale(self, cosa: str, metodo):
        """Chiama il portale web senza far fallire l'intero aggiornamento."""
        try:
            async with async_timeout.timeout(60):
                return await metodo(self.api.piani_anno())
        except ClasseVivaWebError as err:
            _LOGGER.warning("%s dal portale web non disponibili: %s", cosa.capitalize(), err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Errore imprevisto sul portale web (%s): %s", cosa, err)
        return None
