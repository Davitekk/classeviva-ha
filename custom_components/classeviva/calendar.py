"""Entità Calendario per l'agenda ClasseViva.

In Home Assistant il colore si sceglie per CALENDARIO, non per singolo evento:
per avere compiti, annotazioni e prenotazioni d'aula di colori diversi servono
quindi entità separate, che poi si colorano nella card `calendar` della
dashboard. Qui se ne creano quattro:

    Agenda              tutto tranne le prenotazioni d'aula (riepilogo)
    Compiti             solo i compiti assegnati
    Note dei docenti    annotazioni, avvisi, aule virtuali
    Prenotazioni aula   solo le prenotazioni d'aula

L'entità "Agenda" mantiene lo unique_id storico, così chi la usa già in una
dashboard non se la vede sparire.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ClasseVivaCoordinator
from .web import (
    TIPI_RUMOROSI,
    TIPO_AULA_VIRTUALE,
    TIPO_COMPITI,
    TIPO_NOTA,
    TIPO_PRENOTAZIONE,
)

# (suffisso unique_id, etichetta, icona, tipi ammessi)
# tipi = None significa "tutto tranne il rumore" (le prenotazioni d'aula).
CALENDARI = (
    (None, "Agenda", "mdi:calendar-text", None),
    ("compiti", "Compiti", "mdi:notebook-edit", {TIPO_COMPITI}),
    ("note", "Note dei docenti", "mdi:message-text",
     {TIPO_NOTA, TIPO_AULA_VIRTUALE, "avviso"}),
    ("aule", "Prenotazioni aula", "mdi:door-open", {TIPO_PRENOTAZIONE}),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: ClasseVivaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ClasseVivaCalendar(coordinator, suffisso, etichetta, icona, tipi)
        for suffisso, etichetta, icona, tipi in CALENDARI
    )


def _tipo_di(item: dict) -> str:
    """Tipo dell'evento; i dati REST non ce l'hanno, si deduce dal codice."""
    tipo = (item.get("tipo") or "").lower()
    if tipo:
        return tipo
    return TIPO_COMPITI if item.get("evtCode") == "AGHW" else TIPO_NOTA


def _parse(value):
    """Converte una stringa data/ora ClasseViva in datetime o date."""
    if not value:
        return None
    try:
        # gestisce sia "2026-09-15T08:00:00+02:00" sia "2026-09-15"
        if "T" in value:
            return dt_util.as_local(datetime.fromisoformat(value))
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None


def _to_event(item: dict) -> CalendarEvent | None:
    begin = item.get("evtDatetimeBegin") or item.get("evtDateBegin") or item.get("evtDate")
    end = item.get("evtDatetimeEnd") or item.get("evtDateEnd")
    start = _parse(begin)
    if start is None:
        return None
    stop = _parse(end) or start

    subject = (item.get("subjectDesc") or "").strip()
    notes = (item.get("notes") or "").strip()
    author = (item.get("authorName") or "").strip()
    code = item.get("evtCode", "")
    kind = "Compiti" if code == "AGHW" else "Verifica/Nota"

    summary = subject or kind
    if notes:
        summary = f"{subject or kind}: {notes[:60]}"

    is_full_day = bool(item.get("isFullDay"))
    if is_full_day or not isinstance(start, datetime):
        # evento "tutto il giorno": CalendarEvent vuole oggetti date
        s = start if not isinstance(start, datetime) else start.date()
        e = stop if not isinstance(stop, datetime) else stop.date()
        if e <= s:
            e = s + timedelta(days=1)
        return CalendarEvent(start=s, end=e, summary=summary, description=notes or None, location=author or None)

    if not isinstance(stop, datetime) or stop <= start:
        stop = start + timedelta(hours=1)
    return CalendarEvent(start=start, end=stop, summary=summary, description=notes or None, location=author or None)


class ClasseVivaCalendar(CoordinatorEntity, CalendarEntity):
    """Espone una fetta dell'agenda come calendario nella barra laterale di HA."""

    def __init__(
        self,
        coordinator: ClasseVivaCoordinator,
        suffisso: str | None,
        etichetta: str,
        icona: str,
        tipi: set[str] | None,
    ) -> None:
        super().__init__(coordinator)
        self._tipi = tipi
        radice = coordinator.id_unico
        # Il nome dello studente sta nel dispositivo: qui basta l'etichetta.
        self._attr_has_entity_name = True
        self._attr_name = etichetta
        # Senza suffisso resta lo unique_id storico dell'unico calendario iniziale.
        self._attr_unique_id = (
            f"cvv_calendar_{radice}" if suffisso is None
            else f"cvv_calendar_{suffisso}_{radice}"
        )
        self._attr_icon = icona
        self._attr_device_info = {
            "identifiers": {(DOMAIN, radice)},
        }

    def _accetta(self, item: dict) -> bool:
        tipo = _tipo_di(item)
        if self._tipi is None:
            # calendario riepilogativo: tutto tranne le prenotazioni d'aula
            return tipo not in TIPI_RUMOROSI
        return tipo in self._tipi

    def _all_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data or {}
        events = []
        for item in data.get("agenda", []):
            if not self._accetta(item):
                continue
            ev = _to_event(item)
            if ev:
                events.append(ev)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        upcoming = []
        for ev in self._all_events():
            end = ev.end
            end_dt = end if isinstance(end, datetime) else datetime.combine(end, datetime.min.time(), now.tzinfo)
            if end_dt >= now:
                upcoming.append((ev, ev.start))
        if not upcoming:
            return None
        upcoming.sort(key=lambda t: (t[1] if isinstance(t[1], datetime) else datetime.combine(t[1], datetime.min.time(), now.tzinfo)))
        return upcoming[0][0]

    async def async_get_events(self, hass, start_date, end_date) -> list[CalendarEvent]:
        def _as_dt(v):
            return v if isinstance(v, datetime) else datetime.combine(v, datetime.min.time(), start_date.tzinfo)

        out = []
        for ev in self._all_events():
            e_start = _as_dt(ev.start)
            e_end = _as_dt(ev.end)
            if e_end >= start_date and e_start <= end_date:
                out.append(ev)
        return out
