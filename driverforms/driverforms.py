"""
Stagecoach East Midlands - Driver Forms
Modmail plugin: posts a Driver Forms panel with a select menu (Shift Log,
Driver Incident Log, LOA Request). Each form opens a modal, posts a branded
embed in the right channel and writes the data into the Staff Database
through the Google Apps Script web app.

Commands (Administrator):
  ?df setup <web app url> <secret>   connect to the staff database (message is deleted)
  ?df channel <shift|incident|loa> #channel
  ?df role <incident|approver> @role
  ?df panel #channel                 post the panel
  ?df config                         show current settings
  ?df test                           check the bot can reach the sheet
"""

import os
import re
import json
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

TZ = ZoneInfo("Europe/London")
NAVY = 0x15223A
AMBER = 0xF9AE00
GREEN = 0x009779
RED = 0xC0392B
HERE = os.path.dirname(os.path.abspath(__file__))
BANNERS = {
    "panel": "banner-driver-forms.png",
    "shift": "banner-shift-log.png",
    "incident": "banner-incident-report.png",
    "loa": "banner-loa-request.png",
}
PIN_RE = re.compile(r"\[(\d{4})\]")
FOOTER = "Stagecoach East Midlands • Driver Forms"

ERRORS = {
    "not_found": "I couldn't find you on the staff database. Make sure your pin is in your nickname, e.g. **[1234] Name**, or speak to Command.",
    "unauthorised": "The bot isn't connected to the staff database properly. Let Command know.",
    "bad_dates": "Your end date is before your start date.",
}


def banner(kind):
    return discord.File(os.path.join(HERE, "banners", BANNERS[kind]), filename="banner.png")


def now():
    return datetime.now(TZ)


def parse_date(text):
    """DD/MM/YYYY, DD/MM, 'today', 'yesterday' or blank (today). Returns a date or None."""
    t = (text or "").strip().lower()
    today = now().date()
    if t in ("", "today"):
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", t)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    y = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
    try:
        return datetime(y, mo, d).date()
    except ValueError:
        return None


def parse_time(text):
    """18:30, 1830, 18.30, 6:30pm, 6pm. Returns (minutes, 'HH:MM') or None."""
    t = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?:[:.]?(\d{2}))?(am|pm)?", t)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi, f"{h:02d}:{mi:02d}"


def nice_date(d):
    return d.strftime("%d/%m/%Y")


# ----------------------------------------------------------------- modals

class ShiftModal(discord.ui.Modal, title="Shift Log"):
    date = discord.ui.TextInput(label="Date", placeholder="DD/MM/YYYY - leave blank for today", required=False, max_length=10)
    start = discord.ui.TextInput(label="Shift start", placeholder="e.g. 18:30", max_length=8)
    end = discord.ui.TextInput(label="Shift end", placeholder="e.g. 20:15", max_length=8)
    routes = discord.ui.TextInput(label="Routes", placeholder="e.g. 1, 9, 141", max_length=200)
    notes = discord.ui.TextInput(label="Notes", style=discord.TextStyle.paragraph, required=False, max_length=1000,
                                 placeholder="Anything worth knowing (optional)")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction):
        await self.cog.handle_shift(interaction, self)


class IncidentModal(discord.ui.Modal, title="Driver Incident Log"):
    when = discord.ui.TextInput(label="Date and time", placeholder="e.g. 17/11/2026 19:40 - blank for now", required=False, max_length=20)
    location = discord.ui.TextInput(label="Route / location", placeholder="e.g. Route 1, Mansfield bus station", max_length=200)
    what = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph, max_length=1000)
    involved = discord.ui.TextInput(label="Anyone else involved?", placeholder="Passengers, other drivers, vehicles (optional)",
                                    required=False, max_length=300)
    action = discord.ui.TextInput(label="Action taken", style=discord.TextStyle.paragraph, max_length=1000,
                                  placeholder="What did you do after it happened?")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction):
        await self.cog.handle_incident(interaction, self)


class LoaModal(discord.ui.Modal, title="LOA Request"):
    start = discord.ui.TextInput(label="Start date", placeholder="DD/MM/YYYY", max_length=10)
    end = discord.ui.TextInput(label="End date", placeholder="DD/MM/YYYY", max_length=10)
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction):
        await self.cog.handle_loa(interaction, self)


# ----------------------------------------------------------------- panel

class FormSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(
            custom_id="scdf:select",
            placeholder="Choose a form...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Shift Log", value="shift", emoji="🕒",
                                     description="Log a shift you've just finished"),
                discord.SelectOption(label="Driver Incident Log", value="incident", emoji="⚠️",
                                     description="Report an incident, e.g. when no supervisor is on"),
                discord.SelectOption(label="LOA Request", value="loa", emoji="📅",
                                     description="Request a leave of absence"),
            ],
        )

    async def callback(self, interaction):
        modal = {"shift": ShiftModal, "incident": IncidentModal, "loa": LoaModal}[self.values[0]](self.cog)
        await interaction.response.send_modal(modal)
        try:  # reset the menu so the same option can be picked again
            await interaction.message.edit(view=PanelView(self.cog))
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.add_item(FormSelect(cog))


# ----------------------------------------------------------------- cog

class DriverForms(commands.Cog):
    """Stagecoach East Midlands driver forms (shift logs, incidents, LOA)."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.config = {}
        self._session = None
        self.view = PanelView(self)
        bot.add_view(self.view)

    async def cog_load(self):
        await self.load_config()

    async def cog_unload(self):
        self.view.stop()
        if self._session:
            await self._session.close()

    # ---------- config / http

    async def load_config(self):
        self.config = await self.db.find_one({"_id": "config"}) or {"_id": "config"}

    async def save(self, **kwargs):
        self.config.update(kwargs)
        await self.db.find_one_and_update({"_id": "config"}, {"$set": kwargs}, upsert=True)

    @property
    def session(self):
        s = getattr(self.bot, "session", None)
        if s and not s.closed:
            return s
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def call(self, action, **data):
        url, secret = self.config.get("url"), self.config.get("secret")
        if not url or not secret:
            return {"ok": False, "error": "not_setup"}
        payload = {"secret": secret, "action": action, **data}
        try:
            async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30),
                                         allow_redirects=True) as resp:
                text = await resp.text()
            return json.loads(text)
        except Exception as e:  # network error, bad JSON, timeout
            return {"ok": False, "error": f"request_failed: {type(e).__name__}"}

    def who(self, member):
        nick = getattr(member, "nick", None) or member.display_name or ""
        m = PIN_RE.search(nick)
        return {"pin": m.group(1) if m else "", "username": member.name, "discord_id": str(member.id)}

    def channel(self, key):
        cid = self.config.get(f"{key}_channel")
        return self.bot.get_channel(int(cid)) if cid else None

    def role(self, guild, key):
        rid = self.config.get(f"{key}_role")
        return guild.get_role(int(rid)) if rid and guild else None

    async def fail(self, interaction, result):
        err = result.get("error", "unknown")
        msg = ERRORS.get(err)
        if not msg:
            msg = ("The forms aren't set up yet. Let Command know." if err == "not_setup"
                   else f"Something went wrong saving that (`{err}`). Try again, or let Command know.")
        await interaction.followup.send(msg, ephemeral=True)

    def base_embed(self, title, member, staff, colour=NAVY):
        e = discord.Embed(title=title, colour=colour, timestamp=now())
        e.set_author(name=f"[{staff['pin']}] {staff['name']}", icon_url=member.display_avatar.url)
        e.add_field(name="Driver", value=member.mention, inline=True)
        rank = (staff["rank"] or "-") + (" • **Trainee**" if staff["rank"] == "Trainee Driver" else "")
        e.add_field(name="Rank", value=rank, inline=True)
        e.add_field(name="Roblox", value=staff["roblox"] or "-", inline=True)
        e.set_image(url="attachment://banner.png")
        e.set_footer(text=FOOTER)
        return e

    async def post(self, key, embed, kind, content=None, view=None, thread_name=None):
        ch = self.channel(key)
        if ch is None:
            return None
        mentions = discord.AllowedMentions(roles=True, users=True, everyone=False)
        if isinstance(ch, discord.ForumChannel):
            kwargs = dict(name=thread_name or embed.title, content=content, embed=embed, file=banner(kind),
                          allowed_mentions=mentions)
            if view is not None:
                kwargs["view"] = view
            res = await ch.create_thread(**kwargs)
            return res.message
        return await ch.send(content=content, embed=embed, file=banner(kind), view=view, allowed_mentions=mentions)

    # ---------- form handlers

    async def handle_shift(self, interaction, form):
        d = parse_date(form.date.value)
        s, e = parse_time(form.start.value), parse_time(form.end.value)
        if d is None:
            return await interaction.response.send_message("I couldn't read that date. Use DD/MM/YYYY.", ephemeral=True)
        if d > now().date():
            return await interaction.response.send_message("You can't log a shift in the future.", ephemeral=True)
        if not s or not e:
            return await interaction.response.send_message("I couldn't read your shift times. Use HH:MM, e.g. 18:30.", ephemeral=True)
        mins = (e[0] - s[0]) % 1440
        if mins == 0 or mins > 12 * 60:
            return await interaction.response.send_message("Those times don't look right (0 or over 12 hours). Double check them.", ephemeral=True)
        hours = round(mins / 60, 2)
        await interaction.response.defer(ephemeral=True, thinking=True)

        res = await self.call("shift", **self.who(interaction.user), date=d.isoformat(), start=s[1], end=e[1],
                              hours=hours, routes=form.routes.value, notes=form.notes.value)
        if not res.get("ok"):
            return await self.fail(interaction, res)
        staff = res["staff"]

        emb = self.base_embed("🕒 Shift Log", interaction.user, staff)
        emb.add_field(name="Date", value=nice_date(d), inline=True)
        emb.add_field(name="Shift", value=f"{s[1]} - {e[1]}", inline=True)
        emb.add_field(name="Hours", value=f"{hours:g}", inline=True)
        emb.add_field(name="Routes", value=form.routes.value, inline=False)
        if form.notes.value.strip():
            emb.add_field(name="Notes", value=form.notes.value, inline=False)
        await self.post("shift", emb, "shift")
        await interaction.followup.send(f"✅ Shift logged: **{nice_date(d)}**, {s[1]} - {e[1]} ({hours:g}h). Cheers!", ephemeral=True)

    async def handle_incident(self, interaction, form):
        raw = (form.when.value or "").strip()
        if raw:
            parts = raw.split()
            d = parse_date(parts[0])
            t = parse_time(parts[1]) if len(parts) > 1 else None
            if d is None:
                return await interaction.response.send_message("I couldn't read that date. Use DD/MM/YYYY HH:MM.", ephemeral=True)
            when = nice_date(d) + (f" {t[1]}" if t else "")
        else:
            d = now().date()
            when = now().strftime("%d/%m/%Y %H:%M")
        await interaction.response.defer(ephemeral=True, thinking=True)

        res = await self.call("incident", **self.who(interaction.user), date=d.isoformat(), when=when,
                              location=form.location.value, what=form.what.value,
                              involved=form.involved.value, action_taken=form.action.value)
        if not res.get("ok"):
            return await self.fail(interaction, res)
        staff = res["staff"]

        emb = self.base_embed("⚠️ Driver Incident Log", interaction.user, staff, colour=AMBER)
        emb.add_field(name="When", value=when, inline=True)
        emb.add_field(name="Route / location", value=form.location.value, inline=True)
        emb.add_field(name="​", value="​", inline=True)
        emb.add_field(name="What happened", value=form.what.value, inline=False)
        emb.add_field(name="Others involved", value=form.involved.value or "None given", inline=False)
        emb.add_field(name="Action taken", value=form.action.value, inline=False)
        emb.add_field(name="Status", value="🟠 **Needs reviewing** - added to Driving Standards", inline=False)

        pings = [r.mention for r in (self.role(interaction.guild, "incident"),
                                     self.role(interaction.guild, "trainer") if staff["rank"] == "Trainee Driver" else None) if r]
        await self.post("incident", emb, "incident", content=" ".join(pings) or None,
                        thread_name=f"Incident - [{staff['pin']}] {staff['name']} - {nice_date(d)}")
        await interaction.followup.send("✅ Incident logged and sent to the supervisors. Thanks for reporting it.", ephemeral=True)

    async def handle_loa(self, interaction, form):
        s, e = parse_date(form.start.value), parse_date(form.end.value)
        if s is None or e is None:
            return await interaction.response.send_message("I couldn't read those dates. Use DD/MM/YYYY.", ephemeral=True)
        if e < s:
            return await interaction.response.send_message("Your end date is before your start date.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        req_id = "LOA-" + secrets.token_hex(3).upper()
        res = await self.call("loa", **self.who(interaction.user), start=s.isoformat(), end=e.isoformat(),
                              reason=form.reason.value, request_id=req_id)
        if not res.get("ok"):
            return await self.fail(interaction, res)
        staff, status, days = res["staff"], res["status"], res["days"]
        pending = status == "Pending"

        emb = self.base_embed("📅 LOA Request", interaction.user, staff, colour=AMBER if pending else GREEN)
        emb.add_field(name="From", value=nice_date(s), inline=True)
        emb.add_field(name="To", value=nice_date(e), inline=True)
        emb.add_field(name="Days", value=str(days), inline=True)
        emb.add_field(name="Reason", value=form.reason.value, inline=False)
        emb.add_field(name="Status", value="🟠 **Pending** - over 2 weeks, needs Command approval" if pending
                      else "🟢 **Approved (Auto)** - 2 weeks or under", inline=False)
        emb.add_field(name="Request ID", value=f"`{req_id}`", inline=False)

        view, content = None, None
        if pending:
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, emoji="✅",
                                            custom_id=f"scdf:loa:Approved:{req_id}:{interaction.user.id}"))
            view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖️",
                                            custom_id=f"scdf:loa:Denied:{req_id}:{interaction.user.id}"))
            r = self.role(interaction.guild, "approver")
            content = r.mention if r else None
        await self.post("loa", emb, "loa", content=content, view=view,
                        thread_name=f"LOA - [{staff['pin']}] {staff['name']} - {nice_date(s)}")
        msg = (f"📅 Your LOA ({nice_date(s)} - {nice_date(e)}, {days} days) is over 2 weeks so it's been sent to Command for approval. "
               "You'll get a DM when it's been looked at." if pending
               else f"✅ Your LOA ({nice_date(s)} - {nice_date(e)}, {days} days) has been approved automatically. Enjoy the break!")
        await interaction.followup.send(msg, ephemeral=True)

    # ---------- LOA approve / deny buttons

    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("scdf:loa:"):
            return
        _, _, status, req_id, user_id = cid.split(":")
        member = interaction.user
        approver = self.role(interaction.guild, "approver")
        allowed = member.guild_permissions.manage_guild or (approver and approver in getattr(member, "roles", []))
        if not allowed:
            return await interaction.response.send_message("Only Command can approve or deny LOAs.", ephemeral=True)
        await interaction.response.defer()

        res = await self.call("loa_status", request_id=req_id, status=status, reviewer=member.name)
        if not res.get("ok"):
            err = res.get("error", "")
            text = (f"That LOA has already been {err.split('_', 1)[1]}." if err.startswith("already_")
                    else f"Couldn't update the sheet (`{err}`).")
            return await interaction.followup.send(text, ephemeral=True)

        emb = interaction.message.embeds[0]
        for i, f in enumerate(emb.fields):
            if f.name == "Status":
                icon = "🟢" if status == "Approved" else "🔴"
                emb.set_field_at(i, name="Status", value=f"{icon} **{status}** by {member.mention}", inline=False)
        emb.colour = GREEN if status == "Approved" else RED
        await interaction.message.edit(embed=emb, view=None)

        user = interaction.guild.get_member(int(user_id)) or await self.bot.fetch_user(int(user_id))
        try:
            await user.send(f"Your Stagecoach LOA request `{req_id}` has been **{status.lower()}**.")
        except (discord.HTTPException, AttributeError):
            pass

    # ---------- commands

    @commands.group(name="driverforms", aliases=["df"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df(self, ctx):
        """Stagecoach driver forms."""
        await ctx.send_help(ctx.command)

    @df.command(name="setup")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_setup(self, ctx, url: str, secret: str):
        """Connect to the staff database web app. Your message gets deleted."""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await self.save(url=url, secret=secret)
        res = await self.call("ping")
        await ctx.send("✅ Connected to the staff database." if res.get("ok")
                       else f"⚠️ Saved, but the test failed (`{res.get('error')}`). Check the URL and password.")

    @df.command(name="channel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_channel(self, ctx, kind: str, channel: discord.abc.GuildChannel):
        """Set where posts go: shift, incident or loa."""
        kind = kind.lower()
        if kind not in ("shift", "incident", "loa"):
            return await ctx.send("Use `shift`, `incident` or `loa`.")
        await self.save(**{f"{kind}_channel": str(channel.id)})
        await ctx.send(f"✅ {kind.title()} posts will go to {channel.mention}.")

    @df.command(name="role")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_role(self, ctx, kind: str, role: discord.Role):
        """Set roles: incident (pinged for incidents), trainer (pinged for trainee incidents), approver (approves long LOAs)."""
        kind = kind.lower()
        if kind not in ("incident", "trainer", "approver"):
            return await ctx.send("Use `incident`, `trainer` or `approver`.")
        await self.save(**{f"{kind}_role": str(role.id)})
        await ctx.send(f"✅ {kind.title()} role set to {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @df.command(name="panel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_panel(self, ctx, channel: discord.TextChannel = None):
        """Post the Driver Forms panel."""
        channel = channel or ctx.channel
        emb = discord.Embed(
            title="Driver Forms",
            colour=NAVY,
            description=(
                "Pick a form from the menu below. Everything you submit goes straight onto the staff database.\n\n"
                "🕒 **Shift Log**\nLog every shift once you've finished. You need **2 a week** to stay active.\n\n"
                "⚠️ **Driver Incident Log**\nReport anything that happened on the road, especially if no supervisor was on.\n\n"
                "📅 **LOA Request**\nGoing away? Up to 2 weeks is approved automatically. Longer needs Command approval.\n\n"
                "Make sure your pin is in your nickname, e.g. **[1234] Name**."
            ),
        )
        emb.set_image(url="attachment://banner.png")
        emb.set_footer(text=FOOTER)
        await channel.send(embed=emb, file=banner("panel"), view=PanelView(self))
        if channel != ctx.channel:
            await ctx.send(f"✅ Panel posted in {channel.mention}.")

    @df.command(name="config")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_config(self, ctx):
        """Show the current settings."""
        c = self.config
        ch = lambda k: f"<#{c[k]}>" if c.get(k) else "not set"
        rl = lambda k: f"<@&{c[k]}>" if c.get(k) else "not set"
        emb = discord.Embed(title="Driver Forms config", colour=NAVY)
        emb.add_field(name="Staff database", value="connected" if c.get("url") else "not set", inline=False)
        emb.add_field(name="Shift channel", value=ch("shift_channel"))
        emb.add_field(name="Incident channel", value=ch("incident_channel"))
        emb.add_field(name="LOA channel", value=ch("loa_channel"))
        emb.add_field(name="Incident role", value=rl("incident_role"))
        emb.add_field(name="Trainer role", value=rl("trainer_role"))
        emb.add_field(name="Approver role", value=rl("approver_role"))
        await ctx.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    @df.command(name="test")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def df_test(self, ctx, member: discord.Member = None):
        """Check the sheet connection and look someone up."""
        member = member or ctx.author
        res = await self.call("lookup", **self.who(member))
        if res.get("ok"):
            s = res["staff"]
            await ctx.send(f"✅ Found **[{s['pin']}] {s['name']}** - {s['rank']} ({s['activity'] or 'no activity yet'}).")
        else:
            await ctx.send(f"⚠️ `{res.get('error')}`")


async def setup(bot):
    await bot.add_cog(DriverForms(bot))
