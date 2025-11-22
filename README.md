# Bot Telegram per Organizzare File su Google Drive

Questo progetto contiene un bot di Telegram scritto in Python che avvia una conversazione con l'utente per caricare file in percorsi specifici su Google Drive.

**Modello di Sicurezza**: questo bot è progettato per essere eseguito in un ambiente server (come Docker o Railway) where i segreti sono gestiti tramite **variabili d'ambiente**, garantendo che nessun file sensibile (come `credentials.json` o `token.json`) venga esposto nel repository o sul file system del server.

## Indice

- [Come Funziona](#come-funziona)
- [Configurazione delle Credenziali](#configurazione-delle-credenziali)
  - [Prerequisiti](#prerequisiti)
  - [Passo 1: Ottenere il Token del Bot Telegram](#passo-1-ottenere-il-token-del-bot-telegram)
  - [Passo 2: Configurare le API di Google Drive](#passo-2-configurare-le-api-di-google-drive)
  - [Passo 3: Ottenere l'ID della Cartella di Google Drive](#passo-3-ottenere-lid-della-cartella-di-google-drive)
- [Configurazione del Progetto (Variabili d'Ambiente)](#configurazione-del-progetto-variabili-dambiente)
- [Installazione ed Esecuzione](#installazione-ed-esecuzio-ne)
  - [Esecuzione Locale](#esecuzion-locale)
  - [Esecuzione con Docker](#esecuzion-con-docker)
- [Struttura del Progetto](#struttura-del-progetto)

## Come Funziona

Il bot funziona in modo interattivo per garantire che i file vengano archiviati esattamente dove desiderato:

1.  **Invio del File**: Invia un qualsiasi file (documento, immagine, PDF, etc.) al bot su Telegram.
2.  **Richiesta del Percorso**: Il bot ti chiederà in quale percorso desideri salvare il file. Puoi specificare un percorso nidificato (es. `Documenti/Fatture/2025`).
3.  **Verifica e Conferma**:
    - Il bot controlla se il percorso esiste già.
    - Se **esiste**, chiede conferma per caricare il file.
    - Se **non esiste**, chiede il permesso di creare le cartelle e poi caricare il file.
4.  **Azione Finale**:
    - Rispondendo `Sì`, il bot esegue l'azione e invia una notifica di successo.
    - Rispondendo `No`, l'operazione viene annullata.
5.  **Annullamento**: Puoi annullare in qualsiasi momento con `/cancel`.

## Configurazione delle Credenziali

Prima di eseguire il bot, devi raccogliere le seguenti credenziali.

### Prerequisiti

- Python 3.8 o superiore.
- Un account Telegram e uno Google.
- Docker (opzionale, per l'esecuzione in un container).

### Passo 1: Ottenere il Token del Bot Telegram

1.  Cerca `BotFather` su Telegram.
2.  Invia `/newbot` e segui le istruzioni.
3.  Conserva il **Token API** che ti viene fornito.

### Passo 2: Configurare le API di Google Drive

1.  Vai alla [Google Cloud Console](https://console.cloud.google.com/).
2.  Crea un nuovo progetto.
3.  Abilita l'**API di Google Drive** dalla Libreria delle API.
4.  Vai a **API e servizi > Schermata di consenso OAuth**:
    - Tipo utente: **Esterno**.
    - Fornisci nome app, email utente e email sviluppatore.
    - Salta le altre sezioni.
5.  Vai a **API e servizi > Credenziali**:
    - Clicca **+ CREA CREDENZIALI > ID client OAuth**.
    - Tipo di applicazione: **Applicazione desktop**.
    - Clicca su **SCARICA JSON**. Otterrai un file che rinominerai `credentials.json`. **Il contenuto di questo file non verrà salvato nel progetto, ma usato come variabile d'ambiente.**

### Passo 3: Ottenere l'ID della Cartella di Google Drive

1.  Crea una cartella su [Google Drive](https://drive.google.com/) che fungerà da radice per tutti i caricamenti.
2.  Apri la cartella e copia l'ID dall'URL. Esempio: `https://drive.google.com/drive/folders/ID_CARTELLA`.

## Configurazione del Progetto (Variabili d'Ambiente)

Il bot non usa più `config.ini`. La configurazione avviene tramite le seguenti variabili d'ambiente:

- `TELEGRAM_TOKEN`: Il token del tuo bot Telegram.
- `GOOGLE_DRIVE_PARENT_FOLDER_ID`: L'ID della cartella principale di Google Drive.
- `GOOGLE_CREDENTIALS_JSON`: Il **contenuto** del file `credentials.json` come stringa su una sola linea.
- `GOOGLE_TOKEN_JSON` (Opzionale): Il contenuto del file `token.json` generato dopo la prima autenticazione. Utile per evitare di ri-autenticarsi ad ogni avvio, specialmente in ambienti stateless.

#### Come formattare `GOOGLE_CREDENTIALS_JSON`

Il contenuto del file `credentials.json` deve essere convertito in una stringa su una sola riga. Puoi usare un tool online per "minify" JSON o eseguire un comando da terminale:
```bash
# Sostituisci 'credentials.json' con il percorso del tuo file
cat credentials.json | jq -c .
```
Il risultato sarà una stringa compatta da usare come valore per la variabile d'ambiente.

## Installazione ed Esecuzione

### Esecuzione Locale

1.  **Crea un ambiente virtuale:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # Su Windows: venv\Scripts\activate
    ```
2.  **Installa le dipendenze:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Imposta le variabili d'ambiente:**
    ```bash
    export TELEGRAM_TOKEN="IL_TUO_TOKEN"
    export GOOGLE_DRIVE_PARENT_FOLDER_ID="ID_CARTELLA_DRIVE"
    export GOOGLE_CREDENTIALS_JSON='{"installed":{"client_id":"...", ...}}'
    # GOOGLE_TOKEN_JSON è opzionale al primo avvio
    ```
4.  **Esegui il bot:**
    ```bash
    python3 bot.py
    ```
5.  **Prima Esecuzione (Autenticazione Google):**
    - Al primo avvio, il bot genererà un URL di autenticazione nel terminale.
    - Aprilo nel browser, accedi e autorizza l'app.
    - Verrai reindirizzato a una pagina `localhost`. Copia l'URL completo di questa pagina e incollalo nel terminale.
    - Verrà creato un file `token.json` e il suo contenuto verrà stampato a log. Per le esecuzioni future (specialmente su server), puoi impostare questo contenuto nella variabile `GOOGLE_TOKEN_JSON` per saltare l'autenticazione.

### Esecuzione con Docker

È il modo consigliato per eseguire il bot in produzione.

1.  **Crea un file `Dockerfile`** (se non esiste già nel progetto):
    ```Dockerfile
    FROM python:3.9-slim

    WORKDIR /app

    COPY requirements.txt .
    RUN pip install --no-cache-dir -r requirements.txt

    COPY . .

    CMD ["python3", "bot.py"]
    ```
2.  **Crea un file `.env`** per le variabili d'ambiente:
    ```env
    TELEGRAM_TOKEN=IL_TUO_TOKEN
    GOOGLE_DRIVE_PARENT_FOLDER_ID=ID_CARTELLA_DRIVE
    GOOGLE_CREDENTIALS_JSON='{"installed":{...}}'
    # Aggiungi GOOGLE_TOKEN_JSON dopo la prima esecuzione
    ```
3.  **Build e Run del container:**
    ```bash
    docker build -t telegram-drive-bot .
    docker run --env-file .env telegram-drive-bot
    ```
    Alla prima esecuzione con Docker, dovrai comunque completare il flusso OAuth nel terminale dove il container è in esecuzione.

## Struttura del Progetto

```
/
├── .gitignore
├── bot.py              # Logica principale del bot
├── README.md
├── requirements.txt
└── temp_downloads/     # Cartella temporanea per i file scaricati
#
# File sensibili (NON presenti nel repository):
# - credentials.json (il suo contenuto è in GOOGLE_CREDENTIALS_JSON)
# - token.json (generato localmente, il suo contenuto può essere in GOOGLE_TOKEN_JSON)
# - venv/ (ambiente virtuale)
```