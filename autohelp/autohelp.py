# autohelp/autohelp.py
from __future__ import annotations

import asyncio
from logging import getLogger

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

logger = getLogger(__name__)

SEARCH_API_URL = "https://api.shopjava.uk/help/search"
REQUEST_TIMEOUT_SECONDS = 5
QUIET_PERIOD_SECONDS = 10  # how long the customer must go silent before firing


class AutoHelp(commands.Cog):
    """
    Watches a customer's messages in a new modmail thread. Once they've
    gone quiet for ~10 seconds (or staff replies first, whichever comes
    first), searches the help centre and — if there's a confident match —
    replies to them directly with a suggested article and a note that
    the team will be with them shortly.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True
        self.pending: dict[int, list[str]] = {}
        self.timers: dict[int, asyncio.Task] = {}
        self.suggested: set[int] = set()
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        logger.info("[AutoHelp] Cog loaded, session created.")

    async def cog_unload(self):
        for task in self.timers.values():
            task.cancel()
        if self.session:
            await self.session.close()
        logger.info("[AutoHelp] Cog unloaded, session closed.")

    def _reset_timer(self, channel_id: int, thread):
        existing = self.timers.get(channel_id)
        if existing and not existing.done():
            existing.cancel()
            logger.debug(f"[AutoHelp] Cancelled existing timer for channel {channel_id}")
        self.timers[channel_id] = self.bot.loop.create_task(
            self._fire_after_quiet(channel_id, thread)
        )
        logger.debug(f"[AutoHelp] Started {QUIET_PERIOD_SECONDS}s timer for channel {channel_id}")

    async def _fire_after_quiet(self, channel_id: int, thread):
        try:
            await asyncio.sleep(QUIET_PERIOD_SECONDS)
        except asyncio.CancelledError:
            logger.debug(f"[AutoHelp] Timer cancelled for channel {channel_id} (new message or staff reply)")
            return

        if channel_id in self.suggested:
            logger.debug(f"[AutoHelp] Channel {channel_id} already suggested, skipping")
            return

        messages = self.pending.get(channel_id, [])
        logger.debug(f"[AutoHelp] Quiet period elapsed for channel {channel_id}, {len(messages)} messages collected")
        self.suggested.add(channel_id)
        await self._suggest_article(thread, messages)

    @commands.Cog.listener()
    async def on_thread_ready(
        self,
        thread,
        creator: discord.Member,
        category,
        initial_message: discord.Message,
    ):
        logger.info(f"[AutoHelp] on_thread_ready fired for channel {thread.channel.id}, enabled={self.enabled}")
        if not self.enabled:
            return

        channel_id = thread.channel.id
        self.pending[channel_id] = []
        if initial_message and initial_message.content:
            self.pending[channel_id].append(initial_message.content)
            logger.info(f"[AutoHelp] Captured initial message for channel {channel_id}: {initial_message.content[:80]!r}")
        self._reset_timer(channel_id, thread)

    @commands.Cog.listener()
    async def on_thread_reply(
        self,
        thread,
        from_mod: bool,
        message: discord.Message,
        anonymous: bool,
        plain: bool,
    ):
        logger.info(f"[AutoHelp] on_thread_reply fired — channel={thread.channel.id}, from_mod={from_mod}, enabled={self.enabled}")
        if not self.enabled:
            return

        channel_id = thread.channel.id

        if from_mod:
            logger.debug(f"[AutoHelp] Staff replied first in channel {channel_id}, cancelling any pending suggestion")
            self.suggested.add(channel_id)
            existing = self.timers.get(channel_id)
            if existing and not existing.done():
                existing.cancel()
            return

        if channel_id in self.suggested:
            logger.debug(f"[AutoHelp] Channel {channel_id} already suggested, ignoring further messages")
            return

        if message.content:
            self.pending.setdefault(channel_id, []).append(message.content)
            logger.debug(f"[AutoHelp] Appended message for channel {channel_id}: {message.content[:80]!r}")
        self._reset_timer(channel_id, thread)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        channel_id = thread.channel.id
        existing = self.timers.pop(channel_id, None)
        if existing and not existing.done():
            existing.cancel()
        self.pending.pop(channel_id, None)
        self.suggested.discard(channel_id)
        logger.debug(f"[AutoHelp] Cleaned up state for closed thread {channel_id}")

    async def _suggest_article(self, thread, messages: list[str]):
        if not messages:
            logger.debug("[AutoHelp] No messages collected, skipping search")
            return
        if not self.session:
            logger.warning("[AutoHelp] No aiohttp session available — cog_load may not have run")
            return

        query = "\n\n".join(m.strip() for m in messages if m.strip())
        if not query:
            logger.debug("[AutoHelp] Joined query was empty after stripping, skipping search")
            return

        logger.debug(f"[AutoHelp] Querying search API with: {query[:200]!r}")
        result = await self._search_help_centre(query)

        if result is None:
            logger.info("[AutoHelp] No confident match returned from search API")
            return

        logger.info(f"[AutoHelp] Match found: {result.get('title')!r} (score={result.get('score')})")

        text = (
            f"While you wait, this might help: **{result['title']}**\n"
            f"{result.get('description', '')}\n"
            f"<https://help.shopjava.uk/docs/{result['slug']}>\n\n"
            f"A member of the team will get back to you shortly!"
        )
        await self._send_customer_reply(thread, text)

    async def _search_help_centre(self, query: str) -> dict | None:
        if self.session is None:
            return None
        try:
            logger.debug(f"[AutoHelp] GET {SEARCH_API_URL} with q={query[:50]!r}")
            async with self.session.get(
                SEARCH_API_URL,
                params={"q": query, "limit": 1},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                logger.debug(f"[AutoHelp] Search API responded with status {resp.status}")
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[AutoHelp] Search API returned {resp.status}: {body[:300]}")
                    return None
                data = await resp.json()
                logger.debug(f"[AutoHelp] Search API response body: {data}")
                results = data.get("results") or []
                return results[0] if results else None
        except asyncio.TimeoutError:
            logger.error(f"[AutoHelp] Search API request timed out after {REQUEST_TIMEOUT_SECONDS}s")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"[AutoHelp] Search API request failed: {e}")
            return None

    async def _send_customer_reply(self, thread, text: str):
        reply_command = self.bot.get_command("reply")
        if reply_command is None:
            logger.error("[AutoHelp] Could not find the 'reply' command — is Modmail loaded correctly?")
            return

        logger.debug(f"[AutoHelp] Sending customer reply via ctx.invoke: {text[:100]!r}")
        placeholder = await thread.channel.send("\u200b")
        try:
            ctx = await self.bot.get_context(placeholder)
            await ctx.invoke(reply_command, msg=text)
            logger.info(f"[AutoHelp] Successfully sent reply to channel {thread.channel.id}")
        except Exception:
            logger.exception(f"[AutoHelp] Failed to send automated customer reply in thread {thread.channel.id}")
        finally:
            try:
                await placeholder.delete()
            except discord.HTTPException:
                pass

    @commands.command(name="autohelp")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def autohelp_toggle(self, ctx: commands.Context, state: str = None):
        """
        Enable or disable automatic help-article suggestions.
        Usage: ?autohelp enable | ?autohelp disable | ?autohelp
        """
        if state is None:
            status = "enabled" if self.enabled else "disabled"
            await ctx.send(f"AutoHelp is currently **{status}**. Use `?autohelp enable` or `?autohelp disable` to change it.")
            return

        state = state.lower()
        if state in ("enable", "on", "true"):
            self.enabled = True
            await ctx.send("✅ AutoHelp is now **enabled**.")
        elif state in ("disable", "off", "false"):
            self.enabled = False
            await ctx.send("🚫 AutoHelp is now **disabled**.")
        else:
            await ctx.send("Usage: `?autohelp enable` or `?autohelp disable`")

    @commands.command(name="autohelptest")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def autohelp_test(self, ctx: commands.Context, *, message: str):
        """
        Manually run a message through the search API right now, bypassing
        the 10-second wait — for debugging whether the API/search itself
        is working, separate from the timing/trigger logic.
        Usage: ?autohelptest my vehicle wont spawn in studio
        """
        await ctx.send(f"Searching for: `{message}`...")
        result = await self._search_help_centre(message)
        if result is None:
            await ctx.send("No confident match found (or the API request failed — check bot logs).")
        else:
            await ctx.send(
                f"Match: **{result['title']}** (score: {result.get('score')})\n"
                f"{result.get('description', '')}\n"
                f"<https://help.shopjava.uk/docs/{result['slug']}>"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoHelp(bot))