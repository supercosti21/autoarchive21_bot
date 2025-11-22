import logging
import os
import mimetypes
import json
from pathlib import Path
import magic

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler,
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
GOOGLE_TOKEN_JSON = os.getenv('GOOGLE_TOKEN_JSON')
AUTHORIZED_USER_ID = os.getenv('TELEGRAM_ID')

# Define conversation states
GET_PATH, CONFIRM_UPLOAD, SELECT_FOLDER, WAITING_FOR_MORE_FILES, CONFIRM_DELETE, LIST_FILES, SEARCH_FILES = range(7)

# --- Google Drive Setup ---
SCOPES = ['https://www.googleapis.com/auth/drive']
DRIVE_SERVICE = None

def get_drive_service():
    global DRIVE_SERVICE
    if DRIVE_SERVICE:
        return DRIVE_SERVICE

    creds = None
    token_path = 'token.json'

    if GOOGLE_TOKEN_JSON:
        try:
            creds_data = json.loads(GOOGLE_TOKEN_JSON)
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            logger.info("Credentials loaded from GOOGLE_TOKEN_JSON env var.")
        except json.JSONDecodeError:
            logger.error("Invalid JSON in GOOGLE_TOKEN_JSON.")
            creds = None
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        logger.info(f"Credentials loaded from '{token_path}'.")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Credentials expired. Refreshing...")
            creds.refresh(Request())
        else:
            logger.info("No valid credentials found. Starting OAuth flow.")
            if not GOOGLE_CREDENTIALS_JSON:
                logger.error("GOOGLE_CREDENTIALS_JSON env var not set.")
                raise ValueError("GOOGLE_CREDENTIALS_JSON not found.")
            
            try:
                creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = InstalledAppFlow.from_client_config(creds_info, SCOPES)
                creds = flow.run_local_server(port=0)
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Error parsing GOOGLE_CREDENTIALS_JSON: {e}")
                raise ValueError("GOOGLE_CREDENTIALS_JSON is not valid JSON.")

        token_json_content = creds.to_json()
        with open(token_path, 'w') as token:
            token.write(token_json_content)
        logger.info(f"Token saved to {token_path}. For persistence, set GOOGLE_TOKEN_JSON env var.")
        logger.info(token_json_content)

    DRIVE_SERVICE = build('drive', 'v3', credentials=creds)
    logger.info("Google Drive service initialized successfully.")
    return DRIVE_SERVICE

def find_or_create_nested_folder(service, path_string: str, root_folder_id: str):
    current_folder_id = root_folder_id
    path_parts = [part.strip() for part in path_string.split('/') if part.strip()]

    for part in path_parts:
        try:
            query = f"name='{part}' and mimeType='application/vnd.google-apps.folder' and '{current_folder_id}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id)").execute()
            items = results.get('files', [])

            if items:
                current_folder_id = items[0]['id']
                logger.info(f"Found folder '{part}' with ID: {current_folder_id}")
            else:
                logger.info(f"Folder '{part}' not found. Creating...")
                file_metadata = {'name': part, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [current_folder_id]}
                folder = service.files().create(body=file_metadata, fields='id').execute()
                current_folder_id = folder.get('id')
                logger.info(f"Created folder '{part}' with ID: {current_folder_id}")
        except HttpError as error:
            logger.error(f"Error finding or creating folder '{part}': {error}")
            return None
    return current_folder_id

def upload_file_to_drive(service, file_path, folder_id):
    try:
        file_name = os.path.basename(file_path)
        mime_type = magic.from_file(file_path, mime=True) or mimetypes.guess_type(file_path)[0] or 'application/octet-stream'

        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id,webViewLink').execute()
        logger.info(f"File '{file_name}' uploaded with ID: {file.get('id')}")
        return file.get('id'), file.get('webViewLink')
    except HttpError as error:
        logger.error(f"Error uploading file '{file_path}': {error}")
        return None, None

def get_folder_path_string(service, folder_id):
    """Helper to get a readable path for a folder ID."""
    if folder_id == GOOGLE_DRIVE_PARENT_FOLDER_ID:
        return "/"
    try:
        path = []
        file = service.files().get(fileId=folder_id, fields='name, parents').execute()
        while 'parents' in file:
            path.insert(0, file['name'])
            parent_id = file['parents'][0]
            if parent_id == GOOGLE_DRIVE_PARENT_FOLDER_ID:
                break
            file = service.files().get(fileId=parent_id, fields='name, parents').execute()
        return "/" + "/".join(path)
    except Exception as e:
        logger.error(f"Could not retrieve path for folder {folder_id}: {e}")
        return f"(unknown path for ID: {folder_id})"

def list_files_in_folder(service, folder_id, page_size=10):
    """List files in a specific folder."""
    try:
        query = f"'{folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            pageSize=page_size,
            orderBy="name",
            fields="files(id, name, mimeType, size, createdTime, webViewLink)"
        ).execute()
        return results.get('files', [])
    except HttpError as error:
        logger.error(f"Error listing files in folder {folder_id}: {error}")
        return []

def delete_file_from_drive(service, file_id):
    """Delete a file from Google Drive."""
    try:
        service.files().delete(fileId=file_id).execute()
        logger.info(f"File with ID {file_id} deleted successfully.")
        return True
    except HttpError as error:
        logger.error(f"Error deleting file {file_id}: {error}")
        return False

def search_files_by_name(service, file_name, folder_id=None):
    """Search files by name in Drive."""
    try:
        query = f"name contains '{file_name}' and trashed=false"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        
        results = service.files().list(
            q=query,
            pageSize=20,
            orderBy="name",
            fields="files(id, name, mimeType, webViewLink)"
        ).execute()
        return results.get('files', [])
    except HttpError as error:
        logger.error(f"Error searching files: {error}")
        return []

# --- 🔒 ACCESS CONTROL FUNCTION ---
async def check_access(update: Update) -> bool:
    """Verifica che l'utente sia autorizzato ad usare il bot."""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or update.effective_user.first_name
    
    if user_id != AUTHORIZED_USER_ID:
        logger.warning(f"🚫 Accesso negato a user_id: {user_id} ({user_name})")
        try:
            await update.effective_message.reply_text("❌ Accesso non autorizzato.")
        except:
            pass
        return False
    return True

# --- Telegram Conversation Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    
    welcome_message = (
        "🤖 *Benvenuto nel Bot Google Drive!*\n\n"
        "Cosa posso fare per te:\n"
        "📤 Invia file (documenti, foto, video, PDF, ecc.) e li carico su Drive\n"
        "📁 Naviga tra le cartelle usando i pulsanti\n"
        "🔍 Cerca file per nome con /search\n"
        "📋 Vedi i file in una cartella con /list\n"
        "🗑️ Elimina file con /delete\n\n"
        "Inviami un file per iniziare!"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    
    message = update.message
    user = message.from_user
    file_info = None

    # Handle different file types
    if message.document:
        file_info = {'file_id': message.document.file_id, 'file_name': message.document.file_name}
        logger.info(f"Document received from {user.first_name}: {file_info['file_name']}")
    elif message.photo:
        photo = message.photo[-1]
        file_info = {'file_id': photo.file_id, 'file_name': f"photo_{message.message_id}.jpg"}
        logger.info(f"Photo received from {user.first_name}.")
    elif message.video:
        file_info = {'file_id': message.video.file_id, 'file_name': message.video.file_name or f"video_{message.message_id}.mp4"}
        logger.info(f"Video received from {user.first_name}: {file_info.get('file_name')}")
    elif message.audio:
        file_info = {'file_id': message.audio.file_id, 'file_name': message.audio.file_name or f"audio_{message.message_id}.mp3"}
        logger.info(f"Audio received from {user.first_name}.")
    elif message.voice:
        file_info = {'file_id': message.voice.file_id, 'file_name': f"voice_{message.message_id}.ogg"}
        logger.info(f"Voice message received from {user.first_name}.")
    else:
        await message.reply_text("❌ Non posso gestire questo tipo di file.")
        return ConversationHandler.END

    media_group_id = message.media_group_id
    if media_group_id:
        if 'files_to_upload' not in context.user_data or context.user_data.get('media_group_id') != media_group_id:
            context.user_data['files_to_upload'] = [file_info]
            context.user_data['media_group_id'] = media_group_id
        else:
            context.user_data['files_to_upload'].append(file_info)

        keyboard = [[InlineKeyboardButton("✅ Ho finito", callback_data="done_uploading")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await message.reply_text(
            f"📎 File '{file_info['file_name']}' aggiunto. Hai {len(context.user_data['files_to_upload'])} file.\nInvia altri o premi 'Ho finito'.",
            reply_markup=reply_markup
        )
        
        return WAITING_FOR_MORE_FILES
    else:
        if file_info:
            context.user_data['files_to_upload'] = [file_info]
            await message.reply_text(f"✅ Ho ricevuto il file '{file_info['file_name']}'.")
            await show_folder_selection(update, context)
            return SELECT_FOLDER
    
    return ConversationHandler.END

async def done_uploading_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called when the user is done uploading files for a media group."""
    if not await check_access(update):
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    
    file_count = len(context.user_data.get('files_to_upload', []))
    await query.edit_message_text(f"✅ Perfetto! Hai selezionato {file_count} file da caricare.")
    
    await show_folder_selection(query, context)
    return SELECT_FOLDER

async def show_folder_selection(update, context: ContextTypes.DEFAULT_TYPE, folder_id: str = None):
    if folder_id is None:
        folder_id = GOOGLE_DRIVE_PARENT_FOLDER_ID
        context.user_data['current_folder_id'] = folder_id
        context.user_data['folder_path_stack'] = []

    drive_service = get_drive_service()
    query = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive_service.files().list(q=query, pageSize=20, orderBy="name", fields="files(id, name)").execute()
    items = results.get('files', [])

    keyboard = [[InlineKeyboardButton(f"📁 {item['name']}", callback_data=f"select_folder_{item['id']}")] for item in items]
    
    control_buttons = [InlineKeyboardButton("✅ Seleziona questa cartella", callback_data="confirm_folder")]
    if context.user_data['folder_path_stack']:
        control_buttons.append(InlineKeyboardButton("⬅️ Indietro", callback_data="back_folder"))
    
    keyboard.append(control_buttons)
    keyboard.append([InlineKeyboardButton("🔍 Cerca per percorso", callback_data="search_path")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    current_path_str = get_folder_path_string(drive_service, context.user_data['current_folder_id'])

    text = f"📂 Seleziona una cartella. Percorso corrente: `{current_path_str}`"
    
    # Check type and send appropriately
    if hasattr(update, 'edit_message_text'):
        await update.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    elif hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')

async def folder_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    data = query.data
    drive_service = get_drive_service()

    if data.startswith("select_folder_"):
        folder_id = data.split("_", 2)[2]
        folder_details = drive_service.files().get(fileId=context.user_data['current_folder_id'], fields='name').execute()
        context.user_data['folder_path_stack'].append({'id': context.user_data['current_folder_id'], 'name': folder_details.get('name', '..')})
        context.user_data['current_folder_id'] = folder_id
        await show_folder_selection(query, context, folder_id)
        return SELECT_FOLDER

    elif data == "back_folder":
        if context.user_data['folder_path_stack']:
            previous_folder = context.user_data['folder_path_stack'].pop()
            context.user_data['current_folder_id'] = previous_folder['id']
            await show_folder_selection(query, context, previous_folder['id'])
        return SELECT_FOLDER

    elif data == "confirm_folder":
        final_folder_id = context.user_data['current_folder_id']
        context.user_data['final_folder_id'] = final_folder_id
        context.user_data['upload_path'] = get_folder_path_string(drive_service, final_folder_id)
        context.user_data['needs_creation'] = False
        
        reply_keyboard = [['Sì', 'No']]
        await query.edit_message_text(
            f"✅ Hai selezionato la cartella `{context.user_data['upload_path']}`.\n📤 Vuoi caricare i file qui?",
            parse_mode='Markdown'
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Confermi?",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        )
        return CONFIRM_UPLOAD
        
    elif data == "search_path":
        await query.edit_message_text("🔍 Inserisci il percorso completo dove vuoi salvare il file (es. `Fatture/2025/Amazon`).", parse_mode='Markdown')
        return GET_PATH

async def get_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    
    path_input = update.message.text
    context.user_data['upload_path'] = path_input
    
    reply_keyboard = [['Sì', 'No']]
    drive_service = get_drive_service()
    
    final_folder_id = find_or_create_nested_folder(drive_service, path_input, GOOGLE_DRIVE_PARENT_FOLDER_ID)
    
    context.user_data['final_folder_id'] = final_folder_id
    context.user_data['needs_creation'] = True

    await update.message.reply_text(
        f"📁 Il percorso sarà `{path_input}`. Se non esiste, verrà creato.\n✅ Continuo?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True), parse_mode='Markdown'
    )
    return CONFIRM_UPLOAD

async def confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    
    user_reply = update.message.text.lower()
    
    if user_reply not in ['sì', 'si', 'no']:
        await update.message.reply_text("⚠️ Rispondi con 'Sì' o 'No'.")
        return CONFIRM_UPLOAD
        
    if user_reply == 'no':
        await update.message.reply_text("❌ Operazione annullata.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    files_to_upload = context.user_data.get('files_to_upload', [])
    path_info = context.user_data.get('upload_path')
    final_folder_id = context.user_data.get('final_folder_id')
    drive_service = get_drive_service()

    if context.user_data.get('needs_creation', False):
         final_folder_id = find_or_create_nested_folder(drive_service, path_info, GOOGLE_DRIVE_PARENT_FOLDER_ID)

    if not final_folder_id:
         await update.message.reply_text("❌ Errore critico: impossibile trovare o creare la cartella di destinazione.", reply_markup=ReplyKeyboardRemove())
         context.user_data.clear()
         return ConversationHandler.END

    await update.message.reply_text(f"⏳ Caricamento di {len(files_to_upload)} file in corso...", reply_markup=ReplyKeyboardRemove())

    script_dir = os.path.dirname(os.path.abspath(__file__))
    download_dir = Path(os.path.join(script_dir, "temp_downloads"))
    download_dir.mkdir(exist_ok=True)
    
    successful_uploads = 0
    uploaded_links = []
    
    for file_info in files_to_upload:
        file_path = download_dir / file_info['file_name']
        try:
            new_file = await context.bot.get_file(file_info['file_id'])
            await new_file.download_to_drive(file_path)
            logger.info(f"File '{file_info['file_name']}' downloaded to {file_path}")

            uploaded_file_id, web_link = upload_file_to_drive(drive_service, file_path, final_folder_id)

            if uploaded_file_id:
                successful_uploads += 1
                if web_link:
                    uploaded_links.append(f"• [{file_info['file_name']}]({web_link})")
            else:
                await update.message.reply_text(f"❌ Errore durante il caricamento di '{file_info['file_name']}'.")
        except Exception as e:
            logger.error(f"Error processing file {file_info['file_name']}: {e}")
            await update.message.reply_text(f"❌ Errore imprevisto con il file {file_info['file_name']}: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    result_message = f"✅ *Completato!* {successful_uploads}/{len(files_to_upload)} file caricati in `{path_info}`"
    if uploaded_links:
        result_message += "\n\n🔗 *Link ai file:*\n" + "\n".join(uploaded_links)
    
    await update.message.reply_text(result_message, parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

async def list_files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List files in current or specified folder."""
    if not await check_access(update):
        return
    
    drive_service = get_drive_service()
    folder_id = GOOGLE_DRIVE_PARENT_FOLDER_ID
    
    files = list_files_in_folder(drive_service, folder_id, page_size=20)
    
    if not files:
        await update.message.reply_text("📂 Nessun file trovato in questa cartella.")
        return
    
    message = "📋 *File nella cartella:*\n\n"
    for file in files:
        icon = "📁" if file['mimeType'] == 'application/vnd.google-apps.folder' else "📄"
        size = f" ({int(file.get('size', 0)) / 1024:.1f} KB)" if 'size' in file else ""
        message += f"{icon} `{file['name']}`{size}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search files by name."""
    if not await check_access(update):
        return
    
    if not context.args:
        await update.message.reply_text("🔍 Usa: /search <nome_file>")
        return
    
    search_query = ' '.join(context.args)
    drive_service = get_drive_service()
    
    files = search_files_by_name(drive_service, search_query)
    
    if not files:
        await update.message.reply_text(f"❌ Nessun file trovato con nome '{search_query}'.")
        return
    
    message = f"🔍 *Risultati per '{search_query}':*\n\n"
    for file in files[:10]:
        icon = "📁" if file['mimeType'] == 'application/vnd.google-apps.folder' else "📄"
        link = file.get('webViewLink', '')
        if link:
            message += f"{icon} [{file['name']}]({link})\n"
        else:
            message += f"{icon} `{file['name']}`\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    if not await check_access(update):
        return
    
    help_text = (
        "📚 *Comandi disponibili:*\n\n"
        "📤 *Carica file:* Invia un file qualsiasi\n"
        "📋 */list* - Elenca i file nella cartella root\n"
        "🔍 */search <nome>* - Cerca file per nome\n"
        "❓ */help* - Mostra questo messaggio\n"
        "❌ */cancel* - Annulla operazione corrente\n\n"
        "💡 *Tipi di file supportati:*\n"
        "• Documenti (PDF, DOC, TXT, MD, etc.)\n"
        "• Immagini (JPG, PNG, GIF, etc.)\n"
        "• Video e Audio\n"
        "• Qualsiasi altro tipo di file!"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text('❌ Operazione annullata.', reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Si è verificato un errore imprevisto. Il problema è stato registrato.")

def main():
    if not TELEGRAM_TOKEN or not GOOGLE_DRIVE_PARENT_FOLDER_ID:
        logger.error("TELEGRAM_TOKEN or GOOGLE_DRIVE_PARENT_FOLDER_ID missing.")
        exit(1)

    if AUTHORIZED_USER_ID == 123456789:
        logger.warning("⚠️ ATTENZIONE: AUTHORIZED_USER_ID non è stato modificato! Ricorda di inserire il tuo vero User ID Telegram!")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.ATTACHMENT, handle_attachment)],
        states={
            WAITING_FOR_MORE_FILES: [
                MessageHandler(filters.ATTACHMENT, handle_attachment),
                CallbackQueryHandler(done_uploading_callback, pattern='^done_uploading$')
            ],
            SELECT_FOLDER: [CallbackQueryHandler(folder_selection_callback)],
            GET_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_path)],
            CONFIRM_UPLOAD: [MessageHandler(filters.Regex('^(Sì|sì|si|No|no)$'), confirm_upload)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False
    )

    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_files_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(conv_handler)
    
    logger.info("🤖 Bot avviato con successo! 🔒 Modalità privata attiva.")
    logger.info(f"✅ Autorizzato solo user_id: {AUTHORIZED_USER_ID}")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    try:
        get_drive_service()
    except Exception as e:
        print(f"❌ ERRORE FATALE durante l'inizializzazione: {e}")
        exit(1)
        
    logger.info("🚀 Avvio Telegram Bot...")
    main()