import asyncio
import os
import re
import sqlite3
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
URL_PATTERN = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put it in a .env file.")


def db_connect():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    with db_connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER,
                mod_log_channel_id INTEGER,
                bad_words TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL DEFAULT '',
                keyword_response TEXT NOT NULL DEFAULT '',
                panel_webhook_url TEXT NOT NULL DEFAULT '',
                ticket_webhook_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS custom_commands (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                response TEXT NOT NULL,
                PRIMARY KEY (guild_id, name)
            );
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(guild_settings)")}
        for column in ("panel_webhook_url", "ticket_webhook_url"):
            if column not in columns:
                db.execute(f"ALTER TABLE guild_settings ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def get_settings(guild_id: int):
    with db_connect() as db:
        row = db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            row = db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return row


def set_settings(guild_id: int, **values):
    allowed = {"log_channel_id", "mod_log_channel_id", "bad_words", "keyword", "keyword_response", "panel_webhook_url", "ticket_webhook_url"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db_connect() as db:
        db.execute(f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?", (*values.values(), guild_id))


def get_custom_command(guild_id: int, name: str):
    with db_connect() as db:
        return db.execute("SELECT response FROM custom_commands WHERE guild_id = ? AND name = ?", (guild_id, name.lower())).fetchone()


def audit_embed(title: str, description: str, colour: discord.Colour = discord.Colour.blurple()):
    embed = discord.Embed(title=title, description=description, colour=colour, timestamp=discord.utils.utcnow())
    return embed


async def send_log(guild: discord.Guild, embed: discord.Embed):
    settings = get_settings(guild.id)
    channel_id = settings["mod_log_channel_id"] or settings["log_channel_id"]
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        await channel.send(embed=embed)


def valid_webhook_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
        and parsed.path.startswith("/api/webhooks/")
    )


async def send_configured_webhook(guild: discord.Guild, setting_name: str, *, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    url = get_settings(guild.id)[setting_name]
    if not url:
        return
    webhook = discord.Webhook.from_url(url, client=bot, prefer_auth=False)
    await webhook.send(content=content, embed=embed, username=guild.me.display_name if guild.me else None, wait=False)


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="tickets:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Tickets are only available in a server.", ephemeral=True)
        existing = next((channel for channel in guild.text_channels if channel.topic == f"ticket-owner:{interaction.user.id}"), None)
        if existing:
            return await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets", reason="Ticket system setup")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        channel = await guild.create_text_channel(f"ticket-{interaction.user.name[:20]}", category=category, topic=f"ticket-owner:{interaction.user.id}", overwrites=overwrites, reason="Ticket opened")
        with db_connect() as db:
            db.execute("INSERT INTO tickets (channel_id, guild_id, owner_id) VALUES (?, ?, ?)", (channel.id, guild.id, interaction.user.id))
        await channel.send(f"{interaction.user.mention}, your ticket is open.", view=CloseTicketView())
        try:
            await send_configured_webhook(
                guild,
                "ticket_webhook_url",
                embed=audit_embed("Ticket opened", f"Member: {interaction.user.mention}\nChannel: {channel.mention}", discord.Colour.green()),
            )
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="tickets:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.topic or not interaction.channel.topic.startswith("ticket-owner:"):
            return await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        owner_id = int(interaction.channel.topic.split(":", 1)[1])
        if interaction.user.id != owner_id and not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only the ticket owner or a moderator can close this ticket.", ephemeral=True)
        await interaction.response.send_message("Closing this ticket in five seconds.")
        with db_connect() as db:
            db.execute("DELETE FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Ticket closed")


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_database()
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        await self.tree.sync()

    async def on_ready(self):
        if not self.presence_loop.is_running():
            self.presence_loop.start()
        print(f"Logged in as {self.user} ({self.user.id})")

    @tasks.loop(minutes=10)
    async def presence_loop(self):
        await self.change_presence(activity=discord.Game(name="/help | server tools"))

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        settings = get_settings(message.guild.id)
        bad_words = {word.strip().lower() for word in settings["bad_words"].split(",") if word.strip()}
        if bad_words and any(word in message.content.lower().split() for word in bad_words):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention}, that message was removed by the server filter.", delete_after=5)
                await send_log(message.guild, audit_embed("Automod: message removed", f"Author: {message.author.mention}\nChannel: {message.channel.mention}\nMessage ID: `{message.id}`", discord.Colour.red()))
            except discord.HTTPException:
                pass
            return
        keyword = settings["keyword"]
        if keyword and keyword.lower() in message.content.lower():
            await message.channel.send(settings["keyword_response"])
        if message.content.startswith("!"):
            command_name = message.content[1:].split(maxsplit=1)[0].lower()
            custom = get_custom_command(message.guild.id, command_name)
            if custom:
                await message.channel.send(custom["response"])
                return
        await self.process_commands(message)


bot = Bot()


def guild_only():
    return app_commands.checks.guild_only()


@bot.tree.command(name="help", description="Show the bot's feature list")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Server tools", colour=discord.Colour.blurple())
    embed.add_field(name="Moderation", value="/kick /ban /timeout /purge", inline=False)
    embed.add_field(name="Tickets", value="/ticket-panel /ticket-webhook /ticket-webhooks", inline=False)
    embed.add_field(name="Automation", value="/automod /keyword /custom-command", inline=False)
    embed.add_field(name="Embeds", value="/discohook", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="discohook", description="Send a Discohook webhook message to this channel")
@app_commands.describe(webhook_url="The webhook URL copied from Discohook", content="Optional plain text to send")
@guild_only()
@app_commands.checks.has_permissions(manage_webhooks=True)
async def discohook(interaction: discord.Interaction, webhook_url: str, content: Optional[str] = None):
    parsed = urlparse(webhook_url)
    if not valid_webhook_url(webhook_url):
        return await interaction.response.send_message("That is not a valid Discord webhook URL. In Discohook, use Integrations and copy the webhook URL.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    webhook = discord.Webhook.from_url(webhook_url, client=bot, prefer_auth=False)
    try:
        await webhook.send(content=content, username=interaction.guild.me.display_name if interaction.guild and interaction.guild.me else None, wait=False)
    except discord.HTTPException as error:
        return await interaction.followup.send(f"Discord rejected the webhook request: {error}", ephemeral=True)
    await interaction.followup.send("The Discohook webhook message was sent.", ephemeral=True)


@bot.tree.command(name="ticket-panel", description="Post a button that members can use to open tickets")
@guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="Support tickets", description="Press the button below to open a private support ticket.", colour=discord.Colour.blurple())
    await interaction.channel.send(embed=embed, view=TicketView())
    settings = get_settings(interaction.guild.id)
    if settings["panel_webhook_url"]:
        try:
            await send_configured_webhook(
                interaction.guild,
                "panel_webhook_url",
                embed=embed,
            )
        except discord.HTTPException:
            pass
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)


@bot.tree.command(name="ticket-webhook", description="Configure Discohook webhooks for the ticket panel and ticket headers")
@app_commands.describe(kind="Which ticket webhook to configure", webhook_url="Discohook webhook URL, or 'off' to disable")
@app_commands.choices(kind=[
    app_commands.Choice(name="Panel post", value="panel_webhook_url"),
    app_commands.Choice(name="Ticket opened header", value="ticket_webhook_url"),
])
@guild_only()
@app_commands.checks.has_permissions(manage_webhooks=True)
async def ticket_webhook(interaction: discord.Interaction, kind: app_commands.Choice[str], webhook_url: str):
    value = "" if webhook_url.lower() == "off" else webhook_url.strip()
    if value and not valid_webhook_url(value):
        return await interaction.response.send_message("Use the full HTTPS webhook URL copied from Discohook, or `off`.", ephemeral=True)
    set_settings(interaction.guild.id, **{kind.value: value})
    label = "panel post" if kind.value == "panel_webhook_url" else "ticket opened header"
    await interaction.response.send_message(f"The {label} webhook was updated.", ephemeral=True)


@bot.tree.command(name="ticket-webhooks", description="Show whether ticket Discohook webhooks are configured")
@guild_only()
@app_commands.checks.has_permissions(manage_webhooks=True)
async def ticket_webhooks(interaction: discord.Interaction):
    settings = get_settings(interaction.guild.id)
    embed = discord.Embed(title="Ticket webhooks", colour=discord.Colour.blurple())
    embed.add_field(name="Panel post", value="Configured" if settings["panel_webhook_url"] else "Not configured")
    embed.add_field(name="Ticket opened header", value="Configured" if settings["ticket_webhook_url"] else "Not configured")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="automod", description="Configure the simple word filter")
@app_commands.describe(words="Comma-separated words to remove, or 'off' to disable")
@guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def automod(interaction: discord.Interaction, words: str):
    set_settings(interaction.guild.id, bad_words="" if words.lower() == "off" else words)
    await interaction.response.send_message("Automod filter updated.", ephemeral=True)


@bot.tree.command(name="keyword", description="Configure a response to a keyword")
@app_commands.describe(keyword="Keyword, or 'off' to disable", response="Response sent when the keyword appears")
@guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def keyword(interaction: discord.Interaction, keyword: str, response: Optional[str] = None):
    if keyword.lower() == "off":
        set_settings(interaction.guild.id, keyword="", keyword_response="")
    elif not response:
        return await interaction.response.send_message("Provide a response for the keyword.", ephemeral=True)
    else:
        set_settings(interaction.guild.id, keyword=keyword, keyword_response=response)
    await interaction.response.send_message("Keyword response updated.", ephemeral=True)


@bot.tree.command(name="custom-command", description="Create or replace a custom slash command")
@app_commands.describe(name="Command name, 1-32 lowercase letters, numbers, or hyphens", response="The response")
@guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def custom_command(interaction: discord.Interaction, name: str, response: str):
    if not re.fullmatch(r"[a-z0-9-]{1,32}", name):
        return await interaction.response.send_message("The name must use lowercase letters, numbers, or hyphens.", ephemeral=True)
    with db_connect() as db:
        db.execute("INSERT INTO custom_commands (guild_id, name, response) VALUES (?, ?, ?) ON CONFLICT(guild_id, name) DO UPDATE SET response = excluded.response", (interaction.guild.id, name, response))
    await interaction.response.send_message(f"Custom command `/{name}` saved. It becomes available after the command tree refreshes.", ephemeral=True)
    await bot.tree.sync(guild=interaction.guild)


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
@guild_only()
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I cannot kick that member because of role hierarchy.", ephemeral=True)
    await member.kick(reason=reason)
    await interaction.response.send_message(f"Kicked {member.mention}.")
    await send_log(interaction.guild, audit_embed("Member kicked", f"Member: {member.mention}\nModerator: {interaction.user.mention}\nReason: {reason}", discord.Colour.orange()))


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason")
@guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I cannot ban that member because of role hierarchy.", ephemeral=True)
    await member.ban(reason=reason, delete_message_seconds=0)
    await interaction.response.send_message(f"Banned {member.mention}.")
    await send_log(interaction.guild, audit_embed("Member banned", f"Member: {member.mention}\nModerator: {interaction.user.mention}\nReason: {reason}", discord.Colour.red()))


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(member="Member to timeout", minutes="Duration from 1 to 40320 minutes", reason="Reason")
@guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    await member.timeout(timedelta(minutes=minutes), reason=reason)
    await interaction.response.send_message(f"Timed out {member.mention} for {minutes} minutes.")
    await send_log(interaction.guild, audit_embed("Member timed out", f"Member: {member.mention}\nDuration: {minutes} minutes\nReason: {reason}", discord.Colour.orange()))


@bot.tree.command(name="purge", description="Delete recent messages")
@app_commands.describe(amount="Number of messages from 1 to 100")
@guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "I am missing one or more permissions needed for this action."
    elif isinstance(error, app_commands.CheckFailure):
        message = "This command can only be used inside a server."
    else:
        message = "The command failed. Check the bot console for details."
        print(f"Command error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.run(TOKEN)
