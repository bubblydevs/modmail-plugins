# autohelp/autohelp.py
from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://api.shopjava.uk/help/search"
REQUEST_TIMEOUT_SECONDS = 5
QUIET_PERIOD_SECONDS = 20  # how long the customer must go silent before firing


class AutoHelp(commands.Cog):
    """
    Watches a customer's messages in a new modmail thread. Once they've
    gone quiet for ~20 seconds (or staff replies first, whichever comes
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

    async def cog_unload(self):
        for task in self.timers.values():
            task.cancel()
        if self.session:
            await self.session.close()

    def _reset_timer(self, channel_id: int, thread):
        existing = self.timers.get(channel_id)
        if existing and not existing.done():
            existing.cancel()
        self.timers[channel_id] = self.bot.loop.create_task(
            self._fire_after_quiet(channel_id, thread)
        )

    async def _fire_after_quiet(self, channel_id: int, thread):
        try:
            await asyncio.sleep(QUIET_PERIOD_SECONDS)
        except asyncio.CancelledError:
            return

        if channel_id in self.suggested:
            return

        messages = self.pending.get(channel_id, [])
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
        if not self.enabled:
            return

        channel_id = thread.channel.id
        self.pending[channel_id] = []
        if initial_message and initial_message.content:
            self.pending[channel_id].append(initial_message.content)
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
        if not self.enabled:
            return

        channel_id = thread.channel.id

        if from_mod:
            # Staff replied first — cancel any pending suggestion so it
            # never fires after a human has already taken over.
            self.suggested.add(channel_id)
            existing = self.timers.get(channel_id)
            if existing and not existing.done():
                existing.cancel()
            return

        if channel_id in self.suggested:
            return

        if message.content:
            self.pending.setdefault(channel_id, []).append(message.content)
        self._reset_timer(channel_id, thread)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        channel_id = thread.channel.id
        existing = self.timers.pop(channel_id, None)
        if existing and not existing.done():
            existing.cancel()
        self.pending.pop(channel_id, None)
        self.suggested.discard(channel_id)

    async def _suggest_article(self, thread, messages: list[str]):
        if not messages or not self.session:
            return

        query = "\n\n".join(m.strip() for m in messages if m.strip())
        if not query:
            return

        result = await self._search_help_centre(query)
        if result is None:
            return 

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
            async with self.session.get(
                SEARCH_API_URL,
                params={"q": query, "limit": 1},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Help search API returned %s", resp.status)
                    return None
                data = await resp.json()
                results = data.get("results") or []
                return results[0] if results else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("Help search API request failed")
            return None

    async def _send_customer_reply(self, thread, text: str):
        """
        Relays `text` to the customer through Modmail's real reply
        pipeline. A plain channel.send() would only post an internal
        note — this builds a throwaway message purely to get a valid
        Context, then invokes the real `reply` command directly.
        """
        reply_command = self.bot.get_command("reply")
        if reply_command is None:
            logger.error("Could not find the 'reply' command — is Modmail loaded correctly?")
            return

        placeholder = await thread.channel.send("\u200b")
        try:
            ctx = await self.bot.get_context(placeholder)
            await ctx.invoke(reply_command, msg=text)
        except Exception:
            logger.exception("Failed to send automated customer reply in thread %s", thread.channel.id)
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