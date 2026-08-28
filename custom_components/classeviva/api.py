"""Client API per ClasseViva Spaggiari.

Scoperte chiave dal reverse engineering (mitmproxy sull'app ufficiale, ago 2026):

* Il login restituisce DUE token:
    - "token"    -> valido solo sul server dell'anno corrente (web.spaggiari.eu)
    - "tokenAP"  -> valido sui server degli ANNI PRECEDENTI (web25.spaggiari.eu, ...)

* Gli anni passati NON si leggono aggiungendo un suffisso all'URL
  (es. /grades2526): quello resta sul server dell'anno corrente e dà 422/404.
  Si leggono cambiando SOTTODOMINIO: web{YY}.spaggiari.eu, dove YY sono le
  ultime due cifre dell'anno di inizio scolastico (2025 -> web25), usando tokenAP.

* Usare il token "sbagliato" sul dominio "sbagliato" dà 409 "incompatible domain".

* A fine estate il server corrente risponde 422 "school year not started yet":
  in quel caso si ripiega automaticamente sull'anno concluso.

Nessun TLS-fingerprint particolare: aiohttp/requests standard funzionano.
"""
from __future__ import annotations

import logging
import re

import aiohttp

from .const import ANNO_ULTIMO, API_KEY, USER_AGENT, BASE_HOST

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "/rest/v1/auth/login"

# Quante comunicazioni recenti tenere "aperte" (testo scaricato) e quanti testi
# nuovi scaricare per ciclo, per non fare raffiche di richieste a ogni update.
TESTI_COMUNICAZIONI = 30
TESTI_PER_CICLO = 25

# Scritture che valgono "segui l'anno più recente disponibile", confrontate in
# minuscolo: ANNO_ULTIMO è incluso comunque sia scritto in const.py.
SENZA_ANNO = {"", "ultimo", "auto", "last", "recente", str(ANNO_ULTIMO).strip().lower()}


class ClasseVivaAuthError(Exception):
    """Credenziali errate o account bloccato."""


class ClasseVivaApiError(Exception):
    """Errore generico di comunicazione."""


class ClasseVivaApi:
    """Gestisce login, scelta del server/anno e download dei dati."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        anno: int | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        # Anno di INIZIO scolastico richiesto (2025 = a.s. 2025/26); None = il piu' recente.
        self.anno = anno

        # Testi delle comunicazioni gia' scaricati, per pubId: leggerli costa una
        # richiesta ciascuno e non cambiano piu' una volta pubblicati.
        self._testi: dict[str, str] = {}

        self.token: str | None = None
        self.token_ap: str | None = None
        self.student_id: str | None = None
        self.first_name: str = "Studente"
        self.newest_yy: int | None = None
        self.prev_host: str | None = None

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {
            "User-Agent": USER_AGENT,
            "Z-Dev-Apikey": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            h["Z-Auth-Token"] = token
        return h

    async def login(self) -> None:
        """Autentica e riempie token, tokenAP, student_id, anno più recente."""
        payload = {
            "uid": self._username,
            "pass": self._password,
            "ident": None,
            "otp": None,
        }
        try:
            async with self._session.post(
                f"https://{BASE_HOST}{LOGIN_URL}",
                headers=self._headers(),
                json=payload,
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise ClasseVivaApiError(f"Connessione fallita: {err}") from err

        if status == 422:
            # Password scaduta / va cambiata dal portale
            raise ClasseVivaAuthError(data.get("message", "Login non processabile (422)."))
        if status == 420:
            raise ClasseVivaAuthError("Account bloccato temporaneamente (troppi tentativi). Attendi ~15 min.")
        if status in (401, 403):
            raise ClasseVivaAuthError("Credenziali errate.")
        if status != 200 or not data.get("token"):
            raise ClasseVivaApiError(f"Login fallito ({status}): {data}")

        self.token = data["token"]
        self.token_ap = data.get("tokenAP")
        self.first_name = (data.get("firstName") or "Studente").capitalize()

        m = re.search(r"\d+", data.get("ident", ""))
        if not m:
            raise ClasseVivaApiError("Impossibile estrarre lo student_id dall'ident.")
        self.student_id = m.group()

        await self._resolve_years()

    async def _resolve_years(self) -> None:
        """Legge da myportfolio l'anno scolastico più recente e calcola il server storico."""
        status, data = await self._get(BASE_HOST, "/rest/v1/xauth/myportfolio", self.token)
        yy = None
        if status == 200 and isinstance(data, dict):
            for item in data.get("portfolio", {}).get("items", []):
                info = item.get("personalInfo", {})
                if info.get("schoolYY"):
                    yy = int(info["schoolYY"])
                    break
        self.newest_yy = yy
        if yy:
            # 2026 -> web25 (anno concluso 2025/2026). Sottodominio = (anno inizio - 1) % 100.
            self.prev_host = f"web{(yy - 1) % 100:02d}.spaggiari.eu"
        _LOGGER.debug("Anno più recente=%s, server storico=%s", yy, self.prev_host)

    @staticmethod
    def host_anno(anno: int, newest: int | None) -> str:
        """Server che ospita un dato anno scolastico.

        L'anno in corso sta su web.spaggiari.eu, quelli conclusi su web{YY}
        (2025/26 -> web25.spaggiari.eu).
        """
        if newest is None or anno >= newest:
            return BASE_HOST
        return f"web{anno % 100:02d}.spaggiari.eu"

    def piani_anno(self) -> list[tuple[str, int | None, bool]]:
        """Server/anno da interrogare, in ordine di preferenza.

        Ogni elemento e' (host, anno_di_inizio, e_anno_concluso). Se l'utente ha
        scelto un anno preciso c'e' un solo piano; altrimenti si prova l'anno in
        corso e, se non e' ancora partito, si ripiega su quello appena concluso.
        """
        if self.anno is not None:
            host = self.host_anno(self.anno, self.newest_yy)
            return [(host, self.anno, host != BASE_HOST)]

        piani: list[tuple[str, int | None, bool]] = [(BASE_HOST, self.newest_yy, False)]
        if self.prev_host:
            piani.append((
                self.prev_host,
                self.newest_yy - 1 if self.newest_yy else None,
                True,
            ))
        return piani

    def _token_per(self, host: str) -> str | None:
        """Sul server dell'anno in corso vale `token`, sugli storici `tokenAP`."""
        return self.token if host == BASE_HOST else self.token_ap

    async def _get(self, host: str, path: str, token: str | None):
        """GET che ritorna (status, json|text). Non solleva su status != 200."""
        url = f"https://{host}{path}"
        try:
            async with self._session.get(url, headers=self._headers(token)) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 - a volte tornano testo/redirect
                    body = await resp.text()
                return status, body
        except aiohttp.ClientError as err:
            _LOGGER.debug("Errore rete su %s: %s", url, err)
            return None, str(err)

    async def _get_year_bound(self, resource: str):
        """Scarica una risorsa legata all'anno seguendo i piani di `piani_anno`.

        Ritorna (body, host_usato, anno_inizio) oppure (None, None, None).
        """
        base = f"/rest/v1/students/{self.student_id}"

        for host, anno, _storico in self.piani_anno():
            token = self._token_per(host)
            if not token:
                continue
            status, body = await self._get(host, f"{base}/{resource}", token)
            if status == 200:
                return body, host, anno

            if status in (401, 403):
                # Token scaduto: azzeralo e chiedi un nuovo login al ciclo dopo.
                if host == BASE_HOST:
                    self.token = None
                else:
                    self.token_ap = None
                raise ClasseVivaAuthError(f"Token scaduto su {host}.")

            # L'anno in corso non è ancora partito: si passa al piano successivo.
            _LOGGER.debug("%s su %s: %s", resource, host, status)

        return None, None, None

    async def async_fetch(self) -> dict:
        """Scarica tutti i dati utili, con degrado morbido se qualcosa non c'è."""
        if not self.token:
            await self.login()

        result: dict = {
            "first_name": self.first_name,
            "student_id": self.student_id,
        }

        # --- Voti (+ media) --- risorsa legata all'anno ---------------------
        grades_body, gr_host, gr_year = await self._get_year_bound("grades")
        grades = (grades_body or {}).get("grades", []) if isinstance(grades_body, dict) else []
        result["grades"] = grades
        result["grades_year"] = gr_year
        result["media"] = _average(grades)

        # --- File / materiale dei docenti (didattica) ----------------------
        did_body, _, _ = await self._get_year_bound("didactics")
        # nel JSON la chiave è scritta "didacticts" (refuso lato Spaggiari)
        if isinstance(did_body, dict):
            result["didactics"] = did_body.get("didacticts") or did_body.get("didactics") or []
        else:
            result["didactics"] = []

        # --- Note disciplinari ---------------------------------------------
        notes_body, _, _ = await self._get_year_bound("notes/all")
        result["notes"] = _flatten_notes(notes_body)

        # --- Bacheca (globale: sta sempre sul server corrente) -------------
        nb_status, nb_body = await self._get(
            BASE_HOST, f"/rest/v1/students/{self.student_id}/noticeboard", self.token
        )
        result["noticeboard"] = nb_body.get("items", []) if (nb_status == 200 and isinstance(nb_body, dict)) else []
        await self._aggiungi_testi(result["noticeboard"])

        # --- Agenda: gli endpoint a intervallo di date accettano solo le date
        # dell'anno attivo sul server interrogato, quindi vale solo per l'anno in
        # corso. Per gli anni conclusi ci pensa il portale web (vedi web.py).
        result["agenda"] = []
        host, anno, _storico = self.piani_anno()[0]
        if anno:
            ag_status, ag_body = await self._get(
                host,
                f"/rest/v1/students/{self.student_id}/agenda/all/{anno}0901/{anno + 1}0831",
                self._token_per(host),
            )
            if ag_status == 200 and isinstance(ag_body, dict):
                result["agenda"] = ag_body.get("agenda", [])

        return result


    async def leggi_comunicazione(self, evt: str, pub: str | int) -> str | None:
        """Testo di una comunicazione della bacheca.

        ATTENZIONE: l'endpoint che restituisce il testo la marca anche come LETTA
        sul registro, esattamente come aprirla nell'app. Va quindi chiamato solo
        su comunicazioni gia' lette, oppure su richiesta esplicita dell'utente.
        """
        if not self.token:
            await self.login()

        chiave = str(pub)
        if chiave in self._testi:
            return self._testi[chiave]

        path = f"/rest/v1/students/{self.student_id}/noticeboard/read/{evt}/{pub}/101"
        for host, _anno, _storico in self.piani_anno():
            token = self._token_per(host)
            if not token:
                continue
            try:
                async with self._session.post(
                    f"https://{host}{path}", headers=self._headers(token)
                ) as resp:
                    if resp.status != 200:
                        continue
                    body = await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                _LOGGER.debug("Lettura comunicazione %s fallita su %s: %s", pub, host, err)
                continue
            except ValueError:
                continue

            testo = (body.get("item") or {}).get("text")
            if testo is not None:
                self._testi[chiave] = testo
                return testo
        return None

    async def _aggiungi_testi(self, comunicazioni: list) -> None:
        """Allega il testo alle comunicazioni della bacheca gia' lette.

        Le NON lette vengono saltate di proposito: scaricarne il testo le
        marcherebbe come lette, e quella dev'essere una scelta dell'utente
        (servizio `classeviva.leggi_comunicazione`), non un effetto collaterale
        dell'aggiornamento automatico.
        """
        scaricati = 0
        for c in comunicazioni[:TESTI_COMUNICAZIONI]:
            chiave = str(c.get("pubId"))
            if chiave in self._testi:
                c["text"] = self._testi[chiave]
                continue
            if not c.get("readStatus") or scaricati >= TESTI_PER_CICLO:
                continue
            testo = await self.leggi_comunicazione(c.get("evtCode"), c.get("pubId"))
            if testo is not None:
                c["text"] = testo
                scaricati += 1

    async def get_download_url(
        self,
        kind: str,
        *,
        evt: str | None = None,
        pub: str | None = None,
        num: str | int | None = None,
        content_id: str | int | None = None,
    ) -> str | None:
        """Ritorna l'URL diretto (cdndoc) di un allegato, seguendo il redirect 302.

        - kind="noticeboard": serve evt (evtCode), pub (pubId), num (attachNum)
        - kind="didactics":   serve content_id (contentId)

        L'endpoint REST risponde con un 302 verso cdndoc.spaggiari.eu con un token
        AUTH in query string: quell'URL è scaricabile direttamente dal browser.
        """
        if not self.token:
            await self.login()
        sid = self.student_id

        if kind == "noticeboard":
            path = f"/rest/v1/students/{sid}/noticeboard/attach/{evt}/{pub}/{num}"
        elif kind == "didactics":
            path = f"/rest/v1/students/{sid}/didactics/item/{content_id}"
        else:
            return None

        # Stessi server/token dei dati: se l'utente ha scelto un anno preciso,
        # l'allegato va cercato sul server di quell'anno. Il server corrente resta
        # pero' sempre in coda: la bacheca e' globale e vive li', quindi con un anno
        # fissato i suoi allegati si troverebbero altrimenti solo sul server storico.
        hosts = [host for host, _a, _s in self.piani_anno()]
        if BASE_HOST not in hosts:
            hosts.append(BASE_HOST)
        candidates = [(host, self._token_per(host)) for host in hosts]

        for host, tok in candidates:
            if not tok:
                continue
            try:
                async with self._session.get(
                    f"https://{host}{path}",
                    headers=self._headers(tok),
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if loc:
                            return loc
            except aiohttp.ClientError as err:
                _LOGGER.debug("Download %s fallito su %s: %s", kind, host, err)
        return None


def parse_anno(valore) -> int | None:
    """Interpreta l'anno scolastico scritto dall'utente.

    Accetta "25/26", "2025/2026", "25-26", "2025", "25" e restituisce sempre
    l'anno di INIZIO (2025). "ultimo", vuoto o None significano "il piu' recente
    disponibile" e restituiscono None.

    Il confronto ignora maiuscole e spazi, cosi' ANNO_ULTIMO puo' essere scritto
    come si preferisce ("ultimo", "Ultimo", ...) senza rompere il riconoscimento.

    Solleva ValueError se la stringa non contiene un anno riconoscibile.
    """
    if valore is None:
        return None
    if isinstance(valore, int):
        return valore if valore >= 100 else 2000 + valore

    testo = str(valore).strip().lower()
    if testo in SENZA_ANNO:
        return None

    numeri = re.findall(r"\d+", testo)
    if not numeri:
        raise ValueError(f"Anno scolastico non riconosciuto: {valore!r}")

    primo = int(numeri[0])
    if len(numeri[0]) <= 2:
        primo += 2000
    elif not 2000 <= primo <= 2099:
        raise ValueError(f"Anno scolastico fuori intervallo: {valore!r}")
    return primo


def _average(grades: list) -> float | None:
    vals = []
    for g in grades or []:
        dv = g.get("decimalValue")
        if dv is not None:
            try:
                vals.append(float(dv))
            except (TypeError, ValueError):
                pass
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _flatten_notes(body) -> list:
    if not isinstance(body, dict):
        return []
    out = []
    for key, arr in body.items():
        if isinstance(arr, list):
            for n in arr:
                if isinstance(n, dict):
                    n = {**n, "_type": key}
                out.append(n)
    return out
