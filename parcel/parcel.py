import uuid
import aiohttp
import discord
from discord.ext import commands
from datetime import datetime, timedelta

from core import checks
from core.models import PermissionLevel

DISCORD_CHECK_URL = "https://api.parcelroblox.com/api/user/check/{}?option=discord"
PRODUCTS_URL = "https://api.parcelroblox.com/api/hub/user/getproducts/{}"
ALL_PRODUCTS_URL = "https://api.parcelroblox.com/api/hub/getproducts"
ROBLOX_AVATAR_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
ROBLOX_USER_INFO_URL = "https://users.roblox.com/v1/users/{}"
ROBLOX_USERNAME_LOOKUP_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_PROFILE_URL = "https://www.roblox.com/users/{}/profile"
TRANSFER_URL = "https://v2.parcelroblox.com/whitelist/transfer"
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

    # ---------------------------------------------------------------- config

    async def get_hub_auth(self) -> str | None:
        config = await self.db.find_one({"_id": "config"})
        return config["hub_auth"] if config else None

    async def get_v2_auth(self) -> str | None:
        config = await self.db.find_one({"_id": "config"})
        return config.get("v2_auth") if config else None

    async def get_transfer_channel_id(self) -> int | None:
        config = await self.db.find_one({"_id": "config"})
        return config.get("transfer_channel_id") if config else None

    # ------------------------------------------------------------- lookups

    async def discord_to_roblox(self, discord_id: int) -> str | None:
        url = DISCORD_CHECK_URL.format(discord_id)
        async with self.session.get(url) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            if body.get("status") != "200" or not body["details"].get("verified"):
                return None
            return body["details"]["robloxID"]

    async def resolve_roblox_id(self, value: str) -> str | None:
        """Accepts either a raw Roblox ID or a username and returns the ID."""
        if value.isdigit():
            return value
        try:
            async with self.session.post(
                ROBLOX_USERNAME_LOOKUP_URL,
                json={"usernames": [value], "excludeBannedUsers": True},
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        except aiohttp.ClientError:
            return None
        data = body.get("data") or []
        return str(data[0]["id"]) if data else None

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

    async def get_all_products(self) -> list[dict] | None:
        """Fetch every product in the hub (not tied to a specific user)."""
        hub_auth = await self.get_hub_auth()
        if not hub_auth:
            return None

        headers = {"hub-secret-key": hub_auth}
        try:
            async with self.session.get(ALL_PRODUCTS_URL, headers=headers) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
        except aiohttp.ClientError:
            return None

        if body.get("status") != "200":
            return None

        return body.get("details", {}).get("products")

    @staticmethod
    def _normalize_owned(owned: list) -> list[dict]:
        """Backwards-compat: older cache entries stored plain name strings."""
        return [
            item if isinstance(item, dict) else {"productID": None, "name": item}
            for item in owned
        ]

    async def get_profile_data(self, roblox_id: str) -> list[dict] | None:
        """Returns owned products as [{"productID": ..., "name": ...}], using cache when possible."""
        cached = await self.db.find_one({"_id": roblox_id})
        if cached and datetime.utcnow() - cached["fetched_at"] < CACHE_TTL:
            return self._normalize_owned(cached["owned"])

        hub_auth = await self.get_hub_auth()
        if not hub_auth:
            return self._normalize_owned(cached["owned"]) if cached else None

        headers = {"hub-secret-key": hub_auth}
        url = PRODUCTS_URL.format(roblox_id)

        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return self._normalize_owned(cached["owned"]) if cached else None
                body = await resp.json()
        except aiohttp.ClientError:
            return self._normalize_owned(cached["owned"]) if cached else None

        if body.get("status") != "200":
            return self._normalize_owned(cached["owned"]) if cached else None

        owned = [
            {"productID": p["productID"], "name": p["name"]}
            for p in body["details"]["ownedProducts"]
        ]

        await self.db.find_one_and_update(
            {"_id": roblox_id},
            {"$set": {"owned": owned, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )
        return owned

    # -------------------------------------------------------------- embeds

    @staticmethod
    def _chunk_products(names: list[str], limit: int = FIELD_CHAR_LIMIT) -> list[str]:
        """Split product names into evenly-sized groups that each fit under Discord's
        field value limit. Splits by count rather than greedily filling each chunk."""
        if not names:
            return []

        lines = [f"• {name}\n" for name in names]
        total_len = sum(len(line) for line in lines)
        chunk_count = max(1, -(-total_len // limit))
        per_chunk = max(1, -(-len(lines) // chunk_count))

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
            names = [p["name"] for p in owned]
            chunks = self._chunk_products(names)
            shown_names = 0
            for i, chunk in enumerate(chunks[:MAX_PRODUCT_FIELDS]):
                field_name = "Owned Products" if i == 0 else "Owned Products (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)
                shown_names += chunk.count("\n")
            if len(chunks) > MAX_PRODUCT_FIELDS:
                remaining = len(names) - shown_names
                embed.add_field(name="Note", value=f"...and {remaining} more products not shown.", inline=False)

        return embed

    # ------------------------------------------------------- transfer layout

    def _build_transfer_layout(self, request: dict, request_id: str, show_buttons: bool = True):
        status_map = {
            "pending": ("🟡", "PENDING"),
            "approved": ("🟢", "APPROVED"),
            "declined": ("🔴", "DECLINED"),
        }
        emoji, label = status_map.get(request["status"], ("⚪", request["status"].upper()))

        product_names = ", ".join(p["name"] for p in request["products"])
        created_ts = int(request["created_at"].timestamp())

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(content="### Transfer Request"))
        container.add_item(discord.ui.TextDisplay(content=f"**Product Name**\n{product_names}"))
        container.add_item(discord.ui.TextDisplay(
            content=(
                f"**From**\n{request['sender_username'] or 'Unknown'} ({request['sender_roblox_id']})\n\n"
                f"**To**\n{request['recipient_username'] or 'Unknown'} ({request['recipient_roblox_id']})"
            )
        ))
        container.add_item(discord.ui.TextDisplay(
            content=f"**Support Ticket**\n<#{request['ticket_channel_id']}>"
        ))
        container.add_item(discord.ui.TextDisplay(content=f"**Status**\n{emoji} {label}"))

        if request.get("failed_products"):
            failed = ", ".join(request["failed_products"])
            container.add_item(discord.ui.TextDisplay(content=f"**Failed to transfer**\n{failed}"))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            content=f"Requested by {request['requested_by_name']} • <t:{created_ts}:f>"
        ))

        if show_buttons and request["status"] == "pending":
            container.add_item(discord.ui.ActionRow(
                discord.ui.Button(
                    label="Approve",
                    style=discord.ButtonStyle.success,
                    custom_id=f"parceltransfer:approve:{request_id}",
                ),
                discord.ui.Button(
                    label="Decline",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"parceltransfer:decline:{request_id}",
                ),
            ))

        view.add_item(container)
        return view

    # ----------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        embed = await self.build_profile_embed(thread.recipient)
        await thread.channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("parceltransfer:"):
            return

        _, action, request_id = custom_id.split(":", 2)
        doc = await self.db.find_one({"_id": f"transfer:{request_id}"})

        if not doc:
            return await interaction.response.send_message("This request no longer exists.", ephemeral=True)
        if doc["status"] != "pending":
            return await interaction.response.send_message("This request has already been handled.", ephemeral=True)

        await interaction.response.defer()

        ticket_channel = self.bot.get_channel(doc["ticket_channel_id"])
        product_names = ", ".join(p["name"] for p in doc["products"])

        if action == "approve":
            v2_auth = await self.get_v2_auth()
            if not v2_auth:
                return await interaction.followup.send(
                    "No v2 API key set — an owner needs to run `?parcelv2auth` first.", ephemeral=True
                )

            headers = {"Authorization": v2_auth, "Content-Type": "application/json"}
            failed = []
            for product in doc["products"]:
                body = {
                    "product_id": product["productID"],
                    "sender": {"userid": doc["sender_roblox_id"], "userid_type": "roblox"},
                    "recipient": {"userid": doc["recipient_roblox_id"], "userid_type": "roblox"},
                }
                try:
                    async with self.session.patch(TRANSFER_URL, json=body, headers=headers) as resp:
                        result = await resp.json()
                        if resp.status != 200 or result.get("status") != "200":
                            failed.append(product["name"])
                except aiohttp.ClientError:
                    failed.append(product["name"])

            await self.db.find_one_and_update(
                {"_id": doc["_id"]},
                {"$set": {"status": "approved", "failed_products": failed}},
            )
            doc["status"] = "approved"
            doc["failed_products"] = failed

            if ticket_channel:
                await ticket_channel.send(
                    "✅ **Transfer Request Approved**\n"
                    "> **Good news — it's been approved and processed.**\n"
                    f"> **Products transferred:** {product_names}\n"
                    "> \n"
                    "> If you have any questions about this, feel free to ask your support agent in this ticket."
                )

            note = f" ({len(failed)} product(s) failed — check the request card)" if failed else ""
            await interaction.followup.send(f"Approved and processed.{note}", ephemeral=True)

        else:
            await self.db.find_one_and_update({"_id": doc["_id"]}, {"$set": {"status": "declined"}})
            doc["status"] = "declined"

            if ticket_channel:
                await ticket_channel.send(
                    "❌ **Transfer Request Declined**\n"
                    "> **Unfortunately, this request wasn't approved.**\n"
                    f"> **Products requested:** {product_names}\n"
                    "> \n"
                    "> If you'd like to know more, feel free to ask your support agent in this ticket."
                )

            await interaction.followup.send("Declined.", ephemeral=True)

        updated_layout = self._build_transfer_layout(doc, request_id, show_buttons=False)
        await interaction.message.edit(view=updated_layout)

    # -------------------------------------------------------------- commands

    @commands.command(name="parcelauth")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def parcelauth(self, ctx, *, key: str):
        """Set the Parcel hub secret key used for ownership checks."""
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"hub_auth": key}},
            upsert=True,
        )
        await ctx.message.delete()
        await ctx.send("Parcel hub key saved.", delete_after=5)

    @commands.command(name="parcelv2auth")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def parcelv2auth(self, ctx, *, key: str):
        """Set the Parcel v2 API key used for approving transfers."""
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"v2_auth": key}},
            upsert=True,
        )
        await ctx.message.delete()
        await ctx.send("Parcel v2 API key saved.", delete_after=5)

    @commands.command(name="parceltransferchannel")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def parceltransferchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where transfer requests get posted for approval."""
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"transfer_channel_id": channel.id}},
            upsert=True,
        )
        await ctx.send(f"Transfer requests will now be posted in {channel.mention}.")

    @commands.command(name="profile")
    async def profile(self, ctx):
        """Show the ticket-opener's owned Parcel products."""
        thread = await self.bot.threads.find(channel=ctx.channel)
        if not thread:
            return await ctx.send("This command can only be used inside a ticket thread.")

        async with ctx.typing():
            embed = await self.build_profile_embed(thread.recipient)

        await ctx.send(embed=embed)

    @commands.command(name="transfer")
    async def transfer(self, ctx, new_user: str, *, products: str):
        """
        Request a product transfer for the ticket-opener to another Roblox account.
        Usage: ?transfer <new roblox username or id> <product name, product name, ...|all>
        Must be used inside a ticket thread.
        """
        thread = await self.bot.threads.find(channel=ctx.channel)
        if not thread:
            return await ctx.send("This command can only be used inside a ticket thread.")

        async with ctx.typing():
            sender_roblox_id = await self.discord_to_roblox(thread.recipient.id)
            if not sender_roblox_id:
                return await ctx.send("This user doesn't have a verified Roblox account linked on Parcel.")

            recipient_roblox_id = await self.resolve_roblox_id(new_user)
            if not recipient_roblox_id:
                return await ctx.send(f"Couldn't find a Roblox account for `{new_user}`.")

            owned = await self.get_profile_data(sender_roblox_id)
            if not owned:
                return await ctx.send("This user doesn't own any products to transfer.")

            missing: list[str] = []
            if products.strip().lower() == "all":
                matched = owned
            else:
                requested_names = [p.strip() for p in products.split(",") if p.strip()]
                owned_by_lower = {p["name"].lower(): p for p in owned}
                matched = []
                for name in requested_names:
                    found = owned_by_lower.get(name.lower())
                    if found:
                        matched.append(found)
                    else:
                        missing.append(name)

            if not matched:
                return await ctx.send("None of the requested products were found in this user's owned products.")

            transfer_channel_id = await self.get_transfer_channel_id()
            if not transfer_channel_id:
                return await ctx.send(
                    "No transfer request channel set — an owner needs to run `?parceltransferchannel` first."
                )

            transfer_channel = self.bot.get_channel(transfer_channel_id)
            if not transfer_channel:
                return await ctx.send("The configured transfer channel couldn't be found — it may have been deleted.")

            sender_username = await self.get_roblox_username(sender_roblox_id)
            recipient_username = await self.get_roblox_username(recipient_roblox_id)

            request_id = uuid.uuid4().hex
            request = {
                "_id": f"transfer:{request_id}",
                "ticket_channel_id": ctx.channel.id,
                "sender_roblox_id": sender_roblox_id,
                "sender_username": sender_username,
                "recipient_roblox_id": recipient_roblox_id,
                "recipient_username": recipient_username,
                "products": matched,
                "requested_by_name": str(ctx.author),
                "status": "pending",
                "created_at": datetime.utcnow(),
            }
            await self.db.insert_one(request)

            layout = self._build_transfer_layout(request, request_id)
            await transfer_channel.send(view=layout)

            product_names = ", ".join(p["name"] for p in matched)
            note = f"\n\n(Note: couldn't match: {', '.join(missing)})" if missing else ""

        await ctx.send(
            ":hourglass: **Transfer Request Received**\n"
            "> **You're all sorted on your end, it's on us now.**\n"
            f"> **Products requested to transfer:** {product_names}\n"
            "> \n"
            "> Your support agent has sent this request through to management, so there's nothing more you "
            "need to do we'll get it sorted as soon as we can, could be a few mins, could take a couple days "
            "depending on how busy things are.\n"
            "> \n"
            "> No need to keep messaging the ticket to try speed it up, it won't help and can actually knock "
            f"you down the queue.{note}"
        )


async def setup(bot):
    await bot.add_cog(ParcelWhitelist(bot))