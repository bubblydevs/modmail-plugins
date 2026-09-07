import aiohttp
import discord
from discord.ext import commands
from datetime import datetime, timedelta

from core import checks
from core.models import PermissionLevel

DISCORD_CHECK_URL = "https://api.parcelroblox.com/api/user/check/{}?option=discord"
SESSION_URL = "https://hub.parcelroblox.com/getSession"
ROBLOX_AVATAR_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
ROBLOX_USER_INFO_URL = "https://users.roblox.com/v1/users/{}"
ROBLOX_PROFILE_URL = "https://www.roblox.com/users/{}/profile"
CACHE_TTL = timedelta(hours=6)
FIELD_CHAR_LIMIT = 1000
MAX_PRODUCT_FIELDS = 15


class ParcelWhitelist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def get_hub_auth(self) -> str | None:
        config = await self.db.find_one({"_id": "config"})
        return config["hub_auth"] if config else None

    async def discord_to_roblox(self, discord_id: int) -> str | None:
        url = DISCORD_CHECK_URL.format(discord_id)
        async with self.session.get(url) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            if body.get("status") != "200" or not body["details"].get("verified"):
                return None
            return body["details"]["robloxID"]

    async def get_roblox_avatar(self, roblox_id: str) -> str | None:
        params = {
            "userIds": roblox_id,
            "size": "150x150",
            "format": "Png",
            "isCircular": "false",
        }
        try:
            async with self.session.get(ROBLOX_AVATAR_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        except aiohttp.ClientError:
            return None

        data = body.get("data") or []
        return data[0]["imageUrl"] if data else None

    async def get_roblox_username(self, roblox_id: str) -> str | None:
        """Fetch the actual @username (not the display name) straight from Roblox."""
        url = ROBLOX_USER_INFO_URL.format(roblox_id)
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        except aiohttp.ClientError:
            return None
        return body.get("name")

    async def get_profile_data(self, roblox_id: str) -> list[str] | None:
        """Returns the owned-products list, using cache when possible."""
        cached = await self.db.find_one({"_id": roblox_id})
        if cached and datetime.utcnow() - cached["fetched_at"] < CACHE_TTL:
            return cached["owned"]

        hub_auth = await self.get_hub_auth()
        if not hub_auth:
            return cached["owned"] if cached else None

        headers = {"Authorization": hub_auth}
        params = {"robloxPlayerId": roblox_id}

        try:
            async with self.session.get(SESSION_URL, params=params, headers=headers) as resp:
                if resp.status != 200:
                    return cached["owned"] if cached else None
                body = await resp.json()
        except aiohttp.ClientError:
            return cached["owned"] if cached else None

        if body.get("status") != "200":
            return cached["owned"] if cached else None

        products = body["data"]["productsData"]["allProducts"]
        owned = [p["name"] for p in products if p.get("playerData", {}).get("ownsProduct")]

        await self.db.find_one_and_update(
            {"_id": roblox_id},
            {"$set": {"owned": owned, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )
        return owned

    @staticmethod
    def _chunk_products(names: list[str], limit: int = FIELD_CHAR_LIMIT) -> list[str]:
        """Split product names into evenly-sized groups that each fit under Discord's
        field value limit. Splits by count rather than greedily filling each chunk,
        so a long list doesn't end up as one packed field plus a near-empty leftover."""
        if not names:
            return []

        lines = [f"• {name}\n" for name in names]
        total_len = sum(len(line) for line in lines)
        chunk_count = max(1, -(-total_len // limit))  # ceiling division
        per_chunk = max(1, -(-len(lines) // chunk_count))  # ceiling division

        return [
            "".join(lines[i:i + per_chunk])
            for i in range(0, len(lines), per_chunk)
        ]

    async def build_profile_embed(self, member: discord.abc.User) -> discord.Embed:
        roblox_id = await self.discord_to_roblox(member.id)
        embed = discord.Embed(title="Roblox Profile", color=discord.Color.blurple())

        if not roblox_id:
            embed.description = "No verified Roblox account linked on Parcel for this user."
            return embed

        username = await self.get_roblox_username(roblox_id)
        owned = await self.get_profile_data(roblox_id)
        profile_url = ROBLOX_PROFILE_URL.format(roblox_id)

        embed.add_field(name="Username", value=username or "Unknown", inline=True)
        embed.add_field(name="Roblox ID", value=roblox_id, inline=True)
        embed.add_field(name="Profile", value=f"[View on Roblox]({profile_url})", inline=True)

        avatar_url = await self.get_roblox_avatar(roblox_id)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        if owned is None:
            embed.add_field(
                name="Owned Products",
                value="⚠️ Couldn't reach Parcel to check ownership right now.",
                inline=False,
            )
        elif not owned:
            embed.add_field(name="Owned Products", value="No owned products found.", inline=False)
        else:
            chunks = self._chunk_products(owned)
            shown_names = 0
            for i, chunk in enumerate(chunks[:MAX_PRODUCT_FIELDS]):
                field_name = "Owned Products" if i == 0 else "Owned Products (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)
                shown_names += chunk.count("\n")
            if len(chunks) > MAX_PRODUCT_FIELDS:
                remaining = len(owned) - shown_names
                embed.add_field(name="Note", value=f"...and {remaining} more products not shown.", inline=False)

        return embed

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        embed = await self.build_profile_embed(thread.recipient)
        await thread.channel.send(embed=embed)

    @commands.command(name="parcelauth")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def parcelauth(self, ctx, *, key: str):
        """Set the Parcel hub session key used for ownership checks."""
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"hub_auth": key}},
            upsert=True,
        )
        await ctx.message.delete()
        await ctx.send("Parcel hub key saved.", delete_after=5)

    @commands.command(name="profile")
    async def profile(self, ctx):
        """Show the ticket-opener's owned Parcel products."""
        thread = await self.bot.threads.find(channel=ctx.channel)
        if not thread:
            return await ctx.send("This command can only be used inside a ticket thread.")

        async with ctx.typing():
            embed = await self.build_profile_embed(thread.recipient)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ParcelWhitelist(bot))