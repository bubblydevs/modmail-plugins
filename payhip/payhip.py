import aiohttp
import discord
from discord.ext import commands
from datetime import datetime

from core import checks
from core.models import PermissionLevel

DISCORD_CHECK_URL = "https://api.parcelroblox.com/api/user/check/{}?option=discord"
PAYHIP_URL = "https://api.shopjava.uk/billing/payhip/check"
FIELD_CHAR_LIMIT = 1000
MAX_FIELDS = 15


class PayhipCheck(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ---------------------------------------------------------------- config

    async def get_api_key(self) -> str | None:
        config = await self.db.find_one({"_id": "config"})
        return config.get("api_key") if config else None

    # ------------------------------------------------------------- lookups

    async def discord_to_roblox(self, discord_id: int) -> str | None:
        """Same verified Discord->Roblox check used by the Parcel plugin,
        duplicated here so this plugin doesn't depend on that one being loaded."""
        url = DISCORD_CHECK_URL.format(discord_id)
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        except aiohttp.ClientError:
            return None
        if body.get("status") != "200" or not body["details"].get("verified"):
            return None
        return body["details"]["robloxID"]

    async def payhip_lookup(self, **params) -> dict | None:
        """Pass exactly one of orderId / discordUsername / robloxUsername / robloxId."""
        api_key = await self.get_api_key()
        if not api_key:
            return None

        headers = {"x-api-key": api_key}
        try:
            async with self.session.get(PAYHIP_URL, params=params, headers=headers) as resp:
                if resp.status in (401, 400, 404):
                    return None
                if resp.status != 200:
                    return None
                return await resp.json()
        except aiohttp.ClientError:
            return None

    async def resolve_payhip_profile(self, member: discord.abc.User) -> dict | None:
        """Sends both the Roblox ID (if resolvable) and Discord username in one call —
        the API tries robloxId first, then discordUsername, server-side."""
        roblox_id = await self.discord_to_roblox(member.id)

        params = {"discordUsername": member.name}
        if roblox_id:
            params["robloxId"] = roblox_id

        return await self.payhip_lookup(**params)

    # ---------------------------------------------------------------- embeds

    @staticmethod
    def _chunk_lines(lines: list[str], limit: int = FIELD_CHAR_LIMIT) -> list[str]:
        """Split lines into evenly-sized groups that each fit under Discord's field limit."""
        if not lines:
            return []
        blocks = [line + "\n" for line in lines]
        total_len = sum(len(b) for b in blocks)
        chunk_count = max(1, -(-total_len // limit))
        per_chunk = max(1, -(-len(blocks) // chunk_count))
        return ["".join(blocks[i:i + per_chunk]) for i in range(0, len(blocks), per_chunk)]

    @staticmethod
    def _product_lines(products: list[dict]) -> list[str]:
        lines = []
        for p in products:
            status = "✅" if p.get("fulfilled") else "⏳"
            qty = f" x{p['quantity']}" if p.get("quantity", 1) and p.get("quantity", 1) > 1 else ""
            lines.append(f"{status} {p.get('productName', 'Unknown')}{qty}")
        return lines

    async def build_profile_embed(self, member: discord.abc.User) -> discord.Embed:
        profile = await self.resolve_payhip_profile(member)
        return self._build_profile_embed_from_data(profile)

    def _build_profile_embed_from_data(self, profile: dict | None) -> discord.Embed:
        embed = discord.Embed(title="Payhip Purchase History", color=discord.Color.blurple())

        if not profile:
            embed.description = "No Payhip purchase history found for this user."
            return embed

        embed.add_field(name="Discord Username", value=profile.get("discordUsername") or "Unknown", inline=True)
        embed.add_field(name="Roblox Username", value=profile.get("robloxUsername") or "Unknown", inline=True)
        embed.add_field(name="Roblox ID", value=profile.get("robloxId") or "Unknown", inline=True)

        products = profile.get("products") or []
        if products:
            lines = self._product_lines(products)
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks[:MAX_FIELDS]):
                name = "Products" if i == 0 else "Products (cont.)"
                embed.add_field(name=name, value=chunk, inline=False)
        else:
            embed.add_field(name="Products", value="No products found.", inline=False)

        orders = profile.get("orders") or []
        matched_labels = {
            "robloxId": "Roblox ID",
            "robloxUsername": "Roblox username",
            "discordUsername": "Discord username",
        }
        method = profile.get("matchedBy", "unknown")
        embed.set_footer(text=f"{len(orders)} order(s) on file • matched via {matched_labels.get(method, method)}")

        return embed

    async def build_order_embed(self, order: dict) -> discord.Embed:
        embed = discord.Embed(title="Payhip Order", color=discord.Color.blurple())

        embed.add_field(name="Order ID", value=order.get("payhipOrderId", "Unknown"), inline=True)
        embed.add_field(name="Email", value=order.get("email", "Unknown"), inline=True)
        embed.add_field(name="Status", value=order.get("status", "Unknown"), inline=True)

        price = order.get("price")
        currency = order.get("currency", "")
        if price is not None:
            embed.add_field(name="Price", value=f"{price / 100:.2f} {currency}".strip(), inline=True)
        embed.add_field(name="Payment Type", value=order.get("paymentType", "Unknown"), inline=True)
        embed.add_field(name="Gift", value="Yes" if order.get("isGift") else "No", inline=True)

        purchased_at = order.get("purchasedAt")
        if purchased_at:
            try:
                dt = datetime.fromisoformat(purchased_at.replace("Z", "+00:00"))
                embed.add_field(name="Purchased", value=f"<t:{int(dt.timestamp())}:f>", inline=True)
            except ValueError:
                pass

        embed.add_field(name="Discord Username", value=order.get("discordUsername") or "Unknown", inline=True)
        embed.add_field(name="Roblox Username", value=order.get("robloxUsername") or "Unknown", inline=True)
        embed.add_field(name="Roblox ID", value=order.get("robloxId") or "Unknown", inline=True)

        items = order.get("items") or []
        if items:
            lines = self._product_lines(items)
            embed.add_field(name="Items", value="\n".join(lines), inline=False)

        return embed

    # ----------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        embed = await self.build_profile_embed(thread.recipient)
        await thread.channel.send(embed=embed)

    # -------------------------------------------------------------- commands

    @commands.command(name="payhipauth")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def payhipauth(self, ctx, *, key: str):
        """Set the Payhip check API key."""
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"api_key": key}},
            upsert=True,
        )
        await ctx.message.delete()
        await ctx.send("Payhip API key saved.", delete_after=5)

    @commands.command(name="payhip")
    async def payhip(self, ctx, *, query: str = None):
        """
        ?payhip            -> looks up the ticket-opener (Roblox ID first, then Discord username)
        ?payhip <orderId>  -> looks up one specific order by its Payhip order ID
        ?payhip <query>    -> if not an order ID, tries it as a Roblox ID / Roblox username /
                              Discord username instead (in that priority order). Email lookups
                              are not supported by the API.
        """
        thread = await self.bot.threads.find(channel=ctx.channel)
        if not thread:
            return await ctx.send("This command can only be used inside a ticket thread.")

        async with ctx.typing():
            if query:
                query = query.strip()
                order_data = await self.payhip_lookup(orderId=query)
                if order_data:
                    embed = await self.build_order_embed(order_data["order"])
                else:
                    profile = await self.payhip_lookup(
                        robloxId=query, robloxUsername=query, discordUsername=query
                    )
                    if not profile:
                        return await ctx.send(f"No order or profile found for `{query}`.")
                    embed = self._build_profile_embed_from_data(profile)
            else:
                embed = await self.build_profile_embed(thread.recipient)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PayhipCheck(bot))