"""
Slow, resumable channel purge engine.
"""

import asyncio
import json
import os
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Message
from telethon.utils import get_display_name

from .__version__ import VERSION
from .exceptions import TgEraserException


ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class PurgeConfig:
    api_id: int
    api_hash: str
    session_string: str
    channel: str
    state_dir: str
    batch_size: int = 50
    batch_delay_seconds: int = 300
    delay_jitter: float = 0.15
    daily_limit: int = 1000
    iter_wait_seconds: float = 1.0


@dataclass
class PurgeState:
    status: str = "idle"
    channel: str = ""
    channel_title: str = ""
    last_seen_id: int = 0
    scanned: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    deleted_today: int = 0
    day: str = ""
    dry_run_total: int | None = None
    started_at: str | None = None
    updated_at: str | None = None
    last_error: str | None = None


class ChannelPurgeEngine:
    """
    Deletes channel history slowly with checkpointing and explicit pause support.
    """

    def __init__(
        self,
        config: PurgeConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.progress_callback = progress_callback
        self._client: TelegramClient | None = None
        self._lock = asyncio.Lock()
        self._pause_requested = False

    @property
    def state_path(self) -> str:
        channel_key = "".join(
            char if char.isalnum() or char in ("-", "_") else "_"
            for char in self.config.channel
        )
        return os.path.join(self.config.state_dir, f"purge_{channel_key}.json")

    async def connect(self) -> None:
        if self._client and self._client.is_connected():
            return

        self._client = TelegramClient(
            StringSession(self.config.session_string),
            self.config.api_id,
            self.config.api_hash,
            app_version=VERSION,
            flood_sleep_threshold=24 * 60 * 60,
            sequential_updates=True,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise TgEraserException(
                "TG_SESSION_STRING is invalid or expired. Generate a new one."
            )

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()

    def load_state(self) -> PurgeState:
        os.makedirs(self.config.state_dir, exist_ok=True)
        if not os.path.exists(self.state_path):
            return PurgeState(channel=self.config.channel, day=date.today().isoformat())
        with open(self.state_path, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
        return PurgeState(**{**asdict(PurgeState()), **data})

    def save_state(self, state: PurgeState) -> None:
        os.makedirs(self.config.state_dir, exist_ok=True)
        state.updated_at = self._now()
        temp_path = self.state_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as state_file:
            json.dump(asdict(state), state_file, indent=2, sort_keys=True)
        os.replace(temp_path, self.state_path)
        if self.progress_callback:
            self.progress_callback(asdict(state))

    async def dry_run(self) -> PurgeState:
        async with self._lock:
            await self.connect()
            client = self._require_client()
            entity = await self._get_channel()
            state = self.load_state()
            state.channel = self.config.channel
            state.channel_title = get_display_name(entity)
            state.status = "dry_run"
            state.last_error = None
            self.save_state(state)

            messages = await client.get_messages(entity, limit=0)
            state.dry_run_total = getattr(messages, "total", None)
            state.status = "idle"
            self.save_state(state)
            return state

    def request_pause(self) -> None:
        self._pause_requested = True
        state = self.load_state()
        if state.status in {"running", "sleeping", "daily_limit"}:
            state.status = "pausing"
            self.save_state(state)

    async def purge(self) -> PurgeState:
        async with self._lock:
            self._pause_requested = False
            await self.connect()
            entity = await self._get_channel()
            state = self.load_state()
            state.channel = self.config.channel
            state.channel_title = get_display_name(entity)
            if state.status == "complete":
                state.last_seen_id = 0
                state.scanned = 0
                state.deleted = 0
                state.skipped = 0
                state.failed = 0
                state.dry_run_total = None
                state.started_at = self._now()
            state.status = "running"
            state.started_at = state.started_at or self._now()
            state.last_error = None
            self._roll_daily_counter(state)
            self.save_state(state)

            while True:
                self._roll_daily_counter(state)

                if self._pause_requested:
                    state.status = "paused"
                    self.save_state(state)
                    return state

                if self.config.daily_limit > 0 and state.deleted_today >= self.config.daily_limit:
                    state.status = "daily_limit"
                    self.save_state(state)
                    await self._sleep_interruptibly(60)
                    continue

                messages = await self._read_next_messages(entity, state.last_seen_id)
                if not messages:
                    state.status = "complete"
                    self.save_state(state)
                    return state

                messages = self._limit_messages_for_today(messages, state)
                state.scanned += len(messages)
                state.last_seen_id = min(message.id for message in messages)
                deletable_ids = [message.id for message in messages if message.action is None]
                state.skipped += len(messages) - len(deletable_ids)

                if deletable_ids:
                    remaining_today = self._remaining_today(state)
                    if remaining_today is not None:
                        deletable_ids = deletable_ids[:remaining_today]
                    await self._delete_batch(entity, deletable_ids, state)

                self.save_state(state)

                if self._pause_requested:
                    state.status = "paused"
                    self.save_state(state)
                    return state

                if state.status == "running":
                    state.status = "sleeping"
                    self.save_state(state)
                    await self._sleep_interruptibly(self._next_delay())
                    state.status = "running"
                    self.save_state(state)

    async def _read_next_messages(
        self, entity: Channel, offset_id: int
    ) -> list[Message]:
        client = self._require_client()
        messages: list[Message] = []
        async for message in client.iter_messages(
            entity,
            limit=self.config.batch_size,
            offset_id=offset_id,
            wait_time=self.config.iter_wait_seconds,
        ):
            messages.append(message)
        return messages

    async def _delete_batch(
        self, entity: Channel, message_ids: list[int], state: PurgeState
    ) -> None:
        client = self._require_client()
        if not message_ids:
            return

        try:
            await client.delete_messages(entity, message_ids, revoke=True)
            state.deleted += len(message_ids)
            state.deleted_today += len(message_ids)
            state.last_error = None
        except FloodWaitError as error:
            state.status = "flood_wait"
            state.last_error = f"Telegram asked to wait {error.seconds}s"
            self.save_state(state)
            await self._sleep_interruptibly(error.seconds + 30)
            state.status = "running"
        except RPCError as error:
            state.failed += len(message_ids)
            state.last_error = f"{error.__class__.__name__}: {error}"

    async def _get_channel(self) -> Channel:
        client = self._require_client()
        target = self.config.channel
        try:
            target = int(target)
        except ValueError:
            pass

        try:
            entity = await client.get_entity(target)
        except ValueError:
            if isinstance(target, int) and target > 0:
                try:
                    entity = await client.get_entity(int(f"-100{target}"))
                except ValueError:
                    raise
            else:
                raise

        if not isinstance(entity, Channel) or entity.megagroup:
            raise TgEraserException("TG_CHANNEL must target a channel, not a group.")
        return entity

    def _require_client(self) -> TelegramClient:
        if self._client is None:
            raise TgEraserException("Telegram client is not connected.")
        return self._client

    def _roll_daily_counter(self, state: PurgeState) -> None:
        today = date.today().isoformat()
        if state.day != today:
            state.day = today
            state.deleted_today = 0

    def _remaining_today(self, state: PurgeState) -> int | None:
        if self.config.daily_limit <= 0:
            return None
        return max(0, self.config.daily_limit - state.deleted_today)

    def _limit_messages_for_today(
        self, messages: list[Message], state: PurgeState
    ) -> list[Message]:
        remaining_today = self._remaining_today(state)
        if remaining_today is None:
            return messages

        deletable_seen = 0
        limited: list[Message] = []
        for message in messages:
            limited.append(message)
            if message.action is None:
                deletable_seen += 1
            if deletable_seen >= remaining_today:
                break
        return limited

    def _next_delay(self) -> float:
        if self.config.batch_delay_seconds <= 0:
            return 0
        spread = self.config.batch_delay_seconds * self.config.delay_jitter
        return max(0, random.uniform(
            self.config.batch_delay_seconds - spread,
            self.config.batch_delay_seconds + spread,
        ))

    async def _sleep_interruptibly(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            if self._pause_requested:
                return
            step = min(remaining, 5)
            await asyncio.sleep(step)
            remaining -= step

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
