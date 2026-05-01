import asyncio
import logging
import os
import random
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from redgifs.aio import API as RedGifsAPI
from telethon import TelegramClient, events


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("redgifs_telethon_bot")


def get_env(name: str, *, required: bool = True, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


class RedGifsService:
    def __init__(self, default_tag: str, result_count: int = 40) -> None:
        self.default_tag = default_tag
        self.result_count = result_count
        self.api: Optional[RedGifsAPI] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self.api is not None:
                return
            self.api = RedGifsAPI()
            await self.api.login()
            LOGGER.info("Logged in to Redgifs temporary API session")

    async def close(self) -> None:
        async with self._lock:
            if self.api is None:
                return
            await self.api.close()
            self.api = None

    async def get_random_video(self, tag: Optional[str] = None) -> tuple[str, str, str]:
        if self.api is None:
            raise RuntimeError("Redgifs API is not initialized")

        query = (tag or self.default_tag).strip()
        selected = await self._pick_from_tag(query)
        if selected is None:
            selected = await self._pick_from_trending()
            query = "trending"

        video_url = selected.urls.hd or selected.urls.sd
        caption = (
            f"Random Redgifs video\n"
            f"Tag: {query}\n"
            f"Creator: @{selected.username}\n"
            f"Link: {selected.urls.web_url}"
        )
        return query, video_url, caption

    async def _pick_from_tag(self, query: str):
        if self.api is None:
            return None

        creators = await self.api.search_creators(tags=[query])
        creator_items = creators.items or []
        if creator_items:
            creator = random.choice(creator_items)
            creator_result = await self.api.search_creator(creator.username, count=self.result_count)
            gifs = [gif for gif in creator_result.gifs if gif.urls.sd]
            if gifs:
                return random.choice(gifs)

        result = await self.api.search(query, count=self.result_count)
        gifs = [gif for gif in (result.gifs or []) if gif.urls.sd]
        if gifs:
            return random.choice(gifs)

        suggestions = await self.api.fetch_tag_suggestions(query)
        for suggestion in suggestions[:5]:
            result = await self.api.search(suggestion["name"], count=self.result_count)
            gifs = [gif for gif in (result.gifs or []) if gif.urls.sd]
            if gifs:
                return random.choice(gifs)

        return None

    async def _pick_from_trending(self):
        if self.api is None:
            raise RuntimeError("Redgifs API is not initialized")

        top_week = await self.api.get_top_this_week(count=20)
        gifs = [gif for gif in (top_week.gifs or []) if gif.urls.sd]
        if gifs:
            return random.choice(gifs)

        trending = await self.api.get_trending_gifs()
        gifs = [gif for gif in trending if gif.urls.sd]
        if not gifs:
            raise RuntimeError("No Redgifs videos were available from trending feeds")
        return random.choice(gifs)


async def download_video(url: str) -> Path:
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
                while True:
                    chunk = await response.content.read(1024 * 256)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                return Path(tmp_file.name)


async def main() -> None:
    load_dotenv()
    
    api_id=19274214
    api_hash="bf87adfbc2c24c66904f3c36f3c0af3a"
    bot_token="7587402234:AAGEJpgD9kNlNG0ry-sHwblAA1JRdYI6R4k"
    default_tag = "amateur",
    session_name = "redgifs_bot"

    redgifs = RedGifsService(default_tag=default_tag)
    await redgifs.start()

    client = TelegramClient(session_name, api_id, api_hash)

    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start_handler(event: events.NewMessage.Event) -> None:
        await event.respond(
            "Send /send to get a random Redgifs video.\n"
            "You can also use /send <tag> to pick a specific tag."
        )

    @client.on(events.NewMessage(pattern=r"^/send(?:@\w+)?(?:\s+(.+))?$"))
    async def send_handler(event: events.NewMessage.Event) -> None:
        tag = event.pattern_match.group(1)
        status = await event.respond("Fetching a random Redgifs video...")
        temp_path: Optional[Path] = None

        try:
            query, video_url, caption = await redgifs.get_random_video(tag)
            LOGGER.info("Sending Redgifs video for tag=%s", query)
            temp_path = await download_video(video_url)
            await event.client.send_file(
                event.chat_id,
                file=str(temp_path),
                caption=caption,
                supports_streaming=True,
            )
            await status.delete()
        except Exception as exc:
            LOGGER.exception("Failed to send Redgifs video")
            await status.edit(f"Could not fetch a Redgifs video: {exc}")
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    try:
        await client.start(bot_token=bot_token)
        LOGGER.info("Telegram bot is running")
        await client.run_until_disconnected()
    finally:
        await redgifs.close()


if __name__ == "__main__":
    asyncio.run(main())
