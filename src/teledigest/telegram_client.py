# isort: skip_file
from __future__ import annotations

import asyncio
import datetime as dt
import html
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from enum import Enum, auto

from telethon import TelegramClient, events, functions, types
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.channels import JoinChannelRequest

from .config import AppConfig, get_config, log
from .db import get_messages_last_24h, get_relevant_messages_last_24h, save_message
from .llm import build_prompt, llm_summarize, llm_summarize_brief
from .message_utils import reply_long
from .telegraph import post_to_telegraph

user_client: TelegramClient | None = None
bot_client: TelegramClient | None = None

# We'll store numeric chat IDs of channels we care about
scraped_chat_ids: set[int] = set()
chat_id_to_name: dict[int, str] = {}

ok_mark = "\u2705"
cross_mark = "\u274c"


class UserAuthState(Enum):
    OK = auto()
    REQUIRED = auto()
    IN_PROGRESS = auto()


class AuthStep(Enum):
    WAIT_PHONE = auto()
    WAIT_CODE = auto()


@dataclass
class AuthDialog:
    step: AuthStep
    phone: str | None = None
    phone_code_hash: str | None = None


user_auth_state: UserAuthState = UserAuthState.REQUIRED
auth_dialogs: dict[int, AuthDialog] = {}

SUPPORTED_COMMANDS: dict[str, str] = {
    "/auth": (
        "Authorize the scraping user client (accounts with a cloud password "
        "are authorized on the host with --auth)"
    ),
    "/help": "Show this help message",
    "/start": "Alias for /help",
    "/today": "Generate a digest now from the last 24 hours of messages",
    "/digest": "Alias for /today",
    "/status": "Show bot status and configuration summary",
}


def _redact(message: str, *secrets: str) -> str:
    """
    Scrub user-supplied secrets out of text that is about to be sent to a chat.

    Telethon error strings sometimes quote the value that was rejected, and the
    /auth handlers echo error text back to the operator. Anything the operator
    typed during a login dialog is treated as a secret and never echoed.
    """
    out = message
    for secret in secrets:
        for variant in {
            secret,
            secret.strip(),
            "".join(ch for ch in secret if ch.isalnum() or ch == "-"),
        }:
            if len(variant) >= 3:
                out = out.replace(variant, "[redacted]")
    return out


def auth_instructions() -> str:
    """
    Operator-facing instructions for the out-of-band (TTY) login.

    Accounts with a cloud password (2FA) are authorized on the host rather than
    through this chat — see the comment in auth_dialog_handler for why.
    """
    return (
        f"{cross_mark} This account has a cloud password (2FA) enabled.\n\n"
        "For safety, the cloud password is never accepted over this chat — it "
        "would be stored in the message history of the account it protects. "
        "Authorize on the host instead:\n\n"
        "1. Stop the service so it releases the session file:\n"
        "   <code>sudo systemctl stop teledigest</code>\n"
        "2. Log in interactively (you will be prompted for phone, code, and "
        "password; the password is read from the terminal and never stored):\n"
        "   <code>teledigest --config /path/to/teledigest.conf --auth</code>\n"
        "3. Start the service again:\n"
        "   <code>sudo systemctl start teledigest</code>\n\n"
        "Then send <code>/status</code> here to confirm the user client is "
        "authorized."
    )


async def channel_message_handler(event):
    """
    Handles all new messages, but only stores those from scraped_chat_ids.
    """
    chat_id = event.chat_id

    if chat_id not in scraped_chat_ids:
        return  # not one of our target channels

    msg = event.message
    text = msg.message or ""
    date = msg.date
    chat_name = chat_id_to_name.get(chat_id, str(chat_id))
    msg_id = f"{chat_name}_{msg.id}"

    log.info("Got message from %s (id=%s)", chat_name, msg.id)
    save_message(msg_id, chat_name, date, text)


async def is_user_allowed(event) -> bool:
    cfg = get_config()

    # If no restriction configured, allow everyone
    if not cfg.bot.allowed_user_ids and not cfg.bot.allowed_user_names:
        return True

    sender = await event.get_sender()
    username = (getattr(sender, "username", None) or "").lower() if sender else ""
    return event.sender_id in cfg.bot.allowed_user_ids or (
        bool(username) and username in cfg.bot.allowed_user_names
    )


async def help_command(event):
    if not await is_user_allowed(event):
        log.info("/help denied for user_id=%s", event.sender_id)
        await event.reply(f"{cross_mark} You are not allowed to use this command.")
        return

    lines = ["<b>Supported commands</b>", ""]
    for cmd, desc in SUPPORTED_COMMANDS.items():
        lines.append(f"<code>{cmd}</code> — {desc}")

    await event.reply("\n".join(lines), parse_mode="html")


async def today_command(event):
    # permissions check if you added one
    if not await is_user_allowed(event):
        log.info("/today denied for user_id=%s", event.sender_id)
        await event.reply(f"{cross_mark} You are not allowed to use this command.")
        return

    day = dt.date.today()
    log.info(
        "/today requested by %s for rolling last 24h (labelled as %s)",
        event.sender_id,
        day.isoformat(),
    )

    messages = get_relevant_messages_last_24h(max_docs=get_config().llm.max_messages)

    if not messages:
        await event.reply("No messages available for the last 24 hours.")
        return

    summary = llm_summarize(day, messages)

    cfg = get_config()
    if cfg.bot.summary_brief:
        telegraph_url = post_to_telegraph(
            title=f"Digest {day.isoformat()}", html=summary
        )
        brief = llm_summarize_brief(day, summary)
        outgoing = (
            f"{brief}\n\n" f'<a href="{telegraph_url}">Full digest on Telegraph</a>'
        )
    else:
        outgoing = summary

    await reply_long(event, outgoing, parse_mode="html")


async def auth_start_command(event):
    # permissions
    if not await is_user_allowed(event):
        log.info("/auth denied for user_id=%s", event.sender_id)
        await event.reply(f"{cross_mark} You are not allowed to use this command.")
        return

    chat_id = event.chat_id

    if user_auth_state == UserAuthState.OK:
        await event.reply(f"{ok_mark} User client is already authorized.")
        return

    auth_dialogs[chat_id] = AuthDialog(step=AuthStep.WAIT_PHONE)

    await event.reply(
        "Please send your phone number in international format:\n"
        "<code>+123456789</code>\n\n"
        "<i>If this account has a cloud password (2FA), authorization is "
        "completed on the host instead — the password is never accepted over "
        "this chat. The bot will show you the exact steps.</i>",
        parse_mode="html",
    )


async def auth_dialog_handler(event):
    # Ignore commands entirely
    if event.raw_text.startswith("/"):
        return

    chat_id = event.chat_id
    if chat_id not in auth_dialogs:
        return

    # permissions
    if not await is_user_allowed(event):
        log.info("/auth denied for user_id=%s", event.sender_id)
        await event.reply(f"{cross_mark} You are not allowed to use this command.")
        return

    dialog = auth_dialogs[chat_id]
    text = event.raw_text.strip()

    # Phone number step
    if dialog.step == AuthStep.WAIT_PHONE:
        try:
            sent = await user_client.send_code_request(text)
            dialog.phone = text
            dialog.phone_code_hash = sent.phone_code_hash
            dialog.step = AuthStep.WAIT_CODE

            await event.reply(
                "Code sent.\n"
                "Please type the 2FA code you received, but add SPACES between each digit "
                "(for example: 1 2 3 4 5).\n"
                "Do not forward the message; type the code manually."
            )
        except Exception as e:
            del auth_dialogs[chat_id]
            await event.reply(
                f"{cross_mark} Failed to send code: {_redact(str(e), text)}"
            )

    # Code step
    elif dialog.step == AuthStep.WAIT_CODE:
        try:
            await user_client.sign_in(
                phone=dialog.phone,
                code="".join(ch for ch in text if ch.isalnum() or ch == "-"),
                phone_code_hash=dialog.phone_code_hash,
            )

            del auth_dialogs[chat_id]

            global user_auth_state
            user_auth_state = UserAuthState.OK

            await user_client.get_me()
            await ensure_joined_and_resolve_channels()
            await event.reply(f"{ok_mark} Authorization successful!")

        except SessionPasswordNeededError:
            # DELIBERATE: there is no in-chat password step here, and adding one
            # is not the "obvious missing feature" it looks like.
            #
            # The login code above is single-use and expires in minutes. A cloud
            # (2FA) password is long-lived, reusable, and is precisely the
            # credential that is supposed to survive a stolen session. Typing it
            # into this chat would write it into the message history of the very
            # account it protects, turning session theft into full account
            # takeover. delete_messages() does not fix that: deletion is
            # best-effort and racy, and in the exact situation where /auth is
            # needed the bot may not be running to receive and delete it. The
            # generic handler below also echoes exception text back into the
            # chat, which would be a live leak path for a password.
            #
            # The password is handled out-of-band instead, over a TTY, by
            # `teledigest --auth` (Telethon's client.start() prompts for it via
            # getpass). See auth_instructions() and README "First run &
            # authentication".
            del auth_dialogs[chat_id]
            await event.reply(auth_instructions(), parse_mode="html")
        except Exception as e:
            del auth_dialogs[chat_id]
            await event.reply(
                f"{cross_mark} Authorization failed: "
                # Escaped: error text is attacker/Telegram-controlled and this
                # reply is parsed as HTML.
                f"{html.escape(_redact(str(e), text))}\n"
                "Send <code>/auth</code> to try again.",
                parse_mode="html",
            )


async def status_command(event):
    # permissions
    if not await is_user_allowed(event):
        log.info("/status denied for user_id=%s", event.sender_id)
        await event.reply(f"{cross_mark} You are not allowed to use this command.")
        return

    cfg = get_config()
    tz = ZoneInfo(cfg.bot.time_zone)
    day = dt.datetime.now(tz).date()

    log.info(
        "/status requested by %s (rolling last 24h, labelled as %s in %s)",
        event.sender_id,
        day.isoformat(),
        cfg.bot.time_zone,
    )

    relevant = get_relevant_messages_last_24h(max_docs=get_config().llm.max_messages)
    parsed = get_messages_last_24h()

    # A light sanity check for prompt size (useful for troubleshooting)
    prompt_chars = 0
    if relevant:
        _, user_prompt = build_prompt(day, relevant)
        prompt_chars = len(user_prompt)

    digest_time = f"{cfg.bot.summary_hour:02d}:{cfg.bot.summary_minute:02d}"

    channels_list = "\n".join([f"• <code>{c}</code>" for c in cfg.bot.channels])

    text = (
        "<b>Teledigest status</b>\n\n"
        f"<b>Parsed messages (last 24h, UTC):</b> <code>{len(parsed)}</code>\n"
        f"<b>Relevant messages (last 24h, UTC):</b> <code>{len(relevant)}</code>\n"
        f"<b>Planned digest post time:</b> <code>{digest_time}</code> (<code>{cfg.bot.time_zone}</code>)\n"
        f"<b>LLM model:</b> <code>{cfg.llm.model}</code>\n"
        f"<b>Target channel:</b> <code>{cfg.bot.summary_target}</code>\n"
        f"<b>Scrape channels:</b>\n{channels_list}\n"
    )

    if user_auth_state != UserAuthState.OK:
        text += (
            f"\n\n<b>User client:</b> {cross_mark} <b>Authorization required</b>\n"
            "Use <code>/auth</code> to authorize the scraping account.\n"
            "<i>Accounts with a cloud password (2FA) are authorized on the "
            "host with <code>teledigest --auth</code>.</i>"
        )
    else:
        text += f"\n\n<b>User client:</b> {ok_mark} Authorized"

    if relevant:
        text += f"\n<b>Current prompt size:</b> <code>{prompt_chars}</code> chars"
    else:
        text += "\n\n<i>No relevant messages found in the last 24 hours.</i>"

    await reply_long(event, text, parse_mode="html")


async def ensure_joined_and_resolve_channels():
    """
    Using the user account:
    - join channels from CHANNELS
    - resolve their peer chat_ids (same format as event.chat_id)
    """
    global scraped_chat_ids, chat_id_to_name
    scraped_chat_ids = set()
    chat_id_to_name = {}

    cfg = get_config()

    for ch in cfg.bot.channels:
        try:
            # Resolve entity
            ent = await user_client.get_entity(ch)

            # IMPORTANT: use peer id, not ent.id
            peer_id = await user_client.get_peer_id(ent)

            username = getattr(ent, "username", None)
            name = username if username else str(peer_id)
            chat_id_to_name[peer_id] = name

            # Try to join (if already joined, Telegram will just ignore)
            try:
                await user_client(JoinChannelRequest(ent))
                log.info("User account joined channel: %s", ch)
            except Exception as e:
                log.warning(
                    "User account could not join %s (maybe already joined): %s", ch, e
                )

            scraped_chat_ids.add(peer_id)
            log.info("Will scrape chat %s (peer_id=%s)", name, peer_id)

        except Exception as e:
            log.warning("User account cannot resolve %s: %s", ch, e)


def _session_paths(cfg: AppConfig) -> tuple[Path, Path]:
    """
    Return filesystem paths for user & bot session files,
    retrieved from the config file
    """
    sessions_dir = cfg.telegram.sessions_dir

    sessions_dir.mkdir(parents=True, exist_ok=True)

    user_session = sessions_dir / "user.session"
    bot_session = sessions_dir / "bot.session"
    return user_session, bot_session


async def create_clients():
    global user_client, bot_client

    if user_client is not None and bot_client is not None:
        return

    cfg = get_config()

    user_session_path, bot_session_path = _session_paths(cfg)

    log.info(f"Using session paths: user={user_session_path}, bot={bot_session_path}")

    user_client = TelegramClient(
        str(user_session_path), cfg.telegram.api_id, cfg.telegram.api_hash
    )
    bot_client = TelegramClient(
        str(bot_session_path), cfg.telegram.api_id, cfg.telegram.api_hash
    )

    bot_client.add_event_handler(
        status_command, events.NewMessage(pattern=r"^/status$")
    )
    bot_client.add_event_handler(
        help_command, events.NewMessage(pattern=r"^/(help|start)$")
    )
    bot_client.add_event_handler(
        today_command, events.NewMessage(pattern=r"^/(today|digest)$")
    )
    bot_client.add_event_handler(
        auth_start_command, events.NewMessage(pattern=r"^/auth$")
    )
    bot_client.add_event_handler(auth_dialog_handler, events.NewMessage)

    user_client.add_event_handler(channel_message_handler, events.NewMessage)


async def set_bot_menu_commands(client: TelegramClient) -> None:
    """
    Set the bot's menu commands for easy access in the Telegram UI.
    """
    await client(
        functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="en",
            commands=[
                types.BotCommand(command="status", description="Check system status"),
                types.BotCommand(
                    command="today", description="Request today's summary"
                ),
                types.BotCommand(command="help", description="Get help info"),
                types.BotCommand(command="auth", description="Set authentication"),
            ],
        )
    )


async def start_clients(auth_only: bool = False) -> None:
    """
    Start Telegram clients.
    If auth_only=True: authenticate client (create client session file) and return
    without joining channels / registering the bot and its handlers.
    """
    global user_client, bot_client
    if user_client is None or bot_client is None:
        raise RuntimeError("Clients not initialized — call create_clients() first.")

    cfg = get_config()
    log.info("Starting user & bot clients...")
    log.info("Channels to scrape (user account): %s", ", ".join(cfg.bot.channels))

    # Log in with your phone on first run in CLI mode.
    # This is the supported path for accounts with a cloud password (2FA):
    # Telethon's start() prompts for it via getpass, so the password is read
    # from the terminal and never crosses a chat, a log, or the config file.
    if auth_only:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "--auth needs an interactive terminal: it prompts for the "
                "phone number, login code, and (if enabled) the cloud "
                "password. Run it from a shell on the host, e.g. over ssh, or "
                "with `docker run -it` for containers."
            )
        await user_client.start()
        log.info("Auth-only mode: skipping channel joins and handler registration.")
        return

    # Non-interactive startup
    await user_client.connect()

    global user_auth_state
    if not await user_client.is_user_authorized():
        log.warning("User client not authorized. Use /auth command in the bot")
        user_auth_state = UserAuthState.REQUIRED
    else:
        user_auth_state = UserAuthState.OK
        await user_client.get_me()
        await ensure_joined_and_resolve_channels()

    await bot_client.start(bot_token=cfg.telegram.bot_token)
    await set_bot_menu_commands(bot_client)
    log.info("Bot client started (logged in as bot).")


async def run_clients():
    global user_client, bot_client

    if await user_client.is_user_authorized():
        # Keep the clients running
        await asyncio.gather(
            user_client.run_until_disconnected(), bot_client.run_until_disconnected()
        )
    else:
        await bot_client.run_until_disconnected()


async def disconnect_clients(auth_only: bool = False) -> None:
    """Disconnect both Telegram clients if they were initialized."""
    global user_client, bot_client

    # Telethon's disconnect() is async.
    tasks = []
    if user_client:
        tasks.append(user_client.disconnect())
    if bot_client and not auth_only:
        tasks.append(bot_client.disconnect())

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def get_bot_client() -> TelegramClient:
    if bot_client is None:
        raise RuntimeError("Bot client not initialized — call create_clients() first.")
    return bot_client
