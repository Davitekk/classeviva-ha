"""Client per il registro WEB "classico" di ClasseViva (backend PHP legacy).

Perche' serve, visto che c'e' gia' l'API REST (api.py)?
    Perche' voti e agenda dell'anno concluso NON passano dall'API REST:
      * /rest/v1/students/{id}/grades torna sempre {"grades": []} (ed e'
        esattamente il motivo per cui anche l'app ufficiale non mostra nulla
        in "anno precedente");
      * gli endpoint a intervallo di date (agenda, lessons, absences) sul
        server storico accettano solo date dell'anno NUOVO ("invalid date
        range: dates must be between 20260901 and 20271231"), quindi l'agenda
        dell'anno concluso e' irraggiungibile via REST.
    Entrambe le cose vivono solo nel portale web, un'applicazione PHP separata.

Catena di autenticazione (ricostruita dall'HAR del browser, ago 2026):

 1. POST https://web.spaggiari.eu/auth-p7/app/default/AuthApi4.php?a=aLoginPwd
    form-urlencoded: cid, uid, pwd, pin, target
    -> JSON con data.auth.loggedIn=true e cookie PHPSESSID sul dominio
       ".spaggiari.eu" (wildcard: vale quindi anche per web25.spaggiari.eu).

 2. Per l'ANNO PRECEDENTE non basta il cookie: la sessione va "estesa" al
    server storico con un giro di SSO interno
        GET /home/app/default/xasapi.php?a=lap&bu=https://web25.spaggiari.eu
            &ru=/home/&fu=xasapi-ERROR.php
    (a=lap = "login anno precedente"). Senza questo passaggio web25 rimbalza
    su login.php?msasr=on anche con il PHPSESSID valido.

 3. Voti:  GET /cvv/app/default/genitori_voti.php  -> HTML da cui fare scraping.
    Agenda: POST /fml/app/default/agenda_studenti.php?ope=get_events
            (start/end come timestamp unix, piu' classe_id preso dalla pagina)
            -> JSON, la stessa sorgente che alimenta il fullCalendar del sito.

Il portale rifiuta lo User-Agent dell'app (403): serve quello di un browser.

Struttura della pagina voti:
    <tr class="riga_materia_componente" sessione="S1" materia_id="...">
        <div class="materia_desc">nome materia</div>
        <td class="registro cella_voto" componente="1" evento_id="12345">
            <span class="voto_data cella_data">23/10</span>
            <div class="cella_div f_reg_voto_positivo" title="Scritto/Grafico">
                <p class="...">7 1/2</p>
Le righe "riga_competenza_default" sono nascoste (display:none) e vanno saltate,
cosi' come le celle con evento_id vuoto (segnaposto della griglia).
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
from datetime import datetime
from html import unescape
from urllib.parse import urlsplit

import aiohttp

from .const import BASE_HOST, WEB_USER_AGENT

_LOGGER = logging.getLogger(__name__)

LOGIN_PATH = "/auth-p7/app/default/AuthApi4.php?a=aLoginPwd"
LOGIN_PAGE = "/home/app/default/login.php"
MENU_PATH = "/home/app/default/menu_webinfoschool_studenti.php?custcode="
VOTI_PATH = "/cvv/app/default/genitori_voti.php"
AGENDA_PATH = "/fml/app/default/agenda_studenti.php"
NOTE_PATH = "/fml/app/default/gioprof_note_studente.php"
BACHECA_PATH = "/sif/app/default/bacheca_personale.php"
SCRUTINI_PATH = "/sol/app/default/documenti_sol.php"
PUBBLICAZIONI_PATH = "/sol/app/default/pubblicazioni.php"
SSO_PREV_PATH = "/home/app/default/xasapi.php?a=lap&bu=https://{host}&ru=/home/&fu=xasapi-ERROR.php"

# Il portale usa "componente" per indicare il tipo di prova.
COMPONENTI = {"1": "Scritto/Grafico", "2": "Orale", "3": "Pratico"}

# Voti espressi a lettere (dalla legenda della pagina): non entrano nella media.
VOTI_LETTERA = {
    "b": "Buono", "dc": "Discreto", "ds": "Distinto", "i": "Insufficiente",
    "m": "Molto", "ms": "Moltissimo", "nav": "Non avvalentesi",
    "nc": "Non classificato", "nv": "Non valutato", "o": "Ottimo",
    "s": "Sufficiente",
}

# Simboli che modificano il voto base: mezzo punto e i quarti.
MODIFICATORI = {"½": 0.5, "+": 0.25, "-": -0.25}

# Tipi di evento dell'agenda, come li marca il fullCalendar del portale.
# Vengono scaricati tutti: e' calendar.py a dividerli in calendari distinti,
# perche' in Home Assistant il colore si assegna per calendario, non per evento.
TIPO_COMPITI = "compiti"
TIPO_NOTA = "nota"
TIPO_PRENOTAZIONE = "prenotazione"
TIPO_AULA_VIRTUALE = "aule_virtuali"

# Le prenotazioni d'aula ("3 AULA 3") sono due terzi degli eventi e ripetono
# sempre lo stesso titolo: restano fuori dal calendario riepilogativo e dal
# sensore, ma hanno un calendario tutto loro.
TIPI_RUMOROSI = {TIPO_PRENOTAZIONE}

_RE_SESSIONI = re.compile(r'<li><a href="#(S\d+)">(.*?)</a></li>')
_RE_RIGA = re.compile(
    r'<tr class="riga_materia_componente"[^>]*sessione="(?P<sess>[^"]*)"'
    r'[^>]*materia_id="(?P<mid>[^"]*)"(?P<body>.*?)</tr>', re.S)
_RE_MATERIA = re.compile(r'class="materia_desc"[^>]*>(?P<name>.*?)</div>', re.S)
_RE_CELLA = re.compile(
    r'<td class="registro cella_voto[^"]*"[^>]*componente="(?P<comp>\d+)"'
    r'[^>]*evento_codice="(?P<code>[^"]*)"[^>]*evento_id="(?P<eid>[^"]*)"[^>]*>'
    r'(?P<cell>.*?)</td>', re.S)
_RE_DATA = re.compile(r'class="voto_data cella_data">(?P<d>[^<]*)</span>')
_RE_VALORE = re.compile(
    r'class="(?P<cls>[^"]*cella_div[^"]*)"[^>]*title="(?P<title>[^"]*)"[^>]*>\s*'
    r'<p[^>]*>(?P<val>.*?)</p>', re.S)
_RE_VAR_JS = r'var\s+{nome}\s*=\s*"([^"]*)"'

# Pagina Note: tabella con Autore | Data | Nota | Tipo di nota.
_RE_TABELLA_NOTE = re.compile(r'<table[^>]+id="sort_table".*?<tbody>(?P<corpo>.*?)</tbody>', re.S)
_RE_RIGA_NOTE = re.compile(r"<tr[^>]*>(?P<riga>.*?)</tr>", re.S)
_RE_CELLA_NOTE = re.compile(r"<td[^>]*>(?P<cella>.*?)</td>", re.S)
# Una nota non ancora letta mostra un pulsante al posto del testo: confermarla
# chiama gioprof_note_studente.php con ope=inserisci_lettura, come la bacheca.
_RE_NOTA_DA_LEGGERE = re.compile(r'class="[^"]*leggi_nota', re.S)
_RE_NOTA_ID = re.compile(r'nota_id="([^"]*)"')
_RE_NOTA_EVENTO = re.compile(r'evento_codice="([^"]*)"')

# Pagina Scrutini: i documenti stanno in DUE posti. Quelli generati su richiesta
# (pagelle) in una variabile JavaScript; quelli gia' pronti (pagellini, recuperi)
# come righe HTML con l'indirizzo in "xhref".
_RE_DOCUMENTI = re.compile(r"var\s+documenti\s*=\s*(?P<json>\[.*?\])\s*;", re.S)
_RE_DOC_DIRETTO = re.compile(
    r'<span\s+xhref="(?P<url>[^"]+)"\s+class="(?P<cls>[^"]*)"\s+desc="(?P<desc>[^"]*)"'
    r'\s+sess="(?P<sess>[^"]*)"', re.S)

# Il portale distingue tre cose: le "Annotazioni del docente" (comunicazioni),
# i "Richiami" (materiale, compiti, comportamento: avvisi, non sanzioni) e le
# "Note disciplinari" vere e proprie. Le prime due stanno insieme fra le
# annotazioni - i richiami evidenziati - e solo le note restano disciplinari.
CATEGORIA_ANNOTAZIONE = "annotazione"
CATEGORIA_DISCIPLINARE = "disciplinare"


class ClasseVivaWebError(Exception):
    """Errore nel portale web legacy."""


def _testo(frammento: str) -> str:
    """Toglie i tag e normalizza le entita' HTML."""
    return htmllib.unescape(re.sub(r"<[^>]+>", "", frammento)).replace("\xa0", " ").strip()


def _riga_singola(testo: str) -> str:
    """Compatta gli a capo: i titoli dell'agenda sono spesso multiriga."""
    return " ".join((testo or "").split())


def voto_a_numero(voto: str) -> float | None:
    """Converte il voto testuale del registro in un numero.

    Convenzioni ClasseViva: mezzo = 0.5, "+" = un quarto in piu',
    "-" = un quarto in meno. I voti a lettera non sono convertibili.
    """
    v = (voto or "").strip().replace(" ", "")
    if not v:
        return None
    delta = 0.0
    while v and v[-1] in MODIFICATORI:
        delta += MODIFICATORI[v[-1]]
        v = v[:-1]
    if not v:
        return None
    try:
        return round(float(v.replace(",", ".")) + delta, 2)
    except ValueError:
        return None


def _data_completa(data: str, anno_inizio: int | None) -> str:
    """Da "23/10" a "2025-10-23": l'anno si deduce dall'anno scolastico.

    Settembre-dicembre stanno nell'anno di inizio, gennaio-agosto in quello dopo.
    """
    if not anno_inizio or not data or "/" not in data:
        return data
    try:
        giorno, mese = (int(x) for x in data.split("/")[:2])
    except ValueError:
        return data
    anno = anno_inizio if mese >= 9 else anno_inizio + 1
    return f"{anno:04d}-{mese:02d}-{giorno:02d}"


def parse_voti(pagina: str, anno_inizio: int | None = None) -> dict:
    """Estrae voti e periodi dall'HTML di genitori_voti.php."""
    periodi = {k: _testo(v) for k, v in _RE_SESSIONI.findall(pagina)}
    voti: list[dict] = []

    for riga in _RE_RIGA.finditer(pagina):
        sessione, corpo = riga.group("sess"), riga.group("body")
        m = _RE_MATERIA.search(corpo)
        materia = _testo(m.group("name")) if m else ""

        for cella in _RE_CELLA.finditer(corpo):
            if not cella.group("eid"):
                continue  # segnaposto vuoto della griglia
            contenuto = cella.group("cell")
            mv = _RE_VALORE.search(contenuto)
            if not mv:
                continue
            valore = _testo(mv.group("val"))
            if not valore:
                continue

            md = _RE_DATA.search(contenuto)
            data_breve = _testo(md.group("d")) if md else ""
            classi = mv.group("cls")

            voti.append({
                "sessione": sessione,
                "periodo": periodi.get(sessione, sessione),
                "materia": materia,
                "data": _data_completa(data_breve, anno_inizio),
                "data_breve": data_breve,
                "voto": valore,
                "valore": voto_a_numero(valore),
                "tipo": COMPONENTI.get(cella.group("comp"), cella.group("comp")),
                "prova": _testo(mv.group("title")),
                "descrizione": VOTI_LETTERA.get(valore.lower()),
                "negativo": "f_reg_voto_negativo" in classi,
                "escluso_media": "f_reg_voto_dettaglio" in classi,
                "evento_id": cella.group("eid"),
            })

    voti.sort(key=lambda v: (v["data"] or "", v["materia"]), reverse=True)
    return {"periodi": periodi, "voti": voti}


def _media(voti: list[dict]) -> float | None:
    vals = [v["valore"] for v in voti
            if v.get("valore") is not None and not v.get("escluso_media")]
    return round(sum(vals) / len(vals), 2) if vals else None


def calcola_medie(voti: list[dict]) -> dict:
    """Media generale, per materia e per periodo (solo voti numerici in media)."""
    per_materia: dict[str, list] = {}
    per_periodo: dict[str, list] = {}
    for v in voti:
        per_materia.setdefault(v["materia"], []).append(v)
        per_periodo.setdefault(v["periodo"], []).append(v)

    medie_materia = []
    for materia, elenco in sorted(per_materia.items()):
        media = _media(elenco)
        if media is not None:
            medie_materia.append({
                "materia": materia,
                "media": media,
                "voti": len(elenco),
                "insufficienze": sum(1 for v in elenco if v.get("negativo")),
            })

    return {
        "media": _media(voti),
        "medie_materia": medie_materia,
        "medie_periodo": {
            p: _media(e) for p, e in sorted(per_periodo.items())
            if _media(e) is not None
        },
    }


def parse_eventi(eventi: list[dict]) -> list[dict]:
    """Normalizza gli eventi del fullCalendar nello schema dell'API REST.

    Cosi' calendar.py e il sensore Agenda continuano a leggere gli stessi campi
    (evtDatetimeBegin/evtDatetimeEnd/subjectDesc/notes/authorName/evtCode) sia
    che i dati arrivino dal REST sia che arrivino dal portale. Il campo "tipo"
    e' in piu' rispetto al REST e serve a smistare gli eventi per calendario.
    """
    fuori: list[dict] = []
    for e in eventi or []:
        tipo = (e.get("tipo") or "").lower()
        inizio = (e.get("start") or "").replace(" ", "T")
        if not inizio:
            continue
        fine = (e.get("end") or "").replace(" ", "T")
        materia = _riga_singola(e.get("materia_desc") or "")
        autore = _riga_singola(e.get("autore_desc") or "")

        fuori.append({
            "evtDatetimeBegin": inizio,
            "evtDatetimeEnd": fine or inizio,
            "isFullDay": bool(e.get("allDay")),
            "subjectDesc": materia or autore,
            "notes": _riga_singola(e.get("title") or ""),
            "authorName": autore,
            "evtCode": "AGHW" if tipo == TIPO_COMPITI else "AGNT",
            "evtId": e.get("id"),
            "tipo": tipo,
        })

    fuori.sort(key=lambda x: x["evtDatetimeBegin"])
    return fuori


def chiave_titolo(titolo: str | None) -> str:
    """Titolo ridotto a forma confrontabile fra portale e API REST.

    Le due fonti scrivono lo stesso titolo con spaziature e apostrofi diversi,
    e il portale a volte lo tronca: si tiene solo l'inizio, in minuscolo e senza
    punteggiatura.
    """
    testo = re.sub(r"[^\w\s]", " ", (titolo or "").lower())
    return " ".join(testo.split())[:60]


def _data_iso(valore: str) -> str:
    """Da "10-11-2025" a "2025-11-10", per poter ordinare le date."""
    pezzi = (valore or "").strip().split("-")
    if len(pezzi) == 3 and len(pezzi[2]) == 4:
        return f"{pezzi[2]}-{pezzi[1]}-{pezzi[0]}"
    return valore


def parse_note(pagina: str) -> list[dict]:
    """Estrae note disciplinari e annotazioni da gioprof_note_studente.php."""
    tabella = _RE_TABELLA_NOTE.search(pagina)
    if not tabella:
        return []

    note: list[dict] = []
    for riga in _RE_RIGA_NOTE.finditer(tabella.group("corpo")):
        grezze = [c.group("cella") for c in _RE_CELLA_NOTE.finditer(riga.group("riga"))]
        if len(grezze) < 4:
            continue
        celle = [_testo(c) for c in grezze]
        autore, data, testo, tipo = celle[0], celle[1], celle[2], celle[3]

        # nella cella del testo c'e' il pulsante finche' la nota non e' stata letta
        da_leggere = bool(_RE_NOTA_DA_LEGGERE.search(grezze[2]))
        if da_leggere:
            testo = ""
        elif not testo and not tipo:
            continue
        minuscolo = tipo.lower()
        richiamo = "richiamo" in minuscolo
        # disciplinari = solo note e sanzioni; i richiami restano fra le annotazioni
        disciplinare = not richiamo and (
            "nota disciplinare" in minuscolo or "sanzione" in minuscolo
        )
        note.append({
            "autore": autore,
            "data": _data_iso(data),
            "testo": testo,
            "tipo": tipo,
            "categoria": CATEGORIA_DISCIPLINARE if disciplinare else CATEGORIA_ANNOTAZIONE,
            "richiamo": richiamo,
            "letta": not da_leggere,
            "nota_id": (_RE_NOTA_ID.search(grezze[2]) or [None, ""])[1] if da_leggere else "",
            "evento_codice": (_RE_NOTA_EVENTO.search(grezze[2]) or [None, ""])[1] if da_leggere else "",
        })

    note.sort(key=lambda n: n["data"], reverse=True)
    return note


def parse_scrutini(pagina: str) -> list[dict]:
    """Estrae i documenti di scrutinio dalla variabile JS della pagina."""
    fuori = []

    # 1) documenti generati su richiesta (pagelle): serviranno doctype e params
    m = _RE_DOCUMENTI.search(pagina)
    if m:
        try:
            documenti = json.loads(m.group("json"))
        except ValueError:
            documenti = []
            _LOGGER.debug("Elenco documenti scrutinio non leggibile")
        for d in documenti:
            if not isinstance(d, dict):
                continue
            fuori.append({
                "descrizione": d.get("desc") or "Documento",
                "sessione": d.get("sessione") or d.get("sess") or "",
                "doctype": d.get("doctype"),
                "params": d.get("params"),
                "anno": d.get("anno_scol"),
                "tipo": d.get("tipo_documento") or "",
                "url": None,
                # "conferma" = il portale chiede la presa visione prima di aprirlo
                "richiede_presa_visione": "conferma" in str(d.get("css_class") or ""),
            })

    # 2) documenti gia' pronti (pagellini, recuperi): hanno l'indirizzo pronto
    for d in _RE_DOC_DIRETTO.finditer(pagina):
        indirizzo = d.group("url")
        # i segnaposto ":params:" appartengono al modello di riga, non a un documento
        if not indirizzo or indirizzo.startswith(":"):
            continue
        sessione = d.group("sess")
        fuori.append({
            "descrizione": _testo(d.group("desc")) or "Documento",
            "sessione": "" if sessione.startswith(":") else sessione,
            "doctype": None,
            "params": None,
            "anno": None,
            "tipo": "pagina",
            "url": indirizzo,
            "richiede_presa_visione": "conferma" in d.group("cls"),
        })

    return fuori


def _maschera(testo: str) -> str:
    """Toglie da un testo da loggare gli indirizzi email e i numeri lunghi."""
    testo = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[email]", testo)
    return re.sub(r"\d{5,}", "#", testo)


_RE_REDIRECT_JS = re.compile(
    r"""(?:location(?:\.href)?\s*=(?!=)|location\.(?:replace|assign)\s*\()\s*['"]([^'"]+)['"]""",
    re.I)
_RE_REDIRECT_META = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh[^>]*content\s*=\s*["'][^"']*?url\s*=\s*([^"'>\s;]+)""",
    re.I)
_RE_RISORSE = re.compile(
    r"""<(?:script[^>]+src|link[^>]+href)\s*=\s*["']([^"']+)["']""", re.I)


def _senza_query(indirizzo: str) -> str:
    """Host e percorso di un indirizzo per il log: niente query, frammento, id di sessione, email."""
    parti = urlsplit(unescape(indirizzo).strip())
    return _maschera(f"{parti.netloc}{parti.path.split(';')[0]}")[:80]


def _elenco_indirizzi(indirizzi: list[str], massimo: int = 3) -> list[str]:
    visti: list[str] = []
    for indirizzo in indirizzi:
        voce = _senza_query(indirizzo)
        if voce and voce not in visti:
            visti.append(voce)
    return visti[:massimo]


def _riassunto_html(testo: str, massimo: int = 120, *, con_testo: bool = False) -> str:
    """Titolo, redirect e risorse di una risposta, per il log.

    Niente tag, script, email, numeri lunghi o query: dei redirect e delle
    risorse si tiene solo host e percorso. Il testo visibile (che potrebbe
    contenere nomi) si aggiunge solo con `con_testo`, per il log di debug.
    """
    titolo = re.search(r"<title[^>]*>(.*?)</title>", testo, re.I | re.S)
    visibile = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", testo)
    visibile = re.sub(r"(?s)<[^>]+>", " ", visibile)
    visibile = " ".join(unescape(visibile).split())
    titolo = " ".join(unescape(titolo.group(1)).split()) if titolo else ""
    redirect = _elenco_indirizzi(
        _RE_REDIRECT_JS.findall(testo) + _RE_REDIRECT_META.findall(testo))
    risorse = _elenco_indirizzi(_RE_RISORSE.findall(testo), 4)
    testo_log = (f", testo {_maschera(visibile[:massimo * 2])[:massimo]!r}"
                 if con_testo else "")
    return (f"titolo {_maschera(titolo)[:80]!r}{testo_log}, "
            f"redirect {redirect}, risorse {risorse}")


def _percorso(url) -> str:
    """Solo il percorso di un indirizzo, per i log: senza host, query o id di sessione."""
    return urlsplit(str(url)).path.split(";")[0] or "/"


class ClasseVivaWebApi:
    """Sessione autenticata sul portale web legacy (voti e agenda)."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._logged_in = False
        self._prev_host_attivo: str | None = None
        # (host, label) -> motivo, per le pagine che falliscono anche con login nuovo
        self._non_disponibili: dict[tuple[str, str], str] = {}
        self._motivo_fallimento: str | None = None
        self._fallimento_definitivo = False

    def _headers(self, **extra: str) -> dict[str, str]:
        h = {
            "User-Agent": WEB_USER_AGENT,
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        }
        h.update(extra)
        return h

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """Autentica sul portale PHP e ottiene il cookie PHPSESSID."""
        self._logged_in = False
        self._prev_host_attivo = None
        try:
            # Il portale si aspetta che la sessione esista gia': apri prima il login.
            async with self._session.get(
                f"https://{BASE_HOST}{LOGIN_PAGE}",
                headers=self._headers(Accept="text/html"),
            ) as resp:
                await resp.read()

            async with self._session.post(
                f"https://{BASE_HOST}{LOGIN_PATH}",
                data={"cid": "", "uid": self._username, "pwd": self._password,
                      "pin": "", "target": ""},
                headers=self._headers(
                    Accept="application/json, text/javascript, */*; q=0.01",
                    **{"X-Requested-With": "XMLHttpRequest",
                       "Origin": f"https://{BASE_HOST}",
                       "Referer": f"https://{BASE_HOST}{LOGIN_PAGE}"},
                ),
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise ClasseVivaWebError(f"Portale web non raggiungibile: {err}") from err
        except ValueError as err:
            raise ClasseVivaWebError(f"Risposta di login non valida: {err}") from err

        auth = (data or {}).get("data", {}).get("auth", {})
        if not auth.get("loggedIn"):
            errori = auth.get("errors") or (data or {}).get("error") or "credenziali rifiutate"
            raise ClasseVivaWebError(f"Login al portale web fallito: {errori}")
        self._logged_in = True
        _LOGGER.debug("Login portale web ok (account %s)",
                      auth.get("accountInfo", {}).get("type"))

    async def _abilita_anno_precedente(self, host: str) -> None:
        """Estende la sessione al server storico (giro SSO 'lap')."""
        if self._prev_host_attivo == host:
            return
        url = f"https://{BASE_HOST}{SSO_PREV_PATH.format(host=host)}"
        try:
            async with self._session.get(
                url,
                headers=self._headers(Accept="text/html",
                                      Referer=f"https://{BASE_HOST}{MENU_PATH}"),
            ) as resp:
                await resp.read()
                finale = str(resp.url)
        except aiohttp.ClientError as err:
            raise ClasseVivaWebError(f"SSO anno precedente fallito: {err}") from err
        if host not in finale:
            raise ClasseVivaWebError(
                f"SSO anno precedente non ha raggiunto {host} (finito su {finale})")
        self._prev_host_attivo = host

    def _fallita(self, motivo: str, url=None, *, definitiva: bool = False) -> None:
        """Annota perche' `azione()` sta per ritornare None (solo per il log).

        Dell'indirizzo si tiene il percorso: niente host, query, id o token.
        `definitiva` dice che la pagina risponde ma non e' piu' quella attesa:
        un nuovo login non serve.
        """
        self._motivo_fallimento = f"{motivo} ({_percorso(url)})" if url else motivo
        self._fallimento_definitivo = definitiva
        return None

    async def _con_sessione(self, host: str, storico: bool, azione,
                            label: str = "pagina", *, segna: bool = True):
        """Esegue `azione()` con una sessione valida su `host`.

        Se la richiesta viene rimbalzata al login (azione ritorna None) rifa'
        l'autenticazione una volta sola e riprova; poi si arrende.

        Con `segna` una pagina che fallisce anche dopo un login nuovo viene
        segnata come non disponibile (avviso una sola volta): da li' si prova
        una volta per giro, senza nuovi login, finche' non torna a funzionare.
        """
        chiave = (host, label)
        segnata = segna and chiave in self._non_disponibili
        definitiva = False
        for tentativo in range(1 if segnata else 2):
            try:
                if not self._logged_in:
                    await self.login()
                if storico:
                    await self._abilita_anno_precedente(host)
            except ClasseVivaWebError as err:
                _LOGGER.debug("Sessione non pronta su %s: %s", host, err)
                return None

            self._motivo_fallimento = None
            self._fallimento_definitivo = False
            risultato = await azione()
            if risultato is not None:
                if segnata:
                    del self._non_disponibili[chiave]
                    _LOGGER.info("Pagina %s di nuovo disponibile su %s", label, host)
                return risultato

            motivo = self._motivo_fallimento or "motivo non indicato"
            if segnata:
                # La sessione e' buona: e' la pagina a non esserci, niente re-login.
                _LOGGER.debug("Pagina %s ancora non disponibile su %s: %s",
                              label, host, motivo)
                return None
            _LOGGER.debug("Pagina %s su %s non riuscita (tentativo %d): %s",
                          label, host, tentativo + 1, motivo)
            if segna and self._fallimento_definitivo:
                # La pagina risponde ma non e' piu' quella attesa: niente nuovo login.
                definitiva = True
                self._non_disponibili[chiave] = motivo
                break
            if segna and tentativo == 1:
                # Fallita anche dopo un login nuovo: la sessione non c'entra.
                self._non_disponibili[chiave] = motivo
                break

            # Rimbalzati al login: le credenziali di sessione sono scadute.
            self._logged_in = False
            self._prev_host_attivo = None
        if definitiva:
            _LOGGER.warning("Pagina %s del portale web non disponibile su %s: %s. "
                            "Viene ritentata a ogni aggiornamento, senza altri avvisi "
                            "finche' non risponde di nuovo.", label, host, motivo)
        else:
            _LOGGER.warning("Sessione al portale web non utilizzabile su %s (%s): %s",
                            host, label, motivo)
        return None

    # ------------------------------------------------------------------ voti

    async def _scarica_voti(self, host: str) -> str | None:
        """Scarica la pagina dei voti; None se la sessione e' stata rifiutata."""
        try:
            async with self._session.get(
                f"https://{host}{VOTI_PATH}",
                headers=self._headers(Accept="text/html,application/xhtml+xml"),
            ) as resp:
                pagina = await resp.text()
                if "login.php" in str(resp.url):
                    return self._fallita("redirect al login", resp.url)
                return pagina
        except aiohttp.ClientError as err:
            _LOGGER.debug("Pagina voti non scaricata da %s: %s", host, err)
            return self._fallita(f"errore di rete ({type(err).__name__})")

    async def async_fetch_voti(self, piani: list[tuple[str, int | None, bool]]) -> dict:
        """Scarica i voti seguendo i piani server/anno decisi dall'API REST.

        Ogni piano e' (host, anno_di_inizio, e_anno_concluso): si prende il primo
        che restituisce almeno un voto.
        """
        risultato: dict = {"voti": [], "periodi": {}, "media": None,
                           "medie_materia": [], "medie_periodo": {},
                           "anno": None, "fonte": None}

        for host, anno, storico in piani:
            pagina = await self._con_sessione(
                host, storico, lambda: self._scarica_voti(host), "voti"
            )
            if pagina is None:
                continue

            dati = parse_voti(pagina, anno)
            if not dati["voti"]:
                _LOGGER.debug("Nessun voto su %s (anno %s)", host, anno)
                continue

            risultato.update(dati)
            risultato.update(calcola_medie(dati["voti"]))
            risultato["anno"] = f"{anno}/{anno + 1}" if anno else None
            risultato["fonte"] = host
            return risultato

        return risultato

    # ---------------------------------------------------------------- agenda

    async def _scarica_eventi(self, host: str, anno: int | None) -> list[dict] | None:
        """Interroga la stessa sorgente JSON che alimenta il calendario del sito."""
        url = f"https://{host}{AGENDA_PATH}"
        try:
            # La pagina serve solo per leggere classe_id/gruppo_id dal JS.
            async with self._session.get(
                url, headers=self._headers(Accept="text/html"),
            ) as resp:
                pagina = await resp.text()
                if "login.php" in str(resp.url):
                    return self._fallita("redirect al login", resp.url)

            def _var(nome: str) -> str:
                m = re.search(_RE_VAR_JS.format(nome=nome), pagina)
                return m.group(1) if m else ""

            inizio, fine = _intervallo_anno(anno)
            classe_id = _var("classe_id_page")
            gruppo_id = _var("gruppo_id_page")
            if (not classe_id or not gruppo_id) and "/_nuxt/" in pagina:
                # Nuova interfaccia del portale: la pagina non ha piu' questi valori
                # e la richiesta degli eventi non puo' funzionare. Sulle pagine del
                # vecchio portale (es. web25) la richiesta si fa come prima.
                risorse = _elenco_indirizzi(_RE_RISORSE.findall(pagina), 2)
                return self._fallita(
                    "la pagina agenda non contiene classe_id e gruppo_id "
                    f"({len(pagina)} caratteri, risorse {risorse}): il portale web "
                    "sembra usare una nuova interfaccia e l'agenda arriva solo "
                    "dall'API REST",
                    url, definitiva=True,
                )
            async with self._session.post(
                f"{url}?ope=get_events",
                data={
                    "classe_id": classe_id,
                    "gruppo_id": gruppo_id,
                    "nascondi_av": "0",
                    "start": inizio,
                    "end": fine,
                },
                headers=self._headers(
                    Accept="application/json, text/javascript, */*; q=0.01",
                    **{"X-Requested-With": "XMLHttpRequest", "Referer": url},
                ),
            ) as resp:
                stato = resp.status
                tipo = resp.headers.get("Content-Type", "")
                testo = await resp.text()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Agenda non scaricata da %s: %s", host, err)
            return self._fallita(f"errore di rete ({type(err).__name__})")

        try:
            eventi = json.loads(testo)
        except ValueError:
            _LOGGER.debug("Risposta agenda non JSON da %s: %s", host,
                          _riassunto_html(testo, con_testo=True))
            return self._fallita(
                "risposta non JSON "
                f"(stato {stato}, {tipo or 'tipo assente'}, {len(testo)} caratteri, "
                f"classe_id {'trovato' if classe_id else 'assente'}, "
                f"gruppo_id {'trovato' if gruppo_id else 'assente'}, "
                f"risposta: {_riassunto_html(testo)}; "
                f"pagina agenda: {len(pagina)} caratteri, {_riassunto_html(pagina)})",
                url,
            )
        return eventi if isinstance(eventi, list) else []

    # ------------------------------------------------------------------ note

    async def _scarica_pagina(self, host: str, path: str) -> str | None:
        """GET di una pagina del portale; None se rimbalza al login."""
        try:
            async with self._session.get(
                f"https://{host}{path}",
                headers=self._headers(Accept="text/html,application/xhtml+xml"),
            ) as resp:
                pagina = await resp.text()
                if "login.php" in str(resp.url):
                    return self._fallita("redirect al login", resp.url)
                return pagina
        except aiohttp.ClientError as err:
            _LOGGER.debug("Pagina %s non scaricata da %s: %s", path, host, err)
            return self._fallita(f"errore di rete ({type(err).__name__})")

    async def async_fetch_note(self, piani: list[tuple[str, int | None, bool]]) -> dict:
        """Note disciplinari e annotazioni dei docenti."""
        risultato: dict = {"note": [], "anno": None, "fonte": None}

        for host, anno, storico in piani:
            pagina = await self._con_sessione(
                host, storico, lambda: self._scarica_pagina(host, NOTE_PATH), "note"
            )
            if pagina is None:
                continue
            note = parse_note(pagina)
            if not note:
                _LOGGER.debug("Nessuna nota su %s (anno %s)", host, anno)
                continue
            risultato["note"] = note
            risultato["anno"] = f"{anno}/{anno + 1}" if anno else None
            risultato["fonte"] = host
            return risultato

        return risultato

    # --------------------------------------------------------- testi bacheca

    async def async_fetch_testi_bacheca(self) -> dict[str, str]:
        """Testo delle comunicazioni in bacheca, SENZA segnarle come lette.

        L'API REST espone il testo solo con `noticeboard/read/...`, che però
        registra la lettura sul registro. Il portale invece lo restituisce
        insieme all'elenco: segnare come letto è un'azione separata (`read_all`)
        che qui non viene mai chiamata. Vale per le comunicazioni ancora in
        corso di validità, non lette comprese.

        Ritorna {titolo normalizzato: testo}: gli id del portale non coincidono
        con i `pubId` del REST, il titolo è l'unico aggancio comune.
        """

        async def _chiedi():
            try:
                async with self._session.post(
                    f"https://{BASE_HOST}{BACHECA_PATH}",
                    data={"action": "get_comunicazioni", "cerca": "",
                          "ncna": "1", "tipo_com": ""},
                    headers=self._headers(
                        Accept="application/json, text/javascript, */*; q=0.01",
                        **{"X-Requested-With": "XMLHttpRequest",
                           "Referer": f"https://{BASE_HOST}{BACHECA_PATH}"},
                    ),
                ) as resp:
                    if resp.status != 200:
                        return self._fallita(f"stato HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as err:
                _LOGGER.debug("Testi bacheca non scaricati: %s", err)
                return self._fallita(f"errore ({type(err).__name__})")

        dati = await self._con_sessione(BASE_HOST, False, _chiedi, "bacheca")
        if not isinstance(dati, dict):
            return {}

        testi: dict[str, str] = {}
        for gruppo in dati.values():
            if not isinstance(gruppo, list):
                continue
            for com in gruppo:
                if not isinstance(com, dict):
                    continue
                titolo = chiave_titolo(com.get("titolo"))
                testo = _riga_singola(com.get("testo") or "")
                if titolo and testo:
                    testi[titolo] = testo
        return testi

    # -------------------------------------------------------------- scrutini

    async def async_fetch_scrutini(self, piani: list[tuple[str, int | None, bool]]) -> dict:
        """Documenti di scrutinio (pagelle ed esiti) pubblicati per lo studente.

        Legge solo l'elenco: NON conferma la presa visione, che sul portale è un
        passaggio a parte (`pubblicazioni.php?a=RA-LETTURA`) e resta una scelta
        di chi guarda, non un effetto dell'aggiornamento automatico.
        """
        risultato: dict = {"scrutini": [], "anno": None, "fonte": None}

        for host, anno, storico in piani:
            pagina = await self._con_sessione(
                host, storico, lambda: self._scarica_pagina(host, SCRUTINI_PATH), "scrutini"
            )
            if pagina is None:
                continue
            documenti = parse_scrutini(pagina)
            if not documenti:
                _LOGGER.debug("Nessun documento di scrutinio su %s (anno %s)", host, anno)
                continue
            for d in documenti:
                d["host"] = host
            risultato["scrutini"] = documenti
            risultato["anno"] = f"{anno}/{anno + 1}" if anno else None
            risultato["fonte"] = host
            return risultato

        return risultato

    async def async_scarica_scrutinio(self, documento: dict):
        """Scarica il PDF di un documento di scrutinio.

        Il portale lo genera su richiesta (`RA-RICAVA`) e lo serve da un URL che
        vale solo con il cookie di sessione: va quindi scaricato qui e
        ritrasmesso, non passato al browser come link.
        Ritorna (contenuto, content_type) oppure (None, None).
        """
        host = documento.get("host") or BASE_HOST
        storico = host != BASE_HOST

        # documento gia' pronto: si scarica direttamente dal suo indirizzo
        diretto = documento.get("url")
        if diretto:
            indirizzo = diretto if diretto.startswith("http") else (
                f"https://{host}" + ("" if diretto.startswith("/") else "/") + diretto
            )

            async def _prendi():
                try:
                    async with self._session.get(
                        indirizzo, headers=self._headers(Accept="text/html,application/pdf,*/*")
                    ) as resp:
                        if resp.status != 200:
                            return self._fallita(f"stato HTTP {resp.status}")
                        if "login.php" in str(resp.url):
                            return self._fallita("redirect al login", resp.url)
                        return (
                            await resp.read(),
                            resp.headers.get("Content-Type", "text/html"),
                        )
                except aiohttp.ClientError as err:
                    _LOGGER.debug("Documento %s non scaricato: %s", _percorso(indirizzo), err)
                    return self._fallita(f"errore di rete ({type(err).__name__})")

            # Richiesta su comando, non di ogni giro: niente marcatura "non disponibile".
            esito = await self._con_sessione(
                host, storico, _prendi, "documento", segna=False
            )
            return esito if esito else (None, None)

        async def _ricava():
            url = (
                f"https://{host}{PUBBLICAZIONI_PATH}?a=RA-RICAVA"
                f"&doctype={documento.get('doctype')}"
                f"&sessione={documento.get('sessione')}"
                f"&params={documento.get('params')}"
            )
            try:
                async with self._session.post(
                    url,
                    headers=self._headers(
                        Accept="application/json, text/javascript, */*; q=0.01",
                        **{"X-Requested-With": "XMLHttpRequest",
                           "Referer": f"https://{host}{SCRUTINI_PATH}"},
                    ),
                ) as resp:
                    if resp.status != 200:
                        return self._fallita(f"stato HTTP {resp.status}")
                    dati = await resp.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as err:
                _LOGGER.debug("Generazione documento fallita: %s", err)
                return self._fallita(f"errore ({type(err).__name__})")
            if not dati or not dati.get("visualizzabile"):
                _LOGGER.debug("Documento non visualizzabile: %s", (dati or {}).get("error"))
                return self._fallita("documento non visualizzabile")
            return dati.get("url_documento")

        indirizzo = await self._con_sessione(
            host, storico, _ricava, "documento", segna=False
        )
        if not indirizzo:
            return None, None
        if not indirizzo.startswith("http"):
            indirizzo = f"https://{host}" + ("" if indirizzo.startswith("/") else "/") + indirizzo

        try:
            async with self._session.get(
                indirizzo, headers=self._headers(Accept="application/pdf,*/*")
            ) as resp:
                if resp.status != 200:
                    return None, None
                return await resp.read(), resp.headers.get("Content-Type", "application/pdf")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Download documento fallito: %s", err)
            return None, None

    async def async_fetch_agenda(self, piani: list[tuple[str, int | None, bool]]) -> dict:
        """Scarica l'agenda (compiti, annotazioni, prenotazioni) con gli stessi piani."""
        risultato: dict = {"agenda": [], "anno": None, "fonte": None}

        for host, anno, storico in piani:
            eventi = await self._con_sessione(
                host, storico, lambda: self._scarica_eventi(host, anno), "agenda"
            )
            if not eventi:
                _LOGGER.debug("Nessun evento in agenda su %s (anno %s)", host, anno)
                continue

            normalizzati = parse_eventi(eventi)
            if not normalizzati:
                continue

            risultato["agenda"] = normalizzati
            risultato["anno"] = f"{anno}/{anno + 1}" if anno else None
            risultato["fonte"] = host
            return risultato

        return risultato


def _intervallo_anno(anno: int | None) -> tuple[int, int]:
    """Intervallo semichiuso: 1 settembre incluso, 1 settembre escluso."""
    if not anno:
        oggi = datetime.now()
        anno = oggi.year if oggi.month >= 9 else oggi.year - 1
    return (
        int(datetime(anno, 9, 1).timestamp()),
        int(datetime(anno + 1, 9, 1).timestamp()),
    )
