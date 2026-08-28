"""Sensori ClasseViva.

Ogni sensore espone un valore "riassuntivo" come stato e i dati utili (lista) negli
attributi (`items`), pensati per le schede Markdown. Gli allegati includono già una
`download_url` firmata che passa dalla vista /api/classeviva/download.
Nota: lo stato di un sensore in HA è max 255 caratteri: le liste stanno negli attributi.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlencode

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClasseVivaCoordinator
from .download import DOWNLOAD_URL
from .web import (
    CATEGORIA_ANNOTAZIONE,
    CATEGORIA_DISCIPLINARE,
    TIPI_RUMOROSI,
    calcola_medie,
)

_LOGGER = logging.getLogger(__name__)

# async_sign_path firma gli URL della vista di download: senza firma il browser,
# che in una normale navigazione non manda il token di Home Assistant, riceve 401.
# La funzione vive in http.auth; in alcune versioni è ri-esportata da http.
async_sign_path = None
for _modulo in ("homeassistant.components.http.auth", "homeassistant.components.http"):
    try:
        async_sign_path = __import__(_modulo, fromlist=["async_sign_path"]).async_sign_path
        break
    except (ImportError, AttributeError):
        continue
if async_sign_path is None:  # pragma: no cover
    _LOGGER.error(
        "async_sign_path non trovato: i link agli allegati non saranno firmati "
        "e daranno 401. Segnala la versione di Home Assistant in uso."
    )

# quante voci esportare negli attributi (per non gonfiare lo stato)
# HA tronca gli attributi troppo grandi nel recorder: ~16 KB è il tetto pratico.
NOTICEBOARD_LIMIT = 20
VOTI_LIMIT = 100
AGENDA_LIMIT = 50
# Le annotazioni dei docenti sono spesso lunghissime (link + istruzioni):
# negli attributi si tronca, il calendario invece le tiene per intero.
NOTE_MAX = 150
# Il testo intero di 20 comunicazioni sfiora il tetto degli attributi: negli
# attributi si tronca, il testo completo resta nel servizio di lettura.
TESTO_MAX = 500
NOTE_LIMIT = 40
NOTE_TESTO_MAX = 300


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: ClasseVivaCoordinator = hass.data[DOMAIN][entry.entry_id]
    eid = entry.entry_id
    async_add_entities(
        [
            MediaSensor(coordinator, eid),
            GradesSensor(coordinator, eid),
            DidacticsSensor(coordinator, eid),
            NoticeboardSensor(coordinator, eid),
            NotesSensor(coordinator, eid),
            ScrutiniSensor(coordinator, eid),
            AgendaSensor(coordinator, eid),
        ]
    )


class _Base(CoordinatorEntity, SensorEntity):
    # has_entity_name: il nome dello studente sta nel DISPOSITIVO, l'entità porta
    # solo la sua etichetta ("Voti"). Senza, Home Assistant lo ripete due volte
    # e genera id come sensor.classeviva_nome_voti_nome.
    _attr_has_entity_name = True

    _key = ""
    _label = ""
    _icon = "mdi:school"

    def __init__(self, coordinator: ClasseVivaCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_name = self._label
        self._attr_unique_id = f"cvv_{self._key}_{coordinator.id_unico}"
        self._attr_icon = self._icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.id_unico)},
            "name": coordinator.nome_dispositivo,
            "manufacturer": "Spaggiari",
            "model": "Registro Elettronico",
        }

    @property
    def _data(self) -> dict:
        return self.coordinator.data or {}

    def _download_url(self, **params) -> str:
        """URL firmata (valida ~12h) verso la vista di download degli allegati."""
        params["entry_id"] = self._entry_id
        path = f"{DOWNLOAD_URL}?{urlencode(params)}"
        if async_sign_path is not None:
            try:
                return async_sign_path(self.hass, path, timedelta(hours=12))
            except Exception as err:  # noqa: BLE001
                # Meglio saperlo: un URL non firmato dà 401 al momento del click.
                _LOGGER.warning("Firma dell'URL di download non riuscita: %s", err)
                return path
        return path


class MediaSensor(_Base):
    """Media generale.

    Nessuna unità di misura: "/10" non è un'unità che HA conosce e un sensore
    numerico con un'unità non standard può finire in `unknown` se nel registro
    delle entità ne è rimasta memorizzata una diversa da un avvio precedente.
    """

    _key = "media"
    _label = "Media"
    _icon = "mdi:calculator-variant"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self):
        media = self._data.get("media")
        if media is None and self._data.get("voti"):
            # Difesa: se ci sono voti la media non deve mai restare vuota.
            media = calcola_medie(self._data["voti"]).get("media")
        return media

    @property
    def extra_state_attributes(self):
        return {
            "anno": self._data.get("grades_year"),
            "per_materia": self._data.get("medie_materia", []),
            "per_periodo": self._data.get("medie_periodo", {}),
        }


class GradesSensor(_Base):
    """Voti dal portale web; se non arrivano, ripiega su quelli dell'API REST."""

    _key = "grades"
    _label = "Voti"
    _icon = "mdi:numeric"

    def _items(self) -> list[dict]:
        voti = self._data.get("voti")
        if voti:
            # "periodo" per esteso si ripete su ogni voto: negli attributi tengo
            # la sigla (S1/S2/...) e la legenda una volta sola in "periodi".
            return [
                {
                    "data": v.get("data") or v.get("data_breve"),
                    "materia": v.get("materia"),
                    "voto": v.get("voto"),
                    "valore": v.get("valore"),
                    "tipo": v.get("tipo"),
                    "periodo": v.get("sessione"),
                    "negativo": v.get("negativo"),
                    "in_media": not v.get("escluso_media"),
                }
                for v in voti[:VOTI_LIMIT]
            ]
        return [
            {
                "data": g.get("evtDate"),
                "materia": g.get("subjectDesc"),
                "voto": g.get("displayValue") or g.get("decimalValue"),
                "valore": g.get("decimalValue"),
                "tipo": g.get("componentDesc") or g.get("evtCode"),
                "note": g.get("notesForFamily"),
            }
            for g in self._data.get("grades", [])[:VOTI_LIMIT]
        ]

    @property
    def native_value(self):
        return len(self._data.get("voti") or self._data.get("grades", []))

    @property
    def extra_state_attributes(self):
        voti = self._data.get("voti") or []
        return {
            "anno": self._data.get("grades_year"),
            "fonte": self._data.get("voti_fonte"),
            "insufficienze": sum(1 for v in voti if v.get("negativo")),
            "periodi": self._data.get("periodi", {}),
            "items": self._items(),
        }


class DidacticsSensor(_Base):
    _key = "didactics"
    _label = "File Docenti"
    _icon = "mdi:folder-account"

    @property
    def native_value(self):
        total = 0
        for teacher in self._data.get("didactics", []):
            for folder in teacher.get("folders", []):
                total += len(folder.get("contents", []))
        return total

    @property
    def extra_state_attributes(self):
        teachers = []
        for t in self._data.get("didactics", []):
            files = []
            for folder in t.get("folders", []):
                for c in folder.get("contents", []):
                    entry = {
                        "nome": c.get("contentName"),
                        "cartella": folder.get("folderName"),
                        "data": c.get("shareDT"),
                    }
                    if c.get("objectType") == "file" and c.get("contentId"):
                        entry["download_url"] = self._download_url(
                            kind="didactics", cid=c["contentId"]
                        )
                    elif c.get("link"):
                        entry["link"] = c.get("link")
                    files.append(entry)
            teachers.append({"docente": (t.get("teacherName") or "").strip(), "file": files})
        return {"items": teachers}


class NoticeboardSensor(_Base):
    _key = "noticeboard"
    _label = "Bacheca"
    _icon = "mdi:bulletin-board"

    @property
    def native_value(self):
        return sum(1 for i in self._data.get("noticeboard", []) if not i.get("readStatus", True))

    @property
    def extra_state_attributes(self):
        # le più recenti, più TUTTE le non lette: sono quelle che interessano di
        # più e restare fuori dal limite le renderebbe invisibili in dashboard
        tutte_le_com = self._data.get("noticeboard", [])
        selezionate = list(tutte_le_com[:NOTICEBOARD_LIMIT])
        gia = {id(c) for c in selezionate}
        for c in tutte_le_com[NOTICEBOARD_LIMIT:]:
            if not c.get("readStatus", True) and id(c) not in gia:
                selezionate.append(c)

        items = []
        for c in selezionate:
            attachments = []
            for a in c.get("attachments", []):
                attachments.append(
                    {
                        "nome": a.get("fileName"),
                        "download_url": self._download_url(
                            kind="noticeboard",
                            evt=c.get("evtCode"),
                            pub=c.get("pubId"),
                            num=a.get("attachNum"),
                        ),
                    }
                )
            testo = c.get("text") or ""
            items.append(
                {
                    "data": (c.get("pubDT") or "")[:10],
                    "titolo": c.get("cntTitle"),
                    "letto": bool(c.get("readStatus")),
                    # cntValidInRange: la comunicazione è ancora nel suo periodo
                    # di validità. Le scadute restano in bacheca, come sul sito.
                    "attiva": bool(c.get("cntValidInRange")),
                    "valida_fino": c.get("cntValidTo"),
                    "testo": testo[:TESTO_MAX],
                    "troncato": len(testo) > TESTO_MAX,
                    "pub_id": c.get("pubId"),
                    "allegati": attachments,
                }
            )
        tutte = self._data.get("noticeboard", [])
        return {
            "totale": len(tutte),
            "attive": sum(1 for c in tutte if c.get("cntValidInRange")),
            "mostrate": len(items),
            "items": items,
        }


class NotesSensor(_Base):
    """Note disciplinari e annotazioni dei docenti (dal portale web).

    Il REST le riporta sempre a zero: se il portale non risponde restano quelle
    REST, così il sensore non perde comunque i dati che ha.
    """

    _key = "notes"
    _label = "Note"
    _icon = "mdi:alert-circle"

    def _note(self) -> list[dict]:
        web = self._data.get("note_web")
        if web:
            return [
                {
                    "data": n.get("data"),
                    "tipo": n.get("tipo"),
                    "categoria": n.get("categoria"),
                    "richiamo": bool(n.get("richiamo")),
                    "letta": n.get("letta", True),
                    "autore": n.get("autore"),
                    "testo": (n.get("testo") or "")[:NOTE_TESTO_MAX],
                }
                for n in web[:NOTE_LIMIT]
            ]
        return [
            {
                "data": n.get("evtDate"),
                "tipo": n.get("_type"),
                "categoria": CATEGORIA_DISCIPLINARE,
                "autore": n.get("authorName"),
                "testo": (n.get("evtText") or n.get("evtValue") or "")[:NOTE_TESTO_MAX],
            }
            for n in self._data.get("notes", [])[:NOTE_LIMIT]
        ]

    @property
    def native_value(self):
        return len(self._data.get("note_web") or self._data.get("notes", []))

    @property
    def extra_state_attributes(self):
        note = self._note()
        return {
            "anno": self._data.get("note_anno"),
            "fonte": self._data.get("note_fonte"),
            "disciplinari": sum(1 for n in note if n["categoria"] == CATEGORIA_DISCIPLINARE),
            "annotazioni": sum(1 for n in note if n["categoria"] == CATEGORIA_ANNOTAZIONE),
            "richiami": sum(1 for n in note if n.get("richiamo")),
            "da_leggere": sum(1 for n in note if not n.get("letta", True)),
            "items": note,
        }


class ScrutiniSensor(_Base):
    """Pagelle ed esiti di scrutinio pubblicati.

    Lo stato è il numero di documenti: quando la scuola ne pubblica uno nuovo
    il valore cambia, ed è su quello che si aggancia un'automazione di avviso.
    """

    _key = "scrutini"
    _label = "Scrutini"
    _icon = "mdi:file-certificate"

    @property
    def native_value(self):
        return len(self._data.get("scrutini", []))

    @property
    def extra_state_attributes(self):
        items = []
        for i, d in enumerate(self._data.get("scrutini", [])):
            items.append(
                {
                    "descrizione": d.get("descrizione"),
                    "periodo": d.get("sessione"),
                    "anno": d.get("anno"),
                    "presa_visione_richiesta": d.get("richiede_presa_visione"),
                    "tipo": d.get("tipo"),
                    # il PDF vive dietro la sessione del portale: passa dal proxy
                    "download_url": self._download_url(kind="scrutinio", indice=i),
                }
            )
        return {
            "anno": self._data.get("scrutini_anno"),
            "ultimo": items[0]["descrizione"] if items else None,
            "items": items,
        }


class AgendaSensor(_Base):
    _key = "agenda"
    _label = "Agenda"
    _icon = "mdi:calendar"

    def _agenda(self) -> list[dict]:
        """Agenda senza le prenotazioni d'aula, che hanno un calendario dedicato."""
        return [
            e for e in self._data.get("agenda", [])
            if (e.get("tipo") or "").lower() not in TIPI_RUMOROSI
        ]

    @property
    def native_value(self):
        return len(self._agenda())

    @property
    def extra_state_attributes(self):
        agenda = self._agenda()
        items = []
        # i piu' recenti per primi: sono quelli che interessa vedere in dashboard
        for e in sorted(
            agenda,
            key=lambda x: x.get("evtDatetimeBegin") or x.get("evtDate") or "",
            reverse=True,
        )[:AGENDA_LIMIT]:
            note = (e.get("notes") or "")[:NOTE_MAX]
            items.append(
                {
                    "inizio": e.get("evtDatetimeBegin") or e.get("evtDate"),
                    "fine": e.get("evtDatetimeEnd"),
                    "materia": e.get("subjectDesc"),
                    "note": note,
                    "autore": e.get("authorName"),
                    "tipo": "Compiti" if e.get("evtCode") == "AGHW" else "Verifica",
                }
            )
        return {
            "anno": self._data.get("agenda_anno"),
            "fonte": self._data.get("agenda_fonte"),
            "totale": len(agenda),
            "items": items,
        }
