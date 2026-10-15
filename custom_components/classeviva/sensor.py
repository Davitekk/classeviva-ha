"""Sensori ClasseViva.

Ogni sensore espone un valore "riassuntivo" come stato e i dati utili (lista) negli
attributi (`items`), pensati per le schede Markdown. Gli allegati includono già una
`download_url` firmata che passa dalla vista /api/classeviva/download.
Nota: lo stato di un sensore in HA è max 255 caratteri: le liste stanno negli attributi.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import parse_anno
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

# Quante comunicazioni lette esportare, oltre a quelle non lette prioritarie.
# HA tronca gli attributi troppo grandi nel recorder: ~16 KB è il tetto pratico.
NOTICEBOARD_LIMIT = 20
# Lascia 1 KiB per gli attributi aggiunti da Home Assistant sotto il limite Recorder.
NOTICEBOARD_ATTRIBUTE_BUDGET = 15 * 1024
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
SCRUTINI_LIMIT = 20


def _in_school_year(value, year_start: int) -> bool:
    """True when an ISO date belongs to September 1 through August 31."""
    if not value:
        return False
    try:
        event_date = date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    return date(year_start, 9, 1) <= event_date < date(year_start + 1, 9, 1)


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
                return async_sign_path(self.coordinator.hass, path, timedelta(hours=12))
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
            "anno": self._data.get("grades_year") or self.coordinator.etichetta_anno or None,
            "per_materia": self._data.get("medie_materia", []),
            "per_periodo": self._data.get("medie_periodo", {}),
        }


class GradesSensor(_Base):
    """Voti dal portale web; se non arrivano, ripiega su quelli dell'API REST."""

    _key = "grades"
    _label = "Voti"
    _icon = "mdi:numeric"

    def _items(self, limit: int | None = VOTI_LIMIT) -> list[dict]:
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
                for v in (voti if limit is None else voti[:limit])
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
            for g in (
                self._data.get("grades", [])
                if limit is None
                else self._data.get("grades", [])[:limit]
            )
        ]

    @property
    def native_value(self):
        return len(self._data.get("voti") or self._data.get("grades", []))

    @property
    def extra_state_attributes(self):
        voti = self._data.get("voti") or []
        return {
            "anno": self._data.get("grades_year") or self.coordinator.etichetta_anno or None,
            "fonte": self._data.get("voti_fonte"),
            "insufficienze": sum(1 for v in voti if v.get("negativo")),
            "periodi": self._data.get("periodi", {}),
            "items": self._items(),
        }

    def card_attributes(self) -> dict:
        """Attributi completi per la risposta WebSocket della card."""
        attributes = dict(self.extra_state_attributes)
        attributes["items"] = self._items(limit=None)
        return attributes


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
        return self._attributes()

    def _attributes(
        self,
        item_limit: int | None = NOTICEBOARD_LIMIT,
        text_limit: int | None = TESTO_MAX,
        school_year: int | None = None,
        byte_budget: int | None = NOTICEBOARD_ATTRIBUTE_BUDGET,
    ) -> dict:
        # Le non lette hanno priorità. Fra le lette, tieni le più recenti.
        tutte_le_com = self._data.get("noticeboard", [])
        if school_year is not None:
            tutte_le_com = [
                c for c in tutte_le_com
                if _in_school_year(c.get("pubDT"), school_year)
            ]
        if item_limit is None:
            priorita = list(enumerate(tutte_le_com))
        else:
            non_lette = [
                (indice, c)
                for indice, c in enumerate(tutte_le_com)
                if not c.get("readStatus", True)
            ]
            lette_recenti = [
                (indice, c)
                for indice, c in enumerate(tutte_le_com)
                if c.get("readStatus", True)
            ][:item_limit]
            priorita = non_lette + lette_recenti

        attributi = {
            "totale": len(tutte_le_com),
            "attive": sum(1 for c in tutte_le_com if c.get("cntValidInRange")),
            "mostrate": 0,
            "omesse": len(tutte_le_com),
            "items": [],
        }

        def aggiorna_conteggi() -> None:
            attributi["mostrate"] = len(attributi["items"])
            attributi["omesse"] = len(tutte_le_com) - attributi["mostrate"]

        def misura() -> int:
            # ensure_ascii=True sovrastima il testo UTF-8 e conta la forma JSON
            # con i caratteri non ASCII convertiti in escape.
            return len(json.dumps(attributi, ensure_ascii=True).encode("utf-8"))

        candidati = []
        for indice, c in priorita:
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
            testo_originale = c.get("text") or ""
            limite_testo = (
                len(testo_originale)
                if text_limit is None
                else min(len(testo_originale), text_limit)
            )
            item = {
                "data": (c.get("pubDT") or "")[:10],
                "titolo": c.get("cntTitle"),
                "letto": bool(c.get("readStatus")),
                # cntValidInRange: la comunicazione è ancora nel suo periodo
                # di validità. Le scadute restano in bacheca, come sul sito.
                "attiva": bool(c.get("cntValidInRange")),
                "valida_fino": c.get("cntValidTo"),
                "testo": testo_originale[:limite_testo] if byte_budget is None else "",
                "troncato": (
                    len(testo_originale) > limite_testo
                    if byte_budget is None
                    else bool(testo_originale)
                ),
                "pub_id": c.get("pubId"),
                "allegati": attachments,
            }
            candidati.append((indice, item, testo_originale, limite_testo))

        if byte_budget is None:
            attributi["items"] = [item for _, item, _, _ in candidati]
            aggiorna_conteggi()
            return attributi

        # Riserva prima lo spazio per i dati e tutti i link agli allegati.
        # Le comunicazioni non lette precedono quelle lette.
        inclusi = []
        for indice, item, testo_originale, limite_testo in candidati:
            attributi["items"].append(item)
            aggiorna_conteggi()
            if misura() <= byte_budget:
                inclusi.append((indice, item, testo_originale, limite_testo))
            else:
                attributi["items"].pop()
                aggiorna_conteggi()

        # Usa i byte rimasti per il testo, seguendo le priorità. Accorcia il
        # testo prima di omettere una comunicazione e segnala ogni taglio.
        for _, item, testo_originale, limite_testo in inclusi:
            basso = 0
            alto = limite_testo
            while basso < alto:
                medio = (basso + alto + 1) // 2
                item["testo"] = testo_originale[:medio]
                item["troncato"] = medio < len(testo_originale)
                if misura() <= byte_budget:
                    basso = medio
                else:
                    alto = medio - 1
            item["testo"] = testo_originale[:basso]
            item["troncato"] = basso < len(testo_originale)

        # Mantieni l'ordine della sorgente nell'interfaccia dopo aver usato la
        # priorità delle comunicazioni non lette per assegnare lo spazio.
        per_indice = {indice: item for indice, item, _, _ in inclusi}
        attributi["items"] = [per_indice[i] for i in sorted(per_indice)]
        aggiorna_conteggi()
        return attributi


class NotesSensor(_Base):
    """Note disciplinari e annotazioni dei docenti (dal portale web).

    Il REST le riporta sempre a zero: se il portale non risponde restano quelle
    REST, così il sensore non perde comunque i dati che ha.
    """

    _key = "notes"
    _label = "Note"
    _icon = "mdi:alert-circle"

    def _note(
        self,
        limit: int | None = NOTE_LIMIT,
        text_limit: int | None = NOTE_TESTO_MAX,
    ) -> list[dict]:
        web = self._data.get("note_web")
        def testo(value):
            value = value or ""
            return value if text_limit is None else value[:text_limit]

        if web:
            return [
                {
                    "data": n.get("data"),
                    "tipo": n.get("tipo"),
                    "categoria": n.get("categoria"),
                    "richiamo": bool(n.get("richiamo")),
                    "letta": n.get("letta", True),
                    "autore": n.get("autore"),
                    "testo": testo(n.get("testo")),
                }
                for n in (web if limit is None else web[:limit])
            ]
        return [
            {
                "data": n.get("evtDate"),
                "tipo": n.get("_type"),
                "categoria": CATEGORIA_DISCIPLINARE,
                "autore": n.get("authorName"),
                "testo": testo(n.get("evtText") or n.get("evtValue")),
            }
            for n in (
                self._data.get("notes", [])
                if limit is None
                else self._data.get("notes", [])[:limit]
            )
        ]

    @property
    def native_value(self):
        return len(self._data.get("note_web") or self._data.get("notes", []))

    @property
    def extra_state_attributes(self):
        return self._attributes()

    def _attributes(
        self,
        limit: int | None = NOTE_LIMIT,
        text_limit: int | None = NOTE_TESTO_MAX,
    ) -> dict:
        note = self._note(limit=limit, text_limit=text_limit)
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
        return self._attributes()

    def _attributes(self, limit: int | None = SCRUTINI_LIMIT) -> dict:
        items = []
        documenti = self._data.get("scrutini", [])
        if limit is not None:
            documenti = documenti[:limit]
        for i, d in enumerate(documenti):
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
        return self._attributes()

    def _attributes(
        self,
        item_limit: int | None = AGENDA_LIMIT,
        note_limit: int | None = NOTE_MAX,
    ) -> dict:
        agenda = self._agenda()
        items = []
        # i piu' recenti per primi: sono quelli che interessa vedere in dashboard
        for e in sorted(
            agenda,
            key=lambda x: x.get("evtDatetimeBegin") or x.get("evtDate") or "",
            reverse=True,
        ):
            if item_limit is not None and len(items) >= item_limit:
                break
            note = e.get("notes") or ""
            if note_limit is not None:
                note = note[:note_limit]
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


def card_data(coordinator: ClasseVivaCoordinator, entry_id: str) -> dict:
    """Build the complete, school-year-scoped payload for the Lovelace card.

    This response is sent over Home Assistant's authenticated WebSocket API.
    It is not written to sensor state attributes or the recorder.
    """
    data = coordinator.data or {}
    year_start = coordinator.anno
    if year_start is None:
        for source in (
            data.get("grades_year"),
            data.get("agenda_anno"),
            data.get("note_anno"),
            data.get("scrutini_anno"),
        ):
            if source:
                try:
                    year_start = parse_anno(source)
                except ValueError:
                    continue
                if year_start is not None:
                    break

    media_sensor = MediaSensor(coordinator, entry_id)
    grades_sensor = GradesSensor(coordinator, entry_id)
    noticeboard_sensor = NoticeboardSensor(coordinator, entry_id)
    notes_sensor = NotesSensor(coordinator, entry_id)
    scrutini_sensor = ScrutiniSensor(coordinator, entry_id)
    agenda_sensor = AgendaSensor(coordinator, entry_id)

    grade_items = grades_sensor._items(limit=None)
    if year_start is not None:
        grade_items = [v for v in grade_items if _in_school_year(v.get("data"), year_start)]
    grade_attributes = grades_sensor.card_attributes()
    grade_attributes["items"] = grade_items

    # Recompute summaries from the same complete, date-filtered web grades
    # displayed in the card. The regular sensor keeps its compact stored value.
    web_grades = data.get("voti") or []
    if web_grades:
        if year_start is not None:
            web_grades = [v for v in web_grades if _in_school_year(v.get("data"), year_start)]
        summaries = calcola_medie(web_grades)
        media_value = summaries["media"]
        media_attributes = dict(media_sensor.extra_state_attributes)
        media_attributes["per_materia"] = summaries["medie_materia"]
        media_attributes["per_periodo"] = summaries["medie_periodo"]
        grade_attributes["insufficienze"] = sum(1 for v in web_grades if v.get("negativo"))
    else:
        media_value = media_sensor.native_value
        media_attributes = media_sensor.extra_state_attributes

    board_attributes = noticeboard_sensor._attributes(
        item_limit=None,
        text_limit=None,
        school_year=year_start,
        byte_budget=None,
    )
    board_items = board_attributes["items"]
    board_value = sum(1 for item in board_items if not item.get("letto", True))

    note_attributes = notes_sensor._attributes(limit=None, text_limit=None)
    if year_start is not None:
        note_attributes["items"] = [
            item for item in note_attributes["items"]
            if _in_school_year(item.get("data"), year_start)
        ]
    note_items = note_attributes["items"]
    note_attributes["disciplinari"] = sum(
        1 for item in note_items if item.get("categoria") == CATEGORIA_DISCIPLINARE
    )
    note_attributes["annotazioni"] = sum(
        1 for item in note_items if item.get("categoria") == CATEGORIA_ANNOTAZIONE
    )
    note_attributes["richiami"] = sum(1 for item in note_items if item.get("richiamo"))
    note_attributes["da_leggere"] = sum(1 for item in note_items if not item.get("letta", True))

    agenda_attributes = agenda_sensor._attributes(item_limit=None, note_limit=None)
    if year_start is not None:
        agenda_attributes["items"] = [
            item for item in agenda_attributes["items"]
            if _in_school_year(item.get("inizio"), year_start)
        ]
    agenda_attributes["totale"] = len(agenda_attributes["items"])

    scrutini_attributes = scrutini_sensor._attributes(limit=None)
    if year_start is not None:
        items = []
        for item in scrutini_attributes["items"]:
            if not item.get("anno"):
                items.append(item)
                continue
            try:
                item_year = parse_anno(item["anno"])
            except ValueError:
                continue
            if item_year in (None, year_start):
                items.append(item)
        scrutini_attributes["items"] = items
    scrutini_attributes["ultimo"] = (
        scrutini_attributes["items"][0]["descrizione"]
        if scrutini_attributes["items"] else None
    )

    return {
        "nome": coordinator.first_name,
        "anno": year_start,
        "entita": {
            "media": {"state": media_value, "attributes": media_attributes},
            "voti": {"state": len(grade_items), "attributes": grade_attributes},
            "bacheca": {"state": board_value, "attributes": board_attributes},
            "note": {"state": len(note_items), "attributes": note_attributes},
            "scrutini": {
                "state": len(scrutini_attributes["items"]),
                "attributes": scrutini_attributes,
            },
            "agenda": {
                "state": len(agenda_attributes["items"]),
                "attributes": agenda_attributes,
            },
        },
    }
