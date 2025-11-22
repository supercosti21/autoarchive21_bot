import logging
import os
import mimetypes
import json
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration Loading from Environment Variables ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GOOGLE_DRIVE_PARENT_FOLDER_ID = os.getenv('GOOGLE_DRIVE_PARENT_FOLDER_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
# GOOGLE_TOKEN_JSON is used to store and reuse OAuth tokens.
# In a stateless environment, you might need a persistent store. For Railway, this can be a volume.
GOOGLE_TOKEN_JSON = os.getenv('GOOGLE_TOKEN_JSON')

# Define conversation states
GET_PATH, CONFIRM_UPLOAD = range(2)

# --- Google Drive Setup ---
SCOPES = ['https://www.googleapis.com/auth/drive.file']
DRIVE_SERVICE = None

def get_drive_service():
    global DRIVE_SERVICE
    if DRIVE_SERVICE:
        return DRIVE_SERVICE

    creds = None
    token_path = 'token.json'  # Still used to save the token locally if needed

    # Prioritize loading token from environment variable
    if GOOGLE_TOKEN_JSON:
        try:
            creds_data = json.loads(GOOGLE_TOKEN_JSON)
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            logger.info("Credenziali caricate dalla variabile d'ambiente GOOGLE_TOKEN_JSON.")
        except json.JSONDecodeError:
            logger.error("Formato JSON non valido in GOOGLE_TOKEN_JSON.")
            creds = None
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        logger.info(f"Credenziali caricate dal file '{token_path}'.")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Credenziali scadute. Refresh in corso...")
            creds.refresh(Request())
        else:
            logger.info("Nessuna credenziale valida trovata. Avvio del flusso OAuth.")
            if not GOOGLE_CREDENTIALS_JSON:
                logger.error("La variabile d'ambiente GOOGLE_CREDENTIALS_JSON non è impostata.")
                raise ValueError("GOOGLE_CREDENTIALS_JSON non trovato.")
            
            try:
                creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = InstalledAppFlow.from_client_config(creds_info, SCOPES)
                creds = flow.run_local_server(port=0)
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Errore nel parsing di GOOGLE_CREDENTIALS_JSON: {e}")
                raise ValueError("GOOGLE_CREDENTIALS_JSON non è un JSON valido.")

        # Save the credentials for the next run (locally and log the JSON for env var update)
        token_json_content = creds.to_json()
        with open(token_path, 'w') as token:
            token.write(token_json_content)
        
        # Log the token content so it can be copied into the GOOGLE_TOKEN_JSON env var
        logger.info(
            "Nuovo token generato. Per la persistenza in ambienti stateless, "
            "imposta la variabile d'ambiente GOOGLE_TOKEN_JSON con questo contenuto:"
        )
        logger.info(token_json_content)

    DRIVE_SERVICE = build('drive', 'v3', credentials=creds)
    logger.info("Servizio Google Drive inizializzato con successo.")
    return DRIVE_SERVICE

def find_or_create_nested_folder(service, path_string: str, root_folder_id: str):
    """
    Finds a nested folder path, creating any missing folders along the way.
    Returns the ID of the final folder in the path, or None if an error occurs.
    """
    current_folder_id = root_folder_id
    path_parts = [part.strip() for part in path_string.split('/') if part.strip()]

    for part in path_parts:
        try:
            query = f"name='{part}' and mimeType='application/vnd.google-apps.folder' and '{current_folder_id}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id)").execute()
            items = results.get('files', [])

            if items:
                current_folder_id = items[0]['id']
                logger.info(f"Trovata cartella '{part}' con ID: {current_folder_id}")
            else:
                logger.info(f"Cartella '{part}' non trovata. Creazione in corso...")
                file_metadata = {
                    'name': part,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [current_folder_id]
                }
                folder = service.files().create(body=file_metadata, fields='id').execute()
                current_folder_id = folder.get('id')
                logger.info(f"Creata cartella '{part}' con ID: {current_folder_id}")
        except HttpError as error:
            logger.error(f"Errore durante la ricerca o creazione della cartella '{part}': {error}")
            return None
            
    return current_folder_id

def upload_file_to_drive(service, file_path, folder_id):
    try:
        file_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'

        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        logger.info(f"File '{file_name}' caricato con ID: {file.get('id')}")
        return file.get('id')
    except HttpError as error:
        logger.error(f"Errore durante il caricamento del file '{file_path}': {error}")
        return None

# --- Telegram Conversation Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Ciao! Inviami un documento e ti chiederò dove salvarlo su Google Drive.')

async def start_document_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation when a document is received."""
    document = update.message.document
    user = update.message.from_user
    
    logger.info(f"Documento ricevuto da {user.first_name}: {document.file_name}")

    # Store file info in context for later use
    context.user_data['document_to_upload'] = {
        'file_id': document.file_id,
        'file_name': document.file_name,
    }

    await update.message.reply_text(
        f"Ho ricevuto il file '{document.file_name}'.\n\n"
        f"In quale percorso vuoi salvarlo? (es. `Fatture/2025/Amazon`)\n\n"
        f"Puoi annullare in qualsiasi momento con /cancel.",
        parse_mode='Markdown'
    )
    return GET_PATH

async def get_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user-provided path."""
    path_input = update.message.text
    context.user_data['upload_path'] = path_input
    
    reply_keyboard = [['Sì', 'No']]

    try:
        drive_service = get_drive_service()
        # Check if folder exists without creating it yet
        folder_id_check = GOOGLE_DRIVE_PARENT_FOLDER_ID
        path_exists = True
        path_parts = [part.strip() for part in path_input.split('/') if part.strip()]
        
        for part in path_parts:
            query = f"name='{part}' and mimeType='application/vnd.google-apps.folder' and '{folder_id_check}' in parents and trashed=false"
            results = drive_service.files().list(q=query, fields="files(id)").execute()
            items = results.get('files', [])
            if items:
                folder_id_check = items[0]['id']
            else:
                path_exists = False
                break
        
        if path_exists:
            context.user_data['final_folder_id'] = folder_id_check
            context.user_data['needs_creation'] = False
            await update.message.reply_text(
                f"La cartella `{path_input}` esiste già.\nVuoi caricare il file qui?",
                reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
                parse_mode='Markdown'
            )
        else:
            context.user_data['needs_creation'] = True
            await update.message.reply_text(
                f"La cartella `{path_input}` non esiste.\nVuoi che la crei e carichi il file?",
                reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
                parse_mode='Markdown'
            )
            
        return CONFIRM_UPLOAD
        
    except Exception as e:
        logger.error(f"Errore in get_path: {e}")
        await update.message.reply_text(f"Si è verificato un errore: {e}")
        return ConversationHandler.END

async def confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Yes'/'No' confirmation."""
    user_reply = update.message.text.lower()
    
    if user_reply not in ['sì', 'si', 'no']:
        await update.message.reply_text("Per favore, rispondi 'Sì' o 'No'.")
        return CONFIRM_UPLOAD
        
    if user_reply == 'no':
        await update.message.reply_text("Operazione annullata.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    # User confirmed with 'sì' or 'si'
    doc_info = context.user_data.get('document_to_upload')
    path_info = context.user_data.get('upload_path')
    needs_creation = context.user_data.get('needs_creation', False)
    final_folder_id = context.user_data.get('final_folder_id')

    await update.message.reply_text("Ok, procedo...", reply_markup=ReplyKeyboardRemove())

    # Define script_dir here as it's no longer global
    script_dir = os.path.dirname(os.path.abspath(__file__))
    download_dir = Path(os.path.join(script_dir, "temp_downloads"))
    download_dir.mkdir(exist_ok=True)
    file_path = download_dir / doc_info['file_name']

    try:
        # Download
        new_file = await context.bot.get_file(doc_info['file_id'])
        await new_file.download_to_drive(file_path)
        logger.info(f"File '{doc_info['file_name']}' scaricato in {file_path}")

        drive_service = get_drive_service()
        
        # Get or Create Folder
        if needs_creation:
            final_folder_id = find_or_create_nested_folder(drive_service, path_info, GOOGLE_DRIVE_PARENT_FOLDER_ID)
        
        if not final_folder_id:
             await update.message.reply_text("Errore critico: non sono riuscito a creare o trovare la cartella di destinazione.")
             return ConversationHandler.END

        # Upload
        uploaded_file_id = upload_file_to_drive(drive_service, file_path, final_folder_id)

        if uploaded_file_id:
            await update.message.reply_text(f"Fatto! File caricato con successo in `{path_info}`.")
        else:
            await update.message.reply_text("Si è verificato un errore durante il caricamento del file su Google Drive.")

    except Exception as e:
        logger.error(f"Errore in confirm_upload: {e}")
        await update.message.reply_text(f"Si è verificato un errore imprevisto: {e}")
    finally:
        # Cleanup
        if os.path.exists(file_path):
            os.remove(file_path)
        context.user_data.clear()

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        'Operazione annullata.', reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Optionally, send a generic message to the user
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Si è verificato un errore inaspettato. Il problema è stato registrato.")


def main():
    """Start the bot."""
    if not TELEGRAM_TOKEN or not GOOGLE_DRIVE_PARENT_FOLDER_ID:
        logger.error("Token Telegram o ID cartella Drive mancanti in config.ini.")
        exit(1)

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, start_document_flow)],
        states={
            GET_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_path)],
            CONFIRM_UPLOAD: [MessageHandler(filters.Regex('^(Sì|sì|si|No|no)$'), confirm_upload)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Register handlers
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    try:
        get_drive_service()
    except Exception as e:
        print(f"ERRORE FATALE durante l'inizializzazione: {e}")
        exit(1)
        
    logger.info("Avvio del Bot Telegram...")
    main()