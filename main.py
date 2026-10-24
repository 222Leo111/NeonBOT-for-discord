import asyncio
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True          # enable "Server Members Intent" in the Dev Portal
intents.message_content = True  # enable "Message Content Intent" (for edit/delete logs)

# ---------------------------------------------------------------- database
db = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db"))
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER PRIMARY KEY,
    modlog_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS cases (
    guild_id INTEGER, case_num INTEGER, action TEXT,
    user_id INTEGER, mod_id INTEGER, reason TEXT, created_at INTEGER,
    PRIMARY KEY (guild_id, case_num)
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, user_id INTEGER, mod_id INTEGER, reason TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS tempbans (
    guild_id INTEGER, user_id INTEGER, unban_at INTEGER,
    PRIMARY KEY (guild_id, user_id)
);
""")
db.commit()


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        check_tempbans.start()
        await self.tree.sync()


bot = Bot()

# ---------------------------------------------------------------- helpers
ACTION_COLORS = {
    "Ban": discord.Color.red(), "Tempban": discord.Color.red(), "Softban": discord.Color.red(),
    "Kick": discord.Color.orange(), "Timeout": discord.Color.orange(), "Warn": discord.Color.yellow(),
    "Unban": discord.Color.green(), "Untimeout": discord.Color.green(),
}


def get_modlog_channel(guild: discord.Guild):
    row = db.execute("SELECT modlog_channel_id FROM config WHERE guild_id=?", (guild.id,)).fetchone()
    if row and row["modlog_channel_id"]:
        return guild.get_channel(row["modlog_channel_id"])
    return None


async def send_log(guild: discord.Guild, embed: discord.Embed):
    channel = get_modlog_channel(guild)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def log_action(guild, action, user, mod, reason):
    """Saves a numbered case, posts it to the mod log, and returns the case number."""
    user_id = getattr(user, "id", user)
    mod_id = getattr(mod, "id", mod)
    n = db.execute("SELECT COALESCE(MAX(case_num),0)+1 AS n FROM cases WHERE guild_id=?",
                   (guild.id,)).fetchone()["n"]
    db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
               (guild.id, n, action, user_id, mod_id, reason, int(time.time())))
    db.commit()
    embed = discord.Embed(title=f"Case #{n} · {action}",
                          color=ACTION_COLORS.get(action.split()[0], discord.Color.blurple()),
                          timestamp=discord.utils.utcnow())
    if user_id:
        embed.add_field(name="User", value=f"<@{user_id}> ({user_id})")
    embed.add_field(name="Moderator", value=f"<@{mod_id}>")
    embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
    await send_log(guild, embed)
    return n


def can_moderate(interaction: discord.Interaction, target: discord.Member):
    """Returns an error message if the action isn't allowed, else None."""
    if target == interaction.user:
        return "You can't do that to yourself."
    if target.id == bot.user.id:
        return "I'm not doing that to myself."
    if target.id == interaction.guild.owner_id:
        return "You can't moderate the server owner."
    if interaction.user.id != interaction.guild.owner_id and target.top_role >= interaction.user.top_role:
        return "That member's role is equal to or higher than yours."
    if target.top_role >= interaction.guild.me.top_role:
        return "That member's role is higher than mine. Move my role up in Server Settings."
    return None


def choices_from(d):
    return [app_commands.Choice(name=v[0], value=k) for k, v in d.items()]


# ---------------------------------------------------------------- events
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    channel = member.guild.system_channel
    if channel:
        await channel.send(f"Welcome to the server, {member.mention}! 🎉")


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    embed = discord.Embed(title="🗑️ Message deleted", color=discord.Color.dark_grey(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Author", value=f"{message.author.mention} ({message.author.id})")
    embed.add_field(name="Channel", value=message.channel.mention)
    embed.add_field(name="Content", value=message.content[:1000] or "*(no text)*", inline=False)
    if message.attachments:
        embed.add_field(name="Attachments", value=str(len(message.attachments)))
    await send_log(message.guild, embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    embed = discord.Embed(title="✏️ Message edited", color=discord.Color.light_grey(),
                          timestamp=discord.utils.utcnow(), url=after.jump_url)
    embed.add_field(name="Author", value=f"{before.author.mention} ({before.author.id})")
    embed.add_field(name="Channel", value=before.channel.mention)
    embed.add_field(name="Before", value=before.content[:1000] or "*(empty)*", inline=False)
    embed.add_field(name="After", value=after.content[:1000] or "*(empty)*", inline=False)
    await send_log(before.guild, embed)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild:
        embed = discord.Embed(
            title="🧹 Bulk delete",
            description=f"{len(payload.message_ids)} messages deleted in <#{payload.channel_id}>",
            color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        await send_log(guild, embed)


@tasks.loop(minutes=1)
async def check_tempbans():
    rows = db.execute("SELECT guild_id, user_id FROM tempbans WHERE unban_at <= ?",
                      (int(time.time()),)).fetchall()
    for r in rows:
        guild = bot.get_guild(r["guild_id"])
        if guild:
            try:
                await guild.unban(discord.Object(id=r["user_id"]), reason="Tempban expired")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                continue  # try again next minute
            await log_action(guild, "Unban (tempban ended)", r["user_id"], bot.user, "Tempban ended")
        db.execute("DELETE FROM tempbans WHERE guild_id=? AND user_id=?", (r["guild_id"], r["user_id"]))
        db.commit()


@check_tempbans.before_loop
async def before_tempbans():
    await bot.wait_until_ready()


@bot.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "I don't have the permissions I need to do that."
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        msg = "Discord won't let me do that. Check my permissions and that my role is high enough."
    else:
        msg = f"Something went wrong: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ================================================================ FUN / UTILITY
@bot.tree.command(name="ping", description="Check how fast the bot is responding")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


@bot.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides on the dice (default 6)")
async def roll(interaction: discord.Interaction, sides: int = 6):
    if sides < 2:
        await interaction.response.send_message("A die needs at least 2 sides.", ephemeral=True)
        return
    await interaction.response.send_message(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})")


@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="The question you want to ask")
async def eightball(interaction: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Definitely!", "Not a chance.",
               "Ask again later.", "Signs point to yes.", "Very doubtful."]
    await interaction.response.send_message(f"❓ {question}\n🎱 {random.choice(answers)}")


@bot.tree.command(name="userinfo", description="Show info about a member")
@app_commands.describe(member="The member to look up (default: you)")
@app_commands.guild_only()
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=str(member), color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    await interaction.response.send_message(embed=embed)


NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


@bot.tree.command(name="poll", description="Create a poll people can vote on with reactions")
@app_commands.describe(question="What the poll is asking", option1="First choice", option2="Second choice",
                       option3="Third choice (optional)", option4="Fourth choice (optional)",
                       option5="Fifth choice (optional)")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str,
               option3: str = None, option4: str = None, option5: str = None):
    options = [o for o in (option1, option2, option3, option4, option5) if o]
    description = "\n".join(f"{NUMBER_EMOJIS[i]} {o}" for i, o in enumerate(options))
    embed = discord.Embed(title=f"📊 {question}", description=description, color=discord.Color.gold())
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    for i in range(len(options)):
        await message.add_reaction(NUMBER_EMOJIS[i])


@bot.tree.command(name="remind", description="Get a reminder in this channel after a set time")
@app_commands.describe(minutes="How many minutes from now (1 to 1440)", message="What to remind you about")
async def remind(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440], message: str):
    await interaction.response.send_message(f"⏰ Okay, I'll remind you in {minutes} minute(s).", ephemeral=True)

    async def send_reminder():
        await asyncio.sleep(minutes * 60)
        text = f"⏰ {interaction.user.mention}, reminder: {message}"
        try:
            await interaction.channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            try:
                await interaction.user.send(text)
            except discord.HTTPException:
                pass

    bot.loop.create_task(send_reminder())


@bot.tree.command(name="say", description="Make the bot send a message")
@app_commands.describe(message="What the bot should say", channel="Where to send it (default: this channel)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def say(interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions(everyone=False))
    except discord.Forbidden:
        await interaction.response.send_message(f"I can't send messages in {channel.mention}.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Sent in {channel.mention}.", ephemeral=True)


# ================================================================ MODERATION
@bot.tree.command(name="kick", description="Kick a member out of the server (they can rejoin)")
@app_commands.describe(member="The member to kick", reason="Why they're being kicked")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await member.kick(reason=f"{interaction.user}: {reason}")
    case = await log_action(interaction.guild, "Kick", member, interaction.user, reason)
    await interaction.response.send_message(f"👢 Kicked **{member}**. Reason: {reason} (Case #{case})")


@bot.tree.command(name="ban", description="Permanently ban a member from the server")
@app_commands.describe(member="The member to ban", reason="Why they're being banned")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await member.ban(reason=f"{interaction.user}: {reason}")
    case = await log_action(interaction.guild, "Ban", member, interaction.user, reason)
    await interaction.response.send_message(f"🔨 Banned **{member}**. Reason: {reason} (Case #{case})")


TEMPBAN_OPTIONS = {
    "1h": ("1 hour", 1), "6h": ("6 hours", 6), "1d": ("1 day", 24),
    "3d": ("3 days", 72), "7d": ("7 days", 168), "30d": ("30 days", 720),
}


@bot.tree.command(name="tempban", description="Ban a member for a set time, then unban them automatically")
@app_commands.describe(member="The member to ban", duration="How long the ban lasts", reason="Why they're being banned")
@app_commands.choices(duration=choices_from(TEMPBAN_OPTIONS))
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, member: discord.Member,
                  duration: app_commands.Choice[str], reason: str = "No reason given"):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    label, hours = TEMPBAN_OPTIONS[duration.value]
    await member.ban(reason=f"Tempban ({label}) by {interaction.user}: {reason}")
    db.execute("INSERT OR REPLACE INTO tempbans VALUES (?,?,?)",
               (interaction.guild.id, member.id, int(time.time() + hours * 3600)))
    db.commit()
    case = await log_action(interaction.guild, "Tempban", member, interaction.user, f"{label}: {reason}")
    await interaction.response.send_message(
        f"⏳ Banned **{member}** for {label}. Reason: {reason} (Case #{case})")


@bot.tree.command(name="softban", description="Ban then instantly unban to wipe a member's last day of messages")
@app_commands.describe(member="The member to softban", reason="Why they're being softbanned")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def softban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await interaction.response.defer()
    await member.ban(reason=f"Softban by {interaction.user}: {reason}", delete_message_seconds=86400)
    await interaction.guild.unban(member, reason="Softban")
    case = await log_action(interaction.guild, "Softban", member, interaction.user, reason)
    await interaction.followup.send(
        f"🧽 Softbanned **{member}** (last 24h of messages deleted, they can rejoin). (Case #{case})")


@bot.tree.command(name="unban", description="Unban a user using their user ID")
@app_commands.describe(user_id="The banned user's ID (right-click them > Copy User ID)",
                       reason="Why they're being unbanned")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason given"):
    if not user_id.isdigit():
        await interaction.response.send_message("That doesn't look like a user ID (numbers only).", ephemeral=True)
        return
    uid = int(user_id)
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=f"{interaction.user}: {reason}")
    except discord.NotFound:
        await interaction.response.send_message("That user isn't banned.", ephemeral=True)
        return
    db.execute("DELETE FROM tempbans WHERE guild_id=? AND user_id=?", (interaction.guild.id, uid))
    db.commit()
    case = await log_action(interaction.guild, "Unban", uid, interaction.user, reason)
    await interaction.response.send_message(f"✅ Unbanned <@{uid}>. Reason: {reason} (Case #{case})")


@bot.tree.command(name="banlist", description="Show the users currently banned from this server")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def banlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    lines = []
    async for entry in interaction.guild.bans(limit=25):
        lines.append(f"• **{entry.user}** (`{entry.user.id}`) — {entry.reason or 'No reason'}")
    text = "\n".join(lines) if lines else "Nobody is banned."
    await interaction.followup.send(f"🔨 **Banned users (up to 25):**\n{text}"[:2000], ephemeral=True)


TIMEOUT_OPTIONS = {
    "1": ("1 minute", 1), "5": ("5 minutes", 5), "10": ("10 minutes", 10),
    "60": ("1 hour", 60), "360": ("6 hours", 360), "1440": ("1 day", 1440),
    "10080": ("1 week", 10080), "40320": ("28 days (max)", 40320),
}


@bot.tree.command(name="timeout", description="Mute a member for a set time so they can't chat or talk")
@app_commands.describe(member="The member to timeout", duration="How long the timeout lasts",
                       reason="Why they're being timed out")
@app_commands.choices(duration=choices_from(TIMEOUT_OPTIONS))
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member,
                  duration: app_commands.Choice[str], reason: str = "No reason given"):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    label, minutes = TIMEOUT_OPTIONS[duration.value]
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    case = await log_action(interaction.guild, "Timeout", member, interaction.user, f"{label}: {reason}")
    await interaction.response.send_message(
        f"🔇 Timed out **{member}** for {label}. Reason: {reason} (Case #{case})")


@bot.tree.command(name="untimeout", description="Remove a member's timeout early")
@app_commands.describe(member="The member to unmute")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    case = await log_action(interaction.guild, "Untimeout", member, interaction.user, "Timeout removed")
    await interaction.response.send_message(f"🔊 Removed timeout from **{member}**. (Case #{case})")


# ---- warnings
@bot.tree.command(name="warn", description="Give a member a warning (they get a DM)")
@app_commands.describe(member="The member to warn", reason="What they did wrong")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    db.execute("INSERT INTO warnings (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)",
               (interaction.guild.id, member.id, interaction.user.id, reason, int(time.time())))
    db.commit()
    total = db.execute("SELECT COUNT(*) AS c FROM warnings WHERE guild_id=? AND user_id=?",
                       (interaction.guild.id, member.id)).fetchone()["c"]
    case = await log_action(interaction.guild, "Warn", member, interaction.user, reason)
    try:
        await member.send(f"⚠️ You were warned in **{interaction.guild.name}**: {reason}")
        dm_note = ""
    except discord.HTTPException:
        dm_note = " (couldn't DM them)"
    await interaction.response.send_message(
        f"⚠️ Warned {member.mention}: {reason}\nThey now have **{total}** warning(s). (Case #{case}){dm_note}")


@bot.tree.command(name="warnings", description="See all warnings a member has")
@app_commands.describe(member="The member to check")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    rows = db.execute("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                      (interaction.guild.id, member.id)).fetchall()
    if not rows:
        await interaction.response.send_message(f"{member.display_name} has no warnings. ✅", ephemeral=True)
        return
    embed = discord.Embed(title=f"Warnings for {member.display_name} (latest {len(rows)})",
                          color=discord.Color.yellow())
    for r in rows:
        when = discord.utils.format_dt(datetime.fromtimestamp(r["created_at"], tz=timezone.utc), "R")
        embed.add_field(name=f"{when} · by <@{r['mod_id']}>", value=r["reason"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Delete all of a member's warnings")
@app_commands.describe(member="The member whose warnings to clear")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    cur = db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
    db.commit()
    await log_action(interaction.guild, "Clear warnings", member, interaction.user, f"{cur.rowcount} warning(s) cleared")
    await interaction.response.send_message(f"🧼 Cleared {cur.rowcount} warning(s) for {member.mention}.")


# ---- cleaning messages
CLEAN_OPTIONS = {
    "5m": ("Last 5 minutes", None, 5), "15m": ("Last 15 minutes", None, 15),
    "1h": ("Last hour", None, 60), "6h": ("Last 6 hours", None, 360),
    "24h": ("Last 24 hours", None, 1440), "50": ("Last 50 messages", 50, None),
    "100": ("Last 100 messages", 100, None), "500": ("Last 500 messages", 500, None),
}


@bot.tree.command(name="clean", description="Delete recent messages in this channel (pick how far back)")
@app_commands.describe(how_far="Pick how far back to delete",
                       only_from="Only delete messages from this person (optional)")
@app_commands.choices(how_far=choices_from(CLEAN_OPTIONS))
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
async def clean(interaction: discord.Interaction, how_far: app_commands.Choice[str],
                only_from: discord.Member = None):
    label, amount, minutes = CLEAN_OPTIONS[how_far.value]
    channel = interaction.channel
    await interaction.response.defer(ephemeral=True)
    after = discord.utils.utcnow() - timedelta(minutes=minutes) if minutes else None

    def check(m: discord.Message):
        if m.pinned:
            return False
        return only_from is None or m.author.id == only_from.id

    deleted = await channel.purge(limit=amount, after=after, check=check, bulk=True)
    who = f" from {only_from.display_name}" if only_from else ""
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s){who} ({label.lower()}).", ephemeral=True)
    await channel.send(f"🧹 {len(deleted)} message(s) cleared.", delete_after=5)


# ---- channel controls
SLOWMODE_OPTIONS = {
    "0": ("Off", 0), "5": ("5 seconds", 5), "10": ("10 seconds", 10), "30": ("30 seconds", 30),
    "60": ("1 minute", 60), "300": ("5 minutes", 300), "900": ("15 minutes", 900), "3600": ("1 hour", 3600),
}


@bot.tree.command(name="slowmode", description="Limit how often people can send messages in a channel")
@app_commands.describe(delay="How long people must wait between messages", channel="Channel to change (default: this one)")
@app_commands.choices(delay=choices_from(SLOWMODE_OPTIONS))
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, delay: app_commands.Choice[str],
                   channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    label, seconds = SLOWMODE_OPTIONS[delay.value]
    await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
    await log_action(interaction.guild, "Slowmode", None, interaction.user, f"{channel.mention}: {label}")
    await interaction.response.send_message(f"🐌 Slowmode in {channel.mention}: **{label}**")


async def set_can_send(channel: discord.TextChannel, value, reason: str):
    overwrite = channel.overwrites_for(channel.guild.default_role)
    overwrite.send_messages = value
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=reason)


@bot.tree.command(name="lock", description="Stop regular members from sending messages in a channel")
@app_commands.describe(channel="Channel to lock (default: this one)", reason="Why it's being locked")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None,
               reason: str = "No reason given"):
    channel = channel or interaction.channel
    await set_can_send(channel, False, f"Lock by {interaction.user}: {reason}")
    await log_action(interaction.guild, "Lock", None, interaction.user, f"{channel.mention}: {reason}")
    await interaction.response.send_message(f"🔒 {channel.mention} is locked. Reason: {reason}")


@bot.tree.command(name="unlock", description="Let members send messages in a locked channel again")
@app_commands.describe(channel="Channel to unlock (default: this one)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    await set_can_send(channel, None, f"Unlock by {interaction.user}")
    await log_action(interaction.guild, "Unlock", None, interaction.user, channel.mention)
    await interaction.response.send_message(f"🔓 {channel.mention} is unlocked.")


@bot.tree.command(name="lockdown", description="Lock EVERY text channel at once (for raids or emergencies)")
@app_commands.describe(reason="Why the server is going into lockdown")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def lockdown(interaction: discord.Interaction, reason: str = "No reason given"):
    await interaction.response.defer()
    done = 0
    for ch in interaction.guild.text_channels:
        try:
            await set_can_send(ch, False, f"Lockdown by {interaction.user}: {reason}")
            done += 1
        except discord.HTTPException:
            pass
    await log_action(interaction.guild, "Lockdown", None, interaction.user, f"{done} channels: {reason}")
    await interaction.followup.send(f"🚨 **Server lockdown!** Locked {done} channel(s). Reason: {reason}\n"
                                    "Use `/unlockdown` to reopen everything.")


@bot.tree.command(name="unlockdown", description="End a lockdown and reopen every text channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unlockdown(interaction: discord.Interaction):
    await interaction.response.defer()
    done = 0
    for ch in interaction.guild.text_channels:
        try:
            await set_can_send(ch, None, f"Lockdown ended by {interaction.user}")
            done += 1
        except discord.HTTPException:
            pass
    await log_action(interaction.guild, "Unlockdown", None, interaction.user, f"{done} channels reopened")
    await interaction.followup.send(f"✅ Lockdown over. Reopened {done} channel(s).")


# ---- nicknames & roles
@bot.tree.command(name="nick", description="Change a member's nickname (leave the name empty to reset it)")
@app_commands.describe(member="The member to rename", name="The new nickname (empty = reset)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_nicknames=True)
@app_commands.checks.has_permissions(manage_nicknames=True)
@app_commands.checks.bot_has_permissions(manage_nicknames=True)
async def nick(interaction: discord.Interaction, member: discord.Member, name: str = None):
    if err := can_moderate(interaction, member):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await member.edit(nick=name, reason=f"Nickname change by {interaction.user}")
    await log_action(interaction.guild, "Nickname", member, interaction.user, name or "Reset")
    msg = f"✏️ Changed {member.mention}'s nickname to **{name}**." if name else f"✏️ Reset {member.mention}'s nickname."
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


role_group = app_commands.Group(
    name="role", description="Give or remove roles from members", guild_only=True,
    default_permissions=discord.Permissions(manage_roles=True),
)


def role_error(interaction: discord.Interaction, role: discord.Role):
    if role.is_default() or role.managed:
        return "That role can't be given or removed manually."
    if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
        return "That role is equal to or higher than your highest role."
    if role >= interaction.guild.me.top_role:
        return "That role is higher than mine. Move my role up in Server Settings."
    return None


@role_group.command(name="add", description="Give a role to a member")
@app_commands.describe(member="The member to give the role to", role="The role to give")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if err := role_error(interaction, role):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await member.add_roles(role, reason=f"By {interaction.user}")
    await log_action(interaction.guild, "Role added", member, interaction.user, role.name)
    await interaction.response.send_message(f"✅ Gave **{role.name}** to {member.mention}.",
                                            allowed_mentions=discord.AllowedMentions.none())


@role_group.command(name="remove", description="Take a role away from a member")
@app_commands.describe(member="The member to take the role from", role="The role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if err := role_error(interaction, role):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await member.remove_roles(role, reason=f"By {interaction.user}")
    await log_action(interaction.guild, "Role removed", member, interaction.user, role.name)
    await interaction.response.send_message(f"✅ Removed **{role.name}** from {member.mention}.",
                                            allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(role_group)


# ---- mod log & cases
@bot.tree.command(name="modlog", description="Choose the channel where mod actions and deleted/edited messages are logged")
@app_commands.describe(channel="The channel to use as the mod log")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def modlog(interaction: discord.Interaction, channel: discord.TextChannel):
    try:
        await channel.send("📋 This channel is now the mod log.")
    except discord.Forbidden:
        await interaction.response.send_message(f"I can't send messages in {channel.mention}.", ephemeral=True)
        return
    db.execute("INSERT OR REPLACE INTO config (guild_id, modlog_channel_id) VALUES (?,?)",
               (interaction.guild.id, channel.id))
    db.commit()
    await interaction.response.send_message(f"✅ Mod log set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="case", description="Look up a past moderation action by its case number")
@app_commands.describe(number="The case number (shown after each mod action, e.g. 12)")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def case(interaction: discord.Interaction, number: int):
    r = db.execute("SELECT * FROM cases WHERE guild_id=? AND case_num=?",
                   (interaction.guild.id, number)).fetchone()
    if not r:
        await interaction.response.send_message(f"No case #{number} found.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Case #{r['case_num']} · {r['action']}",
                          color=ACTION_COLORS.get(r["action"].split()[0], discord.Color.blurple()),
                          timestamp=datetime.fromtimestamp(r["created_at"], tz=timezone.utc))
    if r["user_id"]:
        embed.add_field(name="User", value=f"<@{r['user_id']}> ({r['user_id']})")
    embed.add_field(name="Moderator", value=f"<@{r['mod_id']}>")
    embed.add_field(name="Reason", value=r["reason"] or "No reason given", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
