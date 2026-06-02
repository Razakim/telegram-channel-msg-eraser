"""
Telegram inline control bot for Koyeb deployments.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon import Button, TelegramClient, events
from telethon.events import CallbackQuery, NewMessage
from telethon.errors import MessageNotModifiedError

from .exceptions import TgEraserException
from .purge import ChannelPurgeEngine, PurgeConfig, PurgeState


Handler = Callable[[CallbackQuery.Event], Awaitable[None]]


@dataclass
class ServiceConfig:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    channel: str
    control_user_ids: set[int]
    state_dir: str = "/tmp/tgeraser"
    batch_size: int = 50
    batch_delay_seconds: int = 300
    daily_limit: int = 1000
    port: int = 8000


class ControlBot:
    """
    Inline-button bot that controls a single channel purge engine.
    """

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        os.makedirs(config.state_dir, exist_ok=True)
        self.bot = TelegramClient(
            os.path.join(config.state_dir, "control_bot"),
            config.api_id,
            config.api_hash,
            sequential_updates=True,
        )
        self.engine = ChannelPurgeEngine(
            PurgeConfig(
                api_id=config.api_id,
                api_hash=config.api_hash,
                session_string=config.session_string,
                channel=config.channel,
                state_dir=config.state_dir,
                batch_size=config.batch_size,
                batch_delay_seconds=config.batch_delay_seconds,
                daily_limit=config.daily_limit,
            )
        )
        self.task: asyncio.Task[PurgeState] | None = None

    async def run(self) -> None:
        # pyrefly: ignore [not-async]
        await self.bot.start(bot_token=self.config.bot_token)
        self._register_handlers()
        health_task = asyncio.create_task(start_health_server(self.config.port))
        try:
            print("Razakim Channel Eraser control bot is running.")
            await self.bot.run_until_disconnected()
        finally:
            health_task.cancel()
            await self.engine.disconnect()
            await self.bot.disconnect()

    def _register_handlers(self) -> None:
        @self.bot.on(events.NewMessage(pattern=r"^/(start|help)$"))
        async def start(event: NewMessage.Event) -> None:
            if not await self._is_allowed(event):
                return
            await event.respond(self._home_text(), buttons=self._home_buttons())

        @self.bot.on(events.CallbackQuery)
        async def callback(event: CallbackQuery.Event) -> None:
            if not await self._is_allowed(event):
                return
            data = event.data.decode("utf-8")
            handlers: dict[str, Handler] = {
                "home": self._show_home,
                "status": self._show_status,
                "settings": self._show_settings,
                "help": self._show_help,
                "dry_run": self._dry_run,
                "start_confirm": self._start_confirm,
                "start_now": self._start_now,
                "pause": self._pause,
                "resume": self._resume,
            }
            handler = handlers.get(data, self._show_home)
            try:
                await handler(event)
            except MessageNotModifiedError:
                await event.answer()

    async def _is_allowed(self, event: NewMessage.Event | CallbackQuery.Event) -> bool:
        sender_id = event.sender_id
        if sender_id in self.config.control_user_ids:
            return True
        text = (
            "Acces refuse. Ajoute ton Telegram user ID dans "
            "TG_CONTROL_USER_IDS sur Koyeb."
        )
        if isinstance(event, CallbackQuery.Event):
            await event.answer(text, alert=True)
        else:
            await event.respond(text)
        return False

    async def _show_home(self, event: CallbackQuery.Event) -> None:
        await event.edit(self._home_text(), buttons=self._home_buttons())

    async def _show_status(self, event: CallbackQuery.Event) -> None:
        state = self.engine.load_state()
        await event.edit(self._status_text(state), buttons=self._status_buttons(state))

    async def _show_settings(self, event: CallbackQuery.Event) -> None:
        await event.edit(self._settings_text(), buttons=self._back_buttons())

    async def _show_help(self, event: CallbackQuery.Event) -> None:
        await event.edit(self._help_text(), buttons=self._back_buttons())

    async def _dry_run(self, event: CallbackQuery.Event) -> None:
        await event.edit("Analyse du canal en cours...", buttons=self._back_buttons())
        try:
            state = await self.engine.dry_run()
            await event.edit(self._status_text(state), buttons=self._status_buttons(state))
        except Exception as error:
            await event.edit(self._error_text(error), buttons=self._back_buttons())

    async def _start_confirm(self, event: CallbackQuery.Event) -> None:
        await event.edit(
            "Confirme la purge du canal.\n\n"
            "Le service supprimera lentement les messages et pourra etre mis en pause.",
            buttons=[
                [Button.inline("Confirmer la purge", b"start_now")],
                [Button.inline("Retour", b"home")],
            ],
        )

    async def _start_now(self, event: CallbackQuery.Event) -> None:
        if self.task and not self.task.done():
            await event.answer("Une purge est deja en cours.", alert=True)
            await self._show_status(event)
            return
        self.task = asyncio.create_task(self.engine.purge())
        self.task.add_done_callback(self._record_task_failure)
        await event.edit(
            "Purge lancee. Je garde le rythme configure et je note la progression.",
            buttons=self._status_buttons(self.engine.load_state()),
        )

    def _record_task_failure(self, task: asyncio.Task[PurgeState]) -> None:
        try:
            task.result()
        except Exception as error:
            state = self.engine.load_state()
            state.status = "error"
            state.last_error = f"{error.__class__.__name__}: {error}"
            self.engine.save_state(state)

    async def _pause(self, event: CallbackQuery.Event) -> None:
        self.engine.request_pause()
        await event.answer("Pause demandee. Elle prendra effet apres le lot courant.")
        await self._show_status(event)

    async def _resume(self, event: CallbackQuery.Event) -> None:
        await self._start_now(event)

    def _home_text(self) -> str:
        return (
            "Razakim Channel Eraser\n\n"
            f"Canal cible: {self.config.channel}\n"
            "Choisis une action. Chaque ecran garde un bouton retour."
        )

    def _home_buttons(self) -> list[list[Button]]:
        return [
            [
                Button.inline("Statut", b"status"),
                Button.inline("Dry-run", b"dry_run"),
            ],
            [
                Button.inline("Lancer", b"start_confirm"),
                Button.inline("Pause", b"pause"),
            ],
            [
                Button.inline("Reprendre", b"resume"),
                Button.inline("Reglages", b"settings"),
            ],
            [Button.inline("Aide", b"help")],
        ]

    def _status_buttons(self, state: PurgeState) -> list[list[Button]]:
        running = state.status in {"running", "sleeping", "flood_wait", "daily_limit"}
        primary = (
            Button.inline("Pause", b"pause")
            if running
            else Button.inline("Reprendre", b"resume")
        )
        return [
            [Button.inline("Actualiser", b"status"), primary],
            [Button.inline("Dry-run", b"dry_run"), Button.inline("Accueil", b"home")],
        ]

    def _back_buttons(self) -> list[list[Button]]:
        return [[Button.inline("Accueil", b"home"), Button.inline("Statut", b"status")]]

    def _status_text(self, state: PurgeState) -> str:
        total = state.dry_run_total if state.dry_run_total is not None else "inconnu"
        lines = [
            "Statut purge",
            "",
            f"Canal: {state.channel_title or self.config.channel}",
            f"Etat: {state.status}",
            f"Messages estimes: {total}",
            f"Scannes: {state.scanned}",
            f"Supprimes: {state.deleted}",
            f"Supprimes aujourd'hui: {state.deleted_today}/{self.config.daily_limit}",
            f"Ignores: {state.skipped}",
            f"Echecs: {state.failed}",
            f"Dernier ID vu: {state.last_seen_id or '-'}",
            f"Derniere maj: {state.updated_at or '-'}",
        ]
        if state.last_error:
            lines.extend(["", f"Derniere erreur: {state.last_error}"])
        return "\n".join(lines)

    def _settings_text(self) -> str:
        return (
            "Reglages actifs\n\n"
            f"Canal: {self.config.channel}\n"
            f"Taille lot: {self.config.batch_size}\n"
            f"Pause entre lots: {self.config.batch_delay_seconds}s\n"
            f"Limite quotidienne: {self.config.daily_limit}\n"
            f"Dossier etat: {self.config.state_dir}\n\n"
            "Change ces valeurs via les variables d'environnement Koyeb."
        )

    def _help_text(self) -> str:
        return (
            "Aide\n\n"
            "Dry-run estime le volume sans supprimer.\n"
            "Lancer demande une confirmation puis supprime par petits lots.\n"
            "Pause arrete proprement apres le lot en cours.\n"
            "Reprendre continue depuis le dernier ID sauvegarde.\n\n"
            "Le compte MTProto doit etre admin du canal avec droit de suppression."
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        return f"Erreur\n\n{error.__class__.__name__}: {error}"


async def start_health_server(port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        body = b"ok\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()


def load_config() -> ServiceConfig:
    missing = [
        name
        for name in (
            "TG_API_ID",
            "TG_API_HASH",
            "TG_BOT_TOKEN",
            "TG_SESSION_STRING",
            "TG_CHANNEL",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise TgEraserException(f"Missing environment variables: {', '.join(missing)}")

    control_ids = {
        int(value.strip())
        for value in os.environ.get("TG_CONTROL_USER_IDS", "").split(",")
        if value.strip()
    }
    if not control_ids:
        raise TgEraserException("TG_CONTROL_USER_IDS must contain at least one user ID.")

    return ServiceConfig(
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        bot_token=os.environ["TG_BOT_TOKEN"],
        session_string=os.environ["TG_SESSION_STRING"],
        channel=os.environ["TG_CHANNEL"],
        control_user_ids=control_ids,
        state_dir=os.environ.get("TG_STATE_DIR", "/tmp/tgeraser"),
        batch_size=int(os.environ.get("TG_BATCH_SIZE", "50")),
        batch_delay_seconds=int(os.environ.get("TG_BATCH_DELAY_SECONDS", "300")),
        daily_limit=int(os.environ.get("TG_DAILY_LIMIT", "1000")),
        port=int(os.environ.get("PORT", "8000")),
    )


async def main() -> None:
    bot = ControlBot(load_config())
    await bot.run()


def entry() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    entry()
