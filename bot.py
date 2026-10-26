"""
bot.py
======

Telegram front-end for the email sender bot.

Structure
---------
* whitelist decorator  -> every handler starts with an access check
* /start, /menu        -> welcome + main inline keyboard
* Settings conversation -> add / test / delete / set-default configs
* Compose conversation  -> recipients -> subject -> body -> preview -> send
* Results screens       -> sent / rejected / failed lists

Everything is async. Blocking work (SMTP) is pushed to a thread inside
email_sender.py, so the event loop never stalls.

Run locally:   python bot.py
On Render:     Start Command = python bot.py   (polling, no webhooks)
"""

from __future__ import annotations
import asyncio
import functools
import html
import io
import database as db
import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
import email_sender as sender

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

# Settings
(
    SETTINGS_MENU,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_SENDER,
    API_PROVIDER,
    API_KEY,
    API_SENDER,
    CONFIRM_DELETE,
    SELECT_CONFIG_TARGET,
    SMTP_SENDER_NAME,
    API_SENDER_NAME,
) = range(13)

# Compose
(
    COMPOSE_RECIPIENTS,
    COMPOSE_SUBJECT,
    COMPOSE_BODY,
    COMPOSE_PREVIEW,
) = range(10, 14)

# Callback data prefixes (kept short - Telegram limits callback_data to 64 bytes)
CB_MENU = "menu"
CB_SETTINGS = "settings"
CB_ADD = "add"            # add:smtp | add:api
CB_TEST = "test"          # test:smtp | test:api
CB_DEL = "del"            # del:smtp | del:api
CB_DEL_YES = "delyes"     # delyes:smtp | delyes:api
CB_DEF = "def"            # def:smtp | def:api
CB_PROV = "prov"          # prov:brevo | prov:resend | prov:sendgrid
CB_COMPOSE = "compose"
CB_SEND = "send"
CB_SENDTEST = "sendtest"
CB_CANCEL = "cancel"
CB_RESULT = "res"         # res:sent | res:rejected | res:failed

# Max characters per Telegram message
TG_LIMIT = 4000

# Very permissive email check - the mail server is the real validator.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# Whitelist decorator
# ---------------------------------------------------------------------------

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def restricted(func: Handler) -> Handler:
    """
    Wrap a handler so it refuses anyone not in ALLOWED_USER_IDS.

    Works for commands, messages and callback queries. For callbacks we also
    answer the query so the spinner in the Telegram client stops.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id not in db.ALLOWED_USER_IDS:
            logger.warning(
                "Unauthorized access attempt by user_id=%s",
                getattr(user, "id", None),
            )
            if update.callback_query:
                await update.callback_query.answer("Not authorized", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("Not authorized")
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✉️ Compose", callback_data=CB_COMPOSE)],
            [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)]]
    )


# def settings_keyboard(configs: dict[str, Optional[db.UserConfig]]) -> InlineKeyboardMarkup:
#     """Build the settings keyboard: Add / Test / Delete / Set default per slot."""
#     rows: list[list[InlineKeyboardButton]] = []

#     for cfg_type, label in (("smtp", "SMTP"), ("api", "API")):
#         cfg = configs.get(cfg_type)
#         row: list[InlineKeyboardButton] = []
#         if cfg is None:
#             row.append(
#                 InlineKeyboardButton(f"➕ Add {label}", callback_data=f"{CB_ADD}:{cfg_type}")
#             )
#         else:
#             row.append(
#                 InlineKeyboardButton(f"🔌 Test {label}", callback_data=f"{CB_TEST}:{cfg_type}")
#             )
#             row.append(
#                 InlineKeyboardButton(f"🗑 Delete {label}", callback_data=f"{CB_DEL}:{cfg_type}")
#             )
#             if not cfg.is_default:
#                 row.append(
#                     InlineKeyboardButton(
#                         f"⭐ Make {label} default", callback_data=f"{CB_DEF}:{cfg_type}"
#                     )
#                 )
#         rows.append(row)

#     rows.append([InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)])
#     return InlineKeyboardMarkup(rows)
def settings_keyboard(configs: dict[str, Optional[db.UserConfig]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    
    has_smtp = configs.get("smtp") is not None
    has_api = configs.get("api") is not None

    # 1. Add buttons (Only show if that slot is empty)
    if not has_smtp:
        rows.append([InlineKeyboardButton("➕ Add SMTP", callback_data=f"{CB_ADD}:smtp")])
    if not has_api:
        rows.append([InlineKeyboardButton("➕ Add API", callback_data=f"{CB_ADD}:api")])

    # 2. Test/Delete buttons (Only show if at least one exists)
    if has_smtp or has_api:
        rows.append([
            InlineKeyboardButton("🔌 Test Config", callback_data=f"{CB_TEST}:menu"),
            InlineKeyboardButton("🗑 Delete Config", callback_data=f"{CB_DEL}:menu")
        ])

    rows.append([InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)])
    return InlineKeyboardMarkup(rows)

def settings_text(configs: dict[str, Optional[db.UserConfig]]) -> str:
    """Render the status block shown above the settings keyboard."""
    lines: list[str] = ["<b>⚙️ Settings</b>", ""]

    for cfg_type, label in (("smtp", "SMTP"), ("api", "API")):
        cfg = configs.get(cfg_type)
        if cfg is None:
            lines.append(f"{label}: ❌ none")
            continue
        star = " ⭐default" if cfg.is_default else ""
        if cfg_type == "smtp":
            detail = f"{cfg.smtp_username}@{cfg.smtp_host}:{cfg.smtp_port}"
        else:
            detail = f"{cfg.api_provider}"
        lines.append(
            f"{label}: ✅ saved ({html.escape(cfg.sender_address)}){star}\n"
            f"    ↳ {html.escape(detail)}"
        )

    lines.append("")
    lines.append("Compose always uses the ⭐default config.")
    return "\n".join(lines)


async def load_configs(user_id: int) -> dict[str, Optional[db.UserConfig]]:
    """Return {'smtp': cfg|None, 'api': cfg|None}."""
    configs = await db.list_configs(user_id)
    by_type: dict[str, Optional[db.UserConfig]] = {"smtp": None, "api": None}
    for cfg in configs:
        by_type[cfg.type] = cfg
    return by_type


async def show_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = True
) -> int:
    """Render (or re-render) the settings screen. Returns SETTINGS_MENU."""
    user_id = update.effective_user.id
    configs = await load_configs(user_id)
    text = settings_text(configs)
    keyboard = settings_keyboard(configs)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        except BadRequest as exc:
            # "Message is not modified" happens when nothing changed.
            if "not modified" not in str(exc).lower():
                raise
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    return SETTINGS_MENU


def chunk_lines(lines: list[str], limit: int = TG_LIMIT) -> list[str]:
    """Split a list of lines into messages that fit Telegram's limit."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks or [""]


# ---------------------------------------------------------------------------
# /start and /menu
# ---------------------------------------------------------------------------

@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Welcome message. Also ends any running conversation."""
    context.user_data.clear()
    await update.effective_message.reply_text(
        "<b>📬 Email Sender Bot</b>\n\n"
        "Send email through your own SMTP account or an API provider "
        "(Brevo / Resend / SendGrid).\n\n"
        "1. Open <b>⚙️ Settings</b> and add a sender config.\n"
        "2. Use <b>✉️ Compose</b> to send a message.\n\n"
        "Your password / API key is encrypted before it is stored.",
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@restricted
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the main menu from anywhere."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "<b>📬 Email Sender Bot</b>\n\nWhat would you like to do?",
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# SETTINGS CONVERSATION
# ---------------------------------------------------------------------------

@restricted
async def cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await show_settings(update, context)


@restricted
async def cb_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the add flow for SMTP or API."""
    query = update.callback_query
    await query.answer()
    cfg_type = query.data.split(":", 1)[1]

    # Guard: one config per type.
    existing = await db.get_config(update.effective_user.id, cfg_type)
    if existing is not None:
        await query.answer("You already have a config of this type.", show_alert=True)
        return await show_settings(update, context)

    context.user_data["new_config"] = {"type": cfg_type}

    if cfg_type == "smtp":
        await query.edit_message_text(
            "<b>➕ Add SMTP</b>\n\nStep 1/5 - send the SMTP <b>host</b>.\n"
            "Example: <code>smtp.gmail.com</code>",
            parse_mode=ParseMode.HTML,
        )
        return SMTP_HOST

    # API: choose provider with buttons.
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Brevo", callback_data=f"{CB_PROV}:brevo")],
            [InlineKeyboardButton("Resend", callback_data=f"{CB_PROV}:resend")],
            [InlineKeyboardButton("SendGrid", callback_data=f"{CB_PROV}:sendgrid")],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_SETTINGS)],
        ]
    )
    await query.edit_message_text(
        "<b>➕ Add API</b>\n\nStep 1/3 - choose your provider:",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return API_PROVIDER


# --- SMTP steps ------------------------------------------------------------

@restricted
async def smtp_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.effective_message.text or "").strip()
    if not host or " " in host:
        await update.effective_message.reply_text("Please send a valid host name.")
        return SMTP_HOST
    context.user_data["new_config"]["smtp_host"] = host
    await update.effective_message.reply_text(
        "Step 2/5 - send the <b>port</b>.\n"
        "Common: <code>587</code> (STARTTLS) or <code>465</code> (SSL).",
        parse_mode=ParseMode.HTML,
    )
    return SMTP_PORT


@restricted
async def smtp_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 65535):
        await update.effective_message.reply_text("Port must be a number 1-65535.")
        return SMTP_PORT
    context.user_data["new_config"]["smtp_port"] = int(raw)
    await update.effective_message.reply_text(
        "Step 3/5 - send the SMTP <b>username</b> (usually your email address).",
        parse_mode=ParseMode.HTML,
    )
    return SMTP_USERNAME


@restricted
async def smtp_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.effective_message.text or "").strip()
    if not username:
        await update.effective_message.reply_text("Username cannot be empty.")
        return SMTP_USERNAME
    context.user_data["new_config"]["smtp_username"] = username
    await update.effective_message.reply_text(
        "Step 4/5 - send the SMTP <b>password</b>.\n\n"
        "⚠️ For Gmail use an App Password, not your normal password.\n"
        "The message will be deleted from this chat after saving.",
        parse_mode=ParseMode.HTML,
    )
    return SMTP_PASSWORD


@restricted
async def smtp_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = (update.effective_message.text or "").strip()
    if not password:
        await update.effective_message.reply_text("Password cannot be empty.")
        return SMTP_PASSWORD

    # Best effort: remove the password from the chat history.
    try:
        await update.effective_message.delete()
    except BadRequest:
        pass

    context.user_data["new_config"]["secret"] = password
    await update.effective_message.reply_text(
        "Step 5/5 - send the <b>sender address</b> (the From: address).",
        parse_mode=ParseMode.HTML,
    )
    return SMTP_SENDER


@restricted
async def smtp_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = (update.effective_message.text or "").strip()
    if not EMAIL_RE.match(address):
        await update.effective_message.reply_text("That does not look like an email address.")
        return SMTP_SENDER

    cfg = context.user_data["new_config"]
    cfg["sender_address"] = address

    # Ensure the sender address is stored in the new config
    context.user_data["new_config"]["sender_address"] = address
    await update.effective_message.reply_text(
        "Step 6/6 - send the <b>sender name</b> (e.g., 'Support Team').\n"
        "This is the name that appears in the recipient's inbox.",
        parse_mode=ParseMode.HTML,
    )
    return SMTP_SENDER_NAME

    saved = await db.save_config(
        user_id=update.effective_user.id,
        config_type="smtp",
        sender_address=address,
        secret_plaintext=cfg["secret"],
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        smtp_username=cfg["smtp_username"],
    )
    context.user_data.pop("new_config", None)

    await status.edit_text(
        "✅ <b>SMTP saved</b>\n\n"
        f"Host: <code>{html.escape(saved.smtp_host or '')}</code>\n"
        f"From: <code>{html.escape(saved.sender_address)}</code>\n"
        f"Default: {'yes ⭐' if saved.is_default else 'no'}",
        parse_mode=ParseMode.HTML,
    )
    return await show_settings(update, context, edit=False)

@restricted
async def smtp_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text or "").strip()
    context.user_data["new_config"]["sender_name"] = name

    cfg = context.user_data["new_config"]
    status = await update.effective_message.reply_text("🔌 Testing connection…")

    result = await sender.test_smtp(
        cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_username"], cfg["secret"]
    )

    if not result.ok:
        await status.edit_text(
            "❌ <b>Test failed - nothing saved</b>\n\n"
            f"<code>{html.escape(result.message)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Settings", callback_data=CB_SETTINGS)]]
            ),
        )
        context.user_data.pop("new_config", None)
        return SETTINGS_MENU

    saved = await db.save_config(
        user_id=update.effective_user.id,
        config_type="smtp",
        sender_address=cfg["sender_address"],
        secret_plaintext=cfg["secret"],
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        smtp_username=cfg["smtp_username"],
        sender_name=cfg.get("sender_name"), # Pass the new name!
    )
    context.user_data.pop("new_config", None)

    await status.edit_text(
        f"✅ <b>SMTP saved</b>\n\nFrom: <code>{html.escape(saved.sender_name or '')} &lt;{html.escape(saved.sender_address)}&gt;</code>",
        parse_mode=ParseMode.HTML,
    )
    return await show_settings(update, context, edit=False)
# --- API steps -------------------------------------------------------------

@restricted
async def api_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    provider = query.data.split(":", 1)[1]
    if provider not in sender.API_PROVIDERS:
        await query.answer("Unknown provider", show_alert=True)
        return API_PROVIDER

    context.user_data.setdefault("new_config", {"type": "api"})
    context.user_data["new_config"]["api_provider"] = provider

    await query.edit_message_text(
        f"<b>➕ Add API - {sender.API_PROVIDERS[provider]['label']}</b>\n\n"
        "Step 2/3 - send your <b>API key</b>.\n"
        "The message will be deleted from this chat after saving.",
        parse_mode=ParseMode.HTML,
    )
    return API_KEY


@restricted
async def api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = (update.effective_message.text or "").strip()
    if not key:
        await update.effective_message.reply_text("API key cannot be empty.")
        return API_KEY

    try:
        await update.effective_message.delete()
    except BadRequest:
        pass

    context.user_data["new_config"]["secret"] = key
    await update.effective_message.reply_text(
        "Step 3/3 - send the <b>sender address</b>.\n"
        "It must be a verified sender/domain in your provider account.",
        parse_mode=ParseMode.HTML,
    )
    return API_SENDER


@restricted
async def api_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = (update.effective_message.text or "").strip()
    if not EMAIL_RE.match(address):
        await update.effective_message.reply_text("That does not look like an email address.")
        return API_SENDER

    cfg = context.user_data["new_config"]
    cfg["sender_address"] = address
    await update.effective_message.reply_text(
        "Step 4/4 - send the <b>sender name</b> (e.g., 'Support Team').\n"
        "This is the name that appears in the recipient's inbox.",
        parse_mode=ParseMode.HTML,
    )
    return API_SENDER_NAME
    saved = await db.save_config(
        user_id=update.effective_user.id,
        config_type="api",
        sender_address=address,
        secret_plaintext=cfg["secret"],
        api_provider=cfg.get("api_provider"),
    )
    context.user_data.pop("new_config", None)

    await status.edit_text(
        "✅ <b>API config saved</b>\n\n"
        f"Provider: <code>{html.escape(saved.api_provider or '')}</code>\n"
        f"From: <code>{html.escape(saved.sender_address)}</code>\n"
        f"Default: {'yes ⭐' if saved.is_default else 'no'}",
        parse_mode=ParseMode.HTML,
    )
    return await show_settings(update, context, edit=False)
@restricted
async def api_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text or "").strip()
    context.user_data["new_config"]["sender_name"] = name

    cfg = context.user_data["new_config"]
    status = await update.effective_message.reply_text("🔌 Validating API key…")
    result = await sender.test_api(cfg["api_provider"], cfg["secret"])

    if not result.ok:
        await status.edit_text(
            "❌ <b>Test failed - nothing saved</b>\n\n"
            f"<code>{html.escape(result.message)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Settings", callback_data=CB_SETTINGS)]]
            ),
        )
        context.user_data.pop("new_config", None)
        return SETTINGS_MENU

    saved = await db.save_config(
        user_id=update.effective_user.id,
        config_type="api",
        sender_address=cfg["sender_address"],
        secret_plaintext=cfg["secret"],
        api_provider=cfg["api_provider"],
        sender_name=cfg.get("sender_name"), # Pass the new name!
    )
    context.user_data.pop("new_config", None)

    await status.edit_text(
        f"✅ <b>API config saved</b>\n\nFrom: <code>{html.escape(saved.sender_name or '')} &lt;{html.escape(saved.sender_address)}&gt;</code>",
        parse_mode=ParseMode.HTML,
    )
    return await show_settings(update, context, edit=False)

# --- Test / delete / set default ------------------------------------------

@restricted
async def cb_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, target = query.data.split(":", 1)

    # If user clicked the main "Test Config" button
    if target == "menu":
        configs = await load_configs(update.effective_user.id)
        buttons = []
        if configs.get("smtp"):
            buttons.append([InlineKeyboardButton("📧 Test SMTP", callback_data=f"{CB_TEST}:smtp")])
        if configs.get("api"):
            buttons.append([InlineKeyboardButton("🔌 Test API", callback_data=f"{CB_TEST}:api")])
        
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_SETTINGS)])
        await query.edit_message_text(
            "<b>Which config do you want to test?</b>",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )
        return SETTINGS_MENU

    # If user clicked "Test SMTP" or "Test API"
    cfg = await db.get_config(update.effective_user.id, target)
    if cfg is None:
        await query.answer("Config not found.", show_alert=True)
        return await show_settings(update, context)

    await query.edit_message_text("🔌 Testing saved config…")

    try:
        secret = db.decrypt_secret(cfg.secret_encrypted)
    except db.SecretDecryptionError as exc:
        await query.edit_message_text(
            f"❌ {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=CB_SETTINGS)]])
        )
        return SETTINGS_MENU

    if target == "smtp":
        result = await sender.test_smtp(cfg.smtp_host or "", cfg.smtp_port or 0, cfg.smtp_username or "", secret)
    else:
        result = await sender.test_api(cfg.api_provider or "", secret)

    icon = "✅" if result.ok else "❌"
    await query.edit_message_text(
        f"{icon} <b>Test result</b>\n\n<code>{html.escape(result.message)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=CB_SETTINGS)]])
    )
    return SETTINGS_MENU

@restricted
@restricted
async def cb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, target = query.data.split(":", 1)

    # If user clicked the main "Delete Config" button
    if target == "menu":
        configs = await load_configs(update.effective_user.id)
        buttons = []
        if configs.get("smtp"):
            buttons.append([InlineKeyboardButton("📧 Delete SMTP", callback_data=f"{CB_DEL}:smtp")])
        if configs.get("api"):
            buttons.append([InlineKeyboardButton("🔌 Delete API", callback_data=f"{CB_DEL}:api")])
        
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_SETTINGS)])
        await query.edit_message_text(
            "<b>Which config do you want to delete?</b>",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )
        return SETTINGS_MENU

    # If user clicked "Delete SMTP" or "Delete API"
    cfg = await db.get_config(update.effective_user.id, target)
    if cfg is None:
        await query.answer("Config not found.", show_alert=True)
        return await show_settings(update, context)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"{CB_DEL_YES}:{target}"),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_SETTINGS),
            ]
        ]
    )
    await query.edit_message_text(
        f"🗑 Delete the <b>{target.upper()}</b> config for "
        f"<code>{html.escape(cfg.sender_address)}</code>?\n\n"
        "This cannot be undone.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return CONFIRM_DELETE

@restricted
async def cb_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # The callback data will look like "delyes:smtp" or "delyes:api"
    cfg_type = query.data.split(":", 1)[1]
    
    deleted = await db.delete_config(update.effective_user.id, cfg_type)
    if deleted:
        await query.answer("Deleted.")
    else:
        await query.answer("Nothing to delete.", show_alert=True)

    # Go back to the main Settings menu after deleting
    return await show_settings(update, context)


@restricted
async def cb_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    cfg_type = query.data.split(":", 1)[1]

    await db.set_default(update.effective_user.id, cfg_type)
    await query.answer("Default updated ⭐")
    return await show_settings(update, context)


# ---------------------------------------------------------------------------
# COMPOSE CONVERSATION
# ---------------------------------------------------------------------------



@restricted
async def cb_compose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: check that a default config exists, then ask for recipients."""
    query = update.callback_query
    await query.answer()

    default_cfg = await db.get_default_config(update.effective_user.id)
    if default_cfg is None:
        await query.edit_message_text(
            "⚠️ <b>No sender configured</b>\n\n"
            "Add an SMTP or API config in Settings first.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
                    [InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)],
                ]
            ),
        )
        return ConversationHandler.END

    context.user_data["compose"] = {
        "config_id": default_cfg.id,
        "sender": default_cfg.sender_address,
    }

    await query.edit_message_text(
        "<b>✉️ Compose - Step 1/3</b>\n\n"
        "Send the <b>recipients</b>:\n"
        "• type addresses separated by commas, or\n"
        "• upload a <code>.txt</code> file with one address per line.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]
        ),
    )
    return COMPOSE_RECIPIENTS


def parse_recipients(raw: str) -> list[str]:
    """Split on commas / semicolons / newlines, validate, dedupe (case-insensitive)."""
    parts = re.split(r"[,;\n\r]+", raw)
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        addr = part.strip().strip("<>").strip()
        if not addr or not EMAIL_RE.match(addr):
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(addr)
    return result


@restricted
async def compose_recipients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    raw = ""

    if message.document:
        doc = message.document
        if not (doc.file_name or "").lower().endswith(".txt"):
            await message.reply_text("Please upload a .txt file, or type the addresses.")
            return COMPOSE_RECIPIENTS
        if doc.file_size and doc.file_size > 2 * 1024 * 1024:
            await message.reply_text("File too large (max 2 MB).")
            return COMPOSE_RECIPIENTS
        tg_file = await doc.get_file()
        buffer = io.BytesIO()
        await tg_file.download_to_memory(buffer)
        raw = buffer.getvalue().decode("utf-8", errors="replace")
    else:
        raw = message.text or ""

    recipients = parse_recipients(raw)
    if not recipients:
        await message.reply_text(
            "No valid email addresses found. Try again (comma-separated)."
        )
        return COMPOSE_RECIPIENTS

    context.user_data["compose"]["recipients"] = recipients
    await message.reply_text(
        f"✅ <b>{len(recipients)} recipient(s)</b> parsed.\n\n"
        "<b>Step 2/3</b> - send the <b>subject</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]
        ),
    )
    return COMPOSE_SUBJECT


@restricted
async def compose_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subject = (update.effective_message.text or "").strip()
    if not subject:
        await update.effective_message.reply_text("Subject cannot be empty.")
        return COMPOSE_SUBJECT
    if len(subject) > 200:
        await update.effective_message.reply_text("Subject is too long (max 200 chars).")
        return COMPOSE_SUBJECT

    context.user_data["compose"]["subject"] = subject
    await update.effective_message.reply_text(
        "<b>Step 3/3</b> - send the <b>body</b>:\n"
        "• type plain text, or\n"
        "• upload an <code>.html</code> file.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]
        ),
    )
    return COMPOSE_BODY


@restricted
async def compose_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    body = ""
    is_html = False

    if message.document:
        doc = message.document
        name = (doc.file_name or "").lower()
        if not (name.endswith(".html") or name.endswith(".htm")):
            await message.reply_text("Please upload an .html file, or type the body.")
            return COMPOSE_BODY
        if doc.file_size and doc.file_size > 2 * 1024 * 1024:
            await message.reply_text("File too large (max 2 MB).")
            return COMPOSE_BODY
        tg_file = await doc.get_file()
        buffer = io.BytesIO()
        await tg_file.download_to_memory(buffer)
        body = buffer.getvalue().decode("utf-8", errors="replace")
        is_html = True
    else:
        body = message.text or ""

    if not body.strip():
        await message.reply_text("Body cannot be empty.")
        return COMPOSE_BODY

    context.user_data["compose"]["body"] = body
    context.user_data["compose"]["is_html"] = is_html

    return await show_preview(update, context)


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the preview screen with Send / Send test / Cancel."""
    data = context.user_data["compose"]
    recipients: list[str] = data["recipients"]
    subject: str = data["subject"]
    body: str = data["body"]
    is_html: bool = data["is_html"]

    snippet = re.sub(r"<[^>]+>", " ", body) if is_html else body
    snippet = re.sub(r"\s+", " ", snippet).strip()[:300]

    text = (
        "<b>📝 Preview</b>\n\n"
        f"<b>From:</b> <code>{html.escape(data['sender'])}</code>\n"
        f"<b>Recipients:</b> {len(recipients)}\n"
        f"<b>Subject:</b> {html.escape(subject)}\n"
        f"<b>Body:</b> {'HTML' if is_html else 'plain text'}\n\n"
        f"<i>{html.escape(snippet)}{'…' if len(snippet) >= 300 else ''}</i>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Send", callback_data=CB_SEND)],
            [InlineKeyboardButton("🧪 Send test to me", callback_data=CB_SENDTEST)],
            [InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)],
        ]
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    return COMPOSE_PREVIEW

@restricted
async def cb_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    recipients = context.user_data["compose"]["recipients"]
    return await _run_send(update, context, recipients, label="Sending emails")

@restricted
async def cb_send_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Send the test email to the sender's own address
    test_recipient = [context.user_data["compose"]["sender"]]
    return await _run_send(update, context, test_recipient, label="Sending test email")

@restricted
async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Cancelled")
    context.user_data.clear()
    await query.edit_message_text(
        "❌ Cancelled.",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# SENDING
# ---------------------------------------------------------------------------

async def _run_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    recipients: list[str],
    *,
    label: str,
) -> int:
    """
    Shared send routine for both "Send" and "Send test to me".

    Loads the config fresh from the DB (so edits made in Settings are picked
    up), decrypts the secret, calls email_sender, then renders the summary.
    """
    query = update.callback_query
    await query.answer()

    data = context.user_data.get("compose")
    if not data:
        await query.edit_message_text(
            "Session expired. Start again from the menu.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    await query.edit_message_text(f"📤 {label}… ({len(recipients)} recipient(s))")

    # Re-read the config: it may have been deleted or changed meanwhile.
    configs = await load_configs(update.effective_user.id)
    cfg = configs.get("smtp") if data.get("config_id") else None
    if cfg is None or cfg.id != data["config_id"]:
        cfg = await db.get_default_config(update.effective_user.id)
    if cfg is None:
        await query.edit_message_text(
            "⚠️ <b>No sender configured</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
                    [InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)],
                ]
            ),
        )
        return ConversationHandler.END

    try:
        secret = db.decrypt_secret(cfg.secret_encrypted)
    except db.SecretDecryptionError as exc:
        await query.edit_message_text(
            f"⚠️ <b>Sender issue - nothing sent</b>\n\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
                    [InlineKeyboardButton("🏠 Menu", callback_data=CB_MENU)],
                ]
            ),
        )
        return ConversationHandler.END

    try:
        summary = await sender.send_emails(
            config_type=cfg.type,
            sender=cfg.sender_address,
            sender_name=cfg.sender_address.split("@")[0],
            recipients=recipients,
            subject=data["subject"],
            body=data["body"],
            is_html=data["is_html"],
            secret=secret,
            api_provider=cfg.api_provider,
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_username=cfg.smtp_username,
        )
          # --- ADD THIS NEW BLOCK HERE ---
        if summary.sender_issue:
            await query.edit_message_text(
                f"⚠️ <b>Sender issue - nothing sent</b>\n\n"
                f"<code>{html.escape(summary.sender_issue_reason)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard()
            )
            return ConversationHandler.END

        # Show the results summary
        text = (
            f"📤 <b>Send complete</b>\n\n"
            f"✅ Sent: {len(summary.sent)}\n"
            f"❌ Rejected: {len(summary.rejected)}\n"
            f"⚠️ Failed: {len(summary.failed)}"
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>Failed to send</b>\n\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    return ConversationHandler.END
# compose conversation handler
compose_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(cb_compose, pattern=f"^{CB_COMPOSE}$")],
    states={
        COMPOSE_RECIPIENTS: [MessageHandler(filters.ALL & ~filters.COMMAND, compose_recipients)],
        COMPOSE_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, compose_subject)],
        COMPOSE_BODY: [MessageHandler(filters.ALL & ~filters.COMMAND, compose_body)],
            COMPOSE_PREVIEW: [
            CallbackQueryHandler(cb_send, pattern=f"^{CB_SEND}$"),
            CallbackQueryHandler(cb_send_test, pattern=f"^{CB_SENDTEST}$"),
            CallbackQueryHandler(cb_cancel, pattern=f"^{CB_CANCEL}$"),
        ],
    },
    fallbacks=[
        CommandHandler("start", cmd_start),
        CallbackQueryHandler(cb_menu, pattern=f"^{CB_MENU}$"),
        CallbackQueryHandler(cb_cancel, pattern=f"^{CB_CANCEL}$"),
    ],
)

# settings conversation handler
settings_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(cb_settings, pattern=f"^{CB_SETTINGS}$")],
    states={
        SETTINGS_MENU: [
            CallbackQueryHandler(cb_add, pattern=f"^{CB_ADD}:"),
            CallbackQueryHandler(cb_test, pattern=f"^{CB_TEST}:"),
            CallbackQueryHandler(cb_delete, pattern=f"^{CB_DEL}:"),
            CallbackQueryHandler(cb_set_default, pattern=f"^{CB_DEF}:"),
            CallbackQueryHandler(cb_settings, pattern=f"^{CB_SETTINGS}$"),
        ],
        SMTP_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_host)],
        SMTP_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_port)],
        SMTP_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_username)],
        SMTP_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_password)],
        SMTP_SENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_sender)],
        SMTP_SENDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, smtp_sender_name)],
        API_PROVIDER: [CallbackQueryHandler(api_provider, pattern=f"^{CB_PROV}:")],
        API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_key)],
        API_SENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_sender)],
        API_SENDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_sender_name)],
        CONFIRM_DELETE: [CallbackQueryHandler(cb_delete_yes, pattern=f"^{CB_DEL_YES}:")],
        SELECT_CONFIG_TARGET: [CallbackQueryHandler(cb_test), CallbackQueryHandler(cb_delete)],
    },
    fallbacks=[
        CommandHandler("start", cmd_start),
        CallbackQueryHandler(cb_menu, pattern=f"^{CB_MENU}$"),
    ],
)

def main():
    # Create and set the event loop for Python 3.15
    loop = asyncio.new_event_loop()
    loop.run_until_complete(db.init_db())
    asyncio.set_event_loop(loop)

    # Build the application
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

  
    
        # --- REGISTER HANDLERS ---
    
    # 1. The /start and /menu commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start)) 
    
    # 2. The Compose and Settings conversations
    application.add_handler(settings_conv)
    application.add_handler(compose_conv)

    # 3. Global callback buttons (Menu, Cancel)
    application.add_handler(CallbackQueryHandler(cb_menu, pattern=f"^{CB_MENU}$"))
    application.add_handler(CallbackQueryHandler(cb_cancel, pattern=f"^{CB_CANCEL}$"))

    # Start the bot!
    print("✅ Bot is polling...")
    application.run_polling( close_loop=False, 
    timeout=60, 
    read_timeout=60, 
    connect_timeout=60)
if __name__ == "__main__":
    main()