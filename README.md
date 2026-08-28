# ClasseViva Spaggiari per Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
[![licenza][license-badge]](LICENSE)

Integrazione non ufficiale del registro elettronico **ClasseViva** (Gruppo Spaggiari):
voti, medie, agenda, bacheca, note e scrutini come entità di Home Assistant.

> Progetto indipendente, non affiliato né supportato da Gruppo Spaggiari.
> Accede solo ai dati del proprio account, come farebbe l'app ufficiale.

C'è anche una **card dedicata**: [classeviva-card][card-url].

## Funzioni

### Sensori

| Entità | Stato | Attributi principali |
|---|---|---|
| Media | media generale | `per_materia`, `per_periodo`, `anno` |
| Voti | numero di voti | `items` (data, materia, voto, tipo, periodo), `insufficienze` |
| Agenda | eventi in agenda | `items` (compiti e annotazioni dei docenti) |
| Bacheca | comunicazioni non lette | `items` con testo, `pub_id`, `attiva` e `download_url` degli allegati |
| Note | note e annotazioni | `items` con `categoria`, `richiamo`, `letta`; `da_leggere` |
| Scrutini | documenti pubblicati | `items` con `download_url` (pagelle, pagellini, recuperi) |
| File Docenti | file condivisi | `items` per docente, con `download_url` |

### Calendari

Uno per tipo di evento, così in dashboard si colorano separatamente — Home Assistant
assegna il colore per calendario, non per singolo evento:

- **Agenda** — riepilogo: compiti e annotazioni
- **Compiti** — solo i compiti assegnati
- **Note dei docenti** — annotazioni, avvisi, aule virtuali
- **Prenotazioni aula** — solo le prenotazioni d'aula

### Servizi

| Servizio | Cosa fa |
|---|---|
| `classeviva.aggiorna` | forza subito il download dei dati |
| `classeviva.carica_testi_bacheca` | scarica i testi mancanti **senza** segnarli come letti |
| `classeviva.leggi_comunicazione` | apre una comunicazione e ne restituisce il testo (**la segna come letta**) |

### Download allegati

La vista `/api/classeviva/download` consegna allegati della bacheca, file dei docenti
e documenti di scrutinio tramite URL firmate valide 12 ore, già pronte negli attributi
dei sensori. Aprire una pagella da qui **non** registra la presa visione.

## Installazione

### HACS (consigliato)

HACS → Integrazioni → menu ⋮ → *Repository personalizzati* → incolla l'URL di questo
repository, categoria **Integration** → installa → riavvia Home Assistant.

### Manuale

Copia `custom_components/classeviva/` in `<config>/custom_components/` e riavvia.

Poi: *Impostazioni → Dispositivi e servizi → Aggiungi integrazione → ClasseViva*.

## Configurazione

| Campo | Descrizione |
|---|---|
| Username | email o codice utente del registro |
| Password | password del registro |
| Seleziona Anno | `Ultimo` per l'anno più recente con dati, oppure un anno preciso |

L'anno accetta i formati `25/26`, `2025/2026` o `25`, sempre interpretato come anno di
**inizio**. Lo stesso account si può aggiungere più volte su anni diversi: ogni istanza
crea un dispositivo separato (`ClasseViva Nome 25/26`).

> `Ultimo` significa *l'anno più recente che contiene dati*. A settembre, finché la
> scuola non apre il nuovo anno, l'API risponde `school year not started yet` e
> l'integrazione mostra quello appena concluso.

## Esempi per la dashboard

Calendari con i colori del registro:

```yaml
type: calendar
initial_view: dayGridMonth
entities:
  - entity: calendar.classeviva_nome_compiti
    color: "#D7D765"
  - entity: calendar.classeviva_nome_note_dei_docenti
    color: "#A5D7FD"
  - entity: calendar.classeviva_nome_prenotazioni_aula
    color: "#55f74f"
```

Medie per materia:

```yaml
type: markdown
content: |
  ## Media {{ state_attr('sensor.classeviva_nome_media','anno') }}: {{ states('sensor.classeviva_nome_media') }}
  {% for m in state_attr('sensor.classeviva_nome_media','per_materia') %}
  - **{{ m.materia }}**: {{ m.media }}{% if m.insufficienze %} ⚠️ {{ m.insufficienze }}{% endif %}
  {%- endfor %}
```

Avviso quando esce un nuovo documento di scrutinio:

```yaml
automation:
  - alias: Nuovo scrutinio
    trigger:
      - platform: state
        entity_id: sensor.classeviva_nome_scrutini
    condition: "{{ trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}"
    action:
      - service: notify.persistent_notification
        data:
          title: Nuovo documento di scrutinio
          message: "{{ state_attr('sensor.classeviva_nome_scrutini','ultimo') }}"
```

Controlla i nomi esatti delle entità in *Strumenti per sviluppatori → Stati*.

## Come funziona

L'integrazione usa **due sorgenti**, perché nessuna delle due basta da sola.

**API REST mobile** (`web.spaggiari.eu/rest/v1`) — bacheca e file dei docenti.
Il login restituisce due token: `token` vale sul server dell'anno in corso, `tokenAP`
sui server degli anni conclusi (`web25.spaggiari.eu` per il 2025/26). Usare il token
sbagliato sul server sbagliato dà `409 incompatible domain`.

**Portale web PHP** — voti, agenda, note e scrutini. Non passano dal REST: `/grades`
risponde sempre vuoto (ed è il motivo per cui anche l'app ufficiale non mostra i voti
degli anni passati), e sul server storico gli endpoint a intervallo di date accettano
solo le date dell'anno nuovo. Il portale richiede un login separato e, per gli anni
conclusi, un giro di SSO interno (`xasapi.php?a=lap`).

Se il portale non risponde l'aggiornamento non fallisce: restano i dati REST.
Dettagli e struttura delle pagine sono commentati in `api.py` e `web.py`.

### Presa visione

Alcune azioni sul registro registrano la lettura, e l'integrazione **non** le compie da
sola:

- il testo di una comunicazione scaduta si ottiene solo dall'endpoint che la segna come
  letta → serve il servizio `leggi_comunicazione`, che è una scelta esplicita
- le comunicazioni ancora valide espongono il testo senza effetti collaterali
- le pagelle si scaricano senza confermare la presa visione, che sul portale è una
  chiamata distinta mai usata qui

## Note

- I dati si aggiornano ogni 30 minuti.
- La chiave API in `const.py` è quella dell'app Android ufficiale. Spaggiari la cambia
  periodicamente: se il login inizia a dare errore `apikey.3` va ri-catturata da una
  versione recente dell'app.
- Troppi tentativi di login sbagliati bloccano l'account per circa 60 minuti.

## Crediti

Sviluppato da [@Davitekk](https://github.com/Davitekk) con l'assistenza di
**Claude** (Anthropic), che ha collaborato al reverse engineering del registro
e alla scrittura del codice.

## Licenza

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/Davitekk/classeviva-ha
[release-url]: https://github.com/Davitekk/classeviva-ha/releases
[license-badge]: https://img.shields.io/github/license/Davitekk/classeviva-ha
[card-url]: https://github.com/Davitekk/classeviva-card
