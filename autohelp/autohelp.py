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
IGNORED_REPLY_AUTHOR_ID = 1345872797754200064  # e.g. an auto-responder/bot — never treat as a staff reply


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

        # Never treat this specific author's replies as a staff reply —
        # they shouldn't cancel the timer or block the suggestion.
        if from_mod and message.author.id == IGNORED_REPLY_AUTHOR_ID:
            from_mod = False

        if from_mod:
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
        if not messages:
            return
        if not self.session:
            logger.warning("[AutoHelp] No aiohttp session available — cog_load may not have run")
            return

        query = "\n\n".join(m.strip() for m in messages if m.strip())
        if not query:
            return

        result = await self._search_help_centre(query)

        if result is None:
            logger.info("[AutoHelp] No confident match returned from search API")
            return

        logger.info(f"[AutoHelp] Match found: {result.get('title')!r} (score={result.get('score')})")

        text = (
            "# :wave: A Staff Member Is On The Way\n"
            "**Thanks for reaching out we'll be with you shortly.**\n"
            "> Our team operates in the **BST/GMT** timezone, so please bear this in mind if you're "
            "messaging us outside of UK working hours replies may take a little longer to arrive.\n"
            "> \n"
            "> While you wait, we took a look through our help centre and found something that might "
            "already answer your question:\n"
            "\n"
            f"**{result['title']}**\n"
            f"{result.get('description', '')}\n"
            f"### :page_facing_up: [Read the full guide here]"
            f"(https://help.shopjava.uk/docs/{result['slug']})\n"
            "\n"
            "> If this doesn't solve it, no worries at all a member of staff will still get to your "
            "ticket as soon as they can."
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
                    body = await resp.text()
                    logger.warning(f"[AutoHelp] Search API returned {resp.status}: {body[:300]}")
                    return None
                data = await resp.json()
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

        placeholder = await thread.channel.send("\u200b")
        try:
            ctx = await self.bot.get_context(placeholder)
            ctx.thread = thread  # ctx.invoke() skips the check that normally sets this
            await ctx.invoke(reply_command, msg=text)
        except Exception:
            logger.exception(f"[AutoHelp] Failed to send automated customer reply in thread {thread.channel.id}")

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