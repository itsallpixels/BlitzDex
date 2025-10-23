import discord
from discord.ext import commands, tasks
from discord import app_commands, File, ui
import os
from dotenv import load_dotenv
import random
import csv
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
import asyncio
from typing import Union
import traceback

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

MIN_SPAWN_INTERVAL = 10
MAX_SPAWN_INTERVAL = 30

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.realpath(__file__)))
print(f"Using data directory: {DATA_DIR}")

CLAIMS_CSV_FILE = os.path.join(DATA_DIR, "card_claims.csv")
INVENTORY_CSV_FILE = os.path.join(DATA_DIR, "user_inventories.csv")
CONFIG_FILE = os.path.join(DATA_DIR, "server_configs.json")

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PREFIX_WEIGHTS_CSV_FILE = os.path.join(SCRIPT_DIR, "prefix_weights.csv")
CARD_NAMES_CSV_FILE = os.path.join(SCRIPT_DIR, "card_names.csv")
CARDS_PATH = os.path.join(SCRIPT_DIR, "cards")
THUMBNAILS_PATH = os.path.join(SCRIPT_DIR, "thumbnails")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="$", intents=intents)

ALL_CARDS = []
PREFIX_WEIGHTS = {}
CARD_ANSWERS = {}
SERVER_CONFIGS = {}
RECENTLY_SPAWNED = deque(maxlen=10)

# --- 2. HELPER FUNCTIONS ---

def load_configs():
    global SERVER_CONFIGS
    try:
        with open(CONFIG_FILE, 'r') as f: SERVER_CONFIGS = json.load(f)
        print(f"Loaded configs for {len(SERVER_CONFIGS)} server(s).")
    except (FileNotFoundError, json.JSONDecodeError):
        SERVER_CONFIGS = {}; print("No config file found. Starting fresh.")

def save_configs():
    with open(CONFIG_FILE, 'w') as f: json.dump(SERVER_CONFIGS, f, indent=4)

def add_card_to_inventory(user: discord.User, card_name: str):
    with open(INVENTORY_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([user.id, user.name, card_name])
    print(f"Added '{card_name}' to {user.name}'s inventory.")

def remove_card_from_inventory(user_id: int, card_name_to_remove: str) -> bool:
    lines, card_removed = [], False
    try:
        with open(INVENTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f: lines = list(csv.reader(f))
    except FileNotFoundError: return False
    with open(INVENTORY_CSV_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if lines:
            writer.writerow(lines[0])
            for row in lines[1:]:
                if str(user_id) == row[0] and card_name_to_remove.lower() == row[2].lower() and not card_removed:
                    card_removed = True; continue
                writer.writerow(row)
    return card_removed

def get_user_inventory(user_id: int) -> defaultdict:
    inventory = defaultdict(int)
    try:
        with open(INVENTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('user_id') and row['user_id'].strip().isdigit() and int(row['user_id']) == user_id:
                    inventory[row['card_name']] += 1
    except (FileNotFoundError, ValueError, KeyError): return inventory
    return inventory

# --- 3. CORE LOADING FUNCTIONS ---

def load_prefix_weights():
    print(f"Loading weights from {PREFIX_WEIGHTS_CSV_FILE}...")
    try:
        with open(PREFIX_WEIGHTS_CSV_FILE, mode='r', encoding='utf-8') as infile:
            reader = csv.reader(infile); next(reader)
            for row in reader:
                prefix, weight_str = row
                try: PREFIX_WEIGHTS[prefix.strip()] = int(weight_str)
                except ValueError: print(f"Warning: Bad weight for prefix '{prefix}'.")
        print(f"Loaded weights for {len(PREFIX_WEIGHTS)} prefixes.")
    except FileNotFoundError: print(f"FATAL ERROR: '{PREFIX_WEIGHTS_CSV_FILE}' not found.")

def load_card_names():
    print(f"Loading names from {CARD_NAMES_CSV_FILE}...")
    try:
        with open(CARD_NAMES_CSV_FILE, mode='r', encoding='utf-8') as infile:
            reader = csv.reader(infile); next(reader)
            for row in reader:
                filename, answers = row[0].strip(), [ans.strip() for ans in row[1:] if ans.strip()]
                if filename and answers: CARD_ANSWERS[filename] = answers
        print(f"Loaded names for {len(CARD_ANSWERS)} cards.")
    except FileNotFoundError: print(f"FATAL ERROR: '{CARD_NAMES_CSV_FILE}' not found.")

def load_cards():
    print("Loading and verifying cards from folders...")
    if not os.path.isdir(CARDS_PATH):
        print(f"FATAL ERROR: 'cards' directory not found at {CARDS_PATH}."); return
    if not os.path.isdir(THUMBNAILS_PATH):
        print(f"FATAL ERROR: 'thumbnails' directory not found at {THUMBNAILS_PATH}."); return
    for filename in os.listdir(CARDS_PATH):
        if not filename.endswith(".png") or '_' not in filename: continue
        if filename not in CARD_ANSWERS:
            print(f"WARNING: Skipping '{filename}' because it's not defined in {CARD_NAMES_CSV_FILE}."); continue
        full_path = os.path.join(CARDS_PATH, filename)
        thumb_path = os.path.join(THUMBNAILS_PATH, f"{filename.replace('.png', '')}_thumb.png")
        if not os.path.exists(full_path):
            print(f"WARNING: Skipping '{filename}' because main image is missing."); continue
        if not os.path.exists(thumb_path):
            print(f"WARNING: Skipping '{filename}' because thumbnail is missing."); continue
        prefix, answers = filename.split('_', 1)[0], CARD_ANSWERS[filename]
        weight = PREFIX_WEIGHTS.get(prefix, 1)
        card_info = {"main_name": answers[0], "all_answers": answers, "weight": weight, "full_path": full_path, "thumb_path": thumb_path}
        ALL_CARDS.append(card_info)
    print(f"Successfully loaded and verified {len(ALL_CARDS)} card files.")

def log_card_claim(user: discord.User, card_name: str):
    header, row = ["timestamp", "user_id", "username", "card_name"], [datetime.now(timezone.utc).isoformat(), user.id, user.name, card_name]
    file_exists = os.path.exists(CLAIMS_CSV_FILE)
    with open(CLAIMS_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists: writer.writerow(header)
        writer.writerow(row)
    print(f"Logged claim: {user.name} claimed {card_name}")

# --- 4. DISCORD UI COMPONENTS ---

class GuessingModal(ui.Modal, title="Guess the Card!"):
    def __init__(self, spawn_view):
        super().__init__(); self.spawn_view = spawn_view
    guess = ui.TextInput(label="Card Name", placeholder="Type your guess here...")
    async def on_submit(self, interaction: discord.Interaction):
        user_guess = self.guess.value.strip().lower()
        if user_guess in self.spawn_view.correct_answers_list:
            if self.spawn_view.claimed:
                await interaction.response.send_message("Someone just beat you to it!", ephemeral=True); return
            self.spawn_view.claimed = True
            self.spawn_view.stop()
            for child in self.spawn_view.children: child.disabled = True
            await self.spawn_view.message.edit(view=self.spawn_view)
            main_name = self.spawn_view.main_display_name
            await interaction.response.send_message(f"✅ Correct! {interaction.user.mention} guessed **{main_name}**!", ephemeral=True)
            log_card_claim(interaction.user, main_name)
            add_card_to_inventory(interaction.user, main_name)
            embed = discord.Embed(title="Card Claimed!", description=f"**{main_name}** was claimed by {interaction.user.mention}!", color=discord.Color.green())
            with open(self.spawn_view.full_card_path, 'rb') as f:
                await interaction.channel.send(content=interaction.user.mention, embed=embed, file=discord.File(f))
        else:
            user_id = interaction.user.id; self.spawn_view.guessers[user_id] += 1
            tries_left = 3 - self.spawn_view.guessers[user_id]
            msg = f"❌ That's not it. You have {tries_left} tries left." if tries_left > 0 else f"❌ Last try. You are locked out."
            await interaction.response.send_message(msg, ephemeral=True)

class SpawnView(ui.View):
    def __init__(self, main_display_name: str, correct_answers_list: list, full_card_path: str):
        super().__init__(timeout=120.0)
        self.main_display_name, self.correct_answers_list, self.full_card_path = main_display_name, [a.lower() for a in correct_answers_list], full_card_path
        self.guessers, self.message, self.claimed = defaultdict(int), None, False
    @ui.button(label="Guess Name", style=discord.ButtonStyle.primary, emoji="❓")
    async def guess_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.guessers[interaction.user.id] >= 3:
            await interaction.response.send_message("You have no more tries for this card.", ephemeral=True); return
        await interaction.response.send_modal(GuessingModal(self))
    async def on_timeout(self):
        if self.claimed: return
        for child in self.children: child.disabled = True
        embed = discord.Embed(title="Card Despawned!", description=f"Nobody claimed **{self.main_display_name}** in time.", color=discord.Color.light_grey())
        if self.message: await self.message.edit(embed=embed, view=self)
        print(f"Card '{self.main_display_name}' despawned.")

# --- 5. SPAWN LOGIC AND SLASH COMMANDS ---

async def do_spawn(source, specific_card_name: str = None):
    if not ALL_CARDS:
        if isinstance(source, discord.Interaction):
            await source.followup.send("Card data isn't loaded.", ephemeral=True)
        return

    chosen_card = None
    if specific_card_name:
        chosen_card = next((card for card in ALL_CARDS if card['main_name'].lower() == specific_card_name.lower()), None)
    else:
        eligible_cards = [card for card in ALL_CARDS if card['main_name'] not in RECENTLY_SPAWNED]
        if not eligible_cards:
            print("All cards recently spawned. Spawning from full pool as fallback.")
            eligible_cards = ALL_CARDS
        eligible_weights = [card['weight'] for card in eligible_cards]
        chosen_card = random.choices(eligible_cards, weights=eligible_weights, k=1)[0]
    
    if not chosen_card:
        if isinstance(source, discord.Interaction):
            await source.followup.send(f"Could not find a card named '{specific_card_name}'.", ephemeral=True)
        print(f"Attempted to spawn non-existent card: {specific_card_name}")
        return

    RECENTLY_SPAWNED.append(chosen_card['main_name'])
    embed = discord.Embed(title="A Wild Card Has Appeared!", description="Click the button and guess its name!", color=discord.Color.blue())
    view = SpawnView(main_display_name=chosen_card['main_name'], correct_answers_list=chosen_card['all_answers'], full_card_path=chosen_card['full_path'])
    
    try:
        with open(chosen_card['thumb_path'], 'rb') as f:
            picture = discord.File(f)
            if isinstance(source, discord.Interaction):
                message = await source.followup.send(embed=embed, file=picture, view=view, wait=True)
            else:
                message = await source.send(embed=embed, file=picture, view=view)
            view.message = message
    except FileNotFoundError:
        print(f"CRITICAL RUNTIME ERROR: Thumbnail not found for a verified card: {chosen_card['thumb_path']}")

# --- MODIFIED: The timed task is now more robust ---
@tasks.loop(seconds=30)
async def timed_spawn_checker():
    now = datetime.now(timezone.utc)
    for guild_id_str, config in list(SERVER_CONFIGS.items()):
        try: # <-- Error handling is now INSIDE the loop for each server
            channel_id = config.get("spawn_channel_id")
            next_spawn_time_str = config.get("next_spawn_time")
            
            if not (channel_id and next_spawn_time_str):
                continue

            next_spawn_time = datetime.fromisoformat(next_spawn_time_str)

            if now >= next_spawn_time:
                channel = bot.get_channel(channel_id)
                if channel:
                    print(f"Spawning card for server {guild_id_str}...")
                    await do_spawn(channel)
                    
                    next_interval = random.randint(MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL)
                    new_next_spawn_time = datetime.now(timezone.utc) + timedelta(minutes=next_interval)
                    SERVER_CONFIGS[guild_id_str]["next_spawn_time"] = new_next_spawn_time.isoformat()
                    print(f"Next spawn for {guild_id_str} in {next_interval} minutes.")
                else:
                    print(f"Could not find configured channel {channel_id} for server {guild_id_str}.")
        
        except Exception: # Catch any and all errors for this specific server
            print(f"--- UNHANDLED EXCEPTION FOR SERVER {guild_id_str} ---")
            traceback.print_exc()
            print("--- Continuing to next server ---")

    save_configs() # Save configs once at the end of all checks

# --- COMMANDS ---

@bot.tree.command(name="ping", description="Replies with the bot's latency.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! `({round(bot.latency * 1000)}ms)`")

def has_spawn_permission(interaction: discord.Interaction) -> bool:
    user = interaction.user; guild_id = str(interaction.guild.id)
    config = SERVER_CONFIGS.get(guild_id, {}); allowed_ids = config.get("spawn_allowed_ids", [])
    if user.guild_permissions.manage_guild: return True
    if user.id in allowed_ids: return True
    if any(role.id in allowed_ids for role in user.roles): return True
    return False

@bot.tree.command(name="spawn", description="Manually spawns a random card.")
async def manual_spawn(interaction: discord.Interaction):
    if not has_spawn_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True); return
    await interaction.response.defer()
    await do_spawn(interaction)

@bot.tree.command(name="spawn_card", description="Manually spawns a specific card.")
@app_commands.describe(card_name="The name of the card to spawn.")
async def specific_spawn(interaction: discord.Interaction, card_name: str):
    if not has_spawn_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True); return
    await interaction.response.defer()
    await do_spawn(interaction, specific_card_name=card_name)

@specific_spawn.autocomplete('card_name')
async def specific_spawn_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    all_card_names = [card['main_name'] for card in ALL_CARDS]
    return [app_commands.Choice(name=name, value=name) for name in all_card_names if current.lower() in name.lower()][:25]

@bot.tree.command(name="inventory", description="Check your or another user's card inventory.")
@app_commands.describe(user="The user whose inventory you want to see (optional).")
async def inventory(interaction: discord.Interaction, user: discord.Member = None):
    target_user, inv = user or interaction.user, get_user_inventory((user or interaction.user).id)
    embed = discord.Embed(title=f"{target_user.display_name}'s Inventory", color=discord.Color.blurple())
    desc = f"Total cards to collect: {len(CARD_ANSWERS)}\n\n"
    if not inv: desc += "This inventory is empty."
    else:
        card_list = "".join([f"**{name}** `x{count}`\n" for name, count in sorted(inv.items())])
        desc += f"**Unique Cards: {len(inv)}**\n\n{card_list}"
    embed.description = desc
    embed.set_thumbnail(url=target_user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="give", description="Give one of your cards to another user.")
@app_commands.describe(user="The user you want to give a card to.", card_name="The name of the card you are giving.")
async def give(interaction: discord.Interaction, user: discord.Member, card_name: str):
    if user.bot or user == interaction.user:
        await interaction.response.send_message("You can't give cards to yourself or a bot.", ephemeral=True); return
    if remove_card_from_inventory(interaction.user.id, card_name):
        add_card_to_inventory(user, card_name)
        await interaction.response.send_message(f"You have given **{card_name}** to {user.mention}.")
    else: await interaction.response.send_message(f"You don't have **{card_name}** to give.", ephemeral=True)

@give.autocomplete('card_name')
async def give_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=card, value=card) for card in get_user_inventory(interaction.user.id) if current.lower() in card.lower()][:25]

card_group = app_commands.Group(name="card", description="Commands related to viewing your cards.")
@card_group.command(name="view", description="View a specific card you own.")
@app_commands.describe(card_name="The name of the card you want to see.")
async def card_view(interaction: discord.Interaction, card_name: str):
    if card_name not in get_user_inventory(interaction.user.id):
        await interaction.response.send_message("You do not own that card.", ephemeral=True); return
    card_to_show = next((card for card in ALL_CARDS if card['main_name'] == card_name), None)
    if not card_to_show:
        await interaction.response.send_message("Error finding that card's image.", ephemeral=True); return
    embed = discord.Embed(title=f"{interaction.user.display_name} is viewing:", description=f"**{card_name}**", color=discord.Color.dark_gold())
    with open(card_to_show['full_path'], 'rb') as f:
        picture = discord.File(f)
        await interaction.response.send_message(embed=embed, file=picture)

@card_view.autocomplete('card_name')
async def card_view_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=card, value=card) for card in get_user_inventory(interaction.user.id) if current.lower() in card.lower()][:25]
bot.tree.add_command(card_group)

config_group = app_commands.Group(name="config", description="Admin commands for this server.", default_permissions=discord.Permissions(manage_guild=True))
@config_group.command(name="spawn_channel", description="Set the channel where cards will automatically spawn.")
@app_commands.describe(channel="The text channel to set for spawns.")
async def set_spawn_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    if guild_id not in SERVER_CONFIGS: SERVER_CONFIGS[guild_id] = {}
    SERVER_CONFIGS[guild_id]["spawn_channel_id"] = channel.id
    if "next_spawn_time" not in SERVER_CONFIGS[guild_id]:
        first_interval = random.randint(MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL)
        first_spawn_time = datetime.now(timezone.utc) + timedelta(minutes=first_interval)
        SERVER_CONFIGS[guild_id]["next_spawn_time"] = first_spawn_time.isoformat()
        await interaction.response.send_message(f"✅ Spawn channel set. First card will spawn in ~{first_interval} minutes.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Spawn channel updated to {channel.mention}.", ephemeral=True)
    save_configs()

@config_group.command(name="allow_spawn", description="Allow a user or role to use the /spawn command.")
@app_commands.describe(target="The user or role to grant permission to.")
async def allow_spawn(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    guild_id = str(interaction.guild.id)
    if guild_id not in SERVER_CONFIGS: SERVER_CONFIGS[guild_id] = {}
    if "spawn_allowed_ids" not in SERVER_CONFIGS[guild_id]: SERVER_CONFIGS[guild_id]["spawn_allowed_ids"] = []
    if target.id not in SERVER_CONFIGS[guild_id]["spawn_allowed_ids"]:
        SERVER_CONFIGS[guild_id]["spawn_allowed_ids"].append(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} can now use `/spawn`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} already has permission.", ephemeral=True)

@config_group.command(name="deny_spawn", description="Revoke a user or role's permission to use /spawn.")
@app_commands.describe(target="The user or role to revoke permission from.")
async def deny_spawn(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    guild_id = str(interaction.guild.id)
    if guild_id not in SERVER_CONFIGS or "spawn_allowed_ids" not in SERVER_CONFIGS[guild_id]:
        await interaction.response.send_message("⚠️ No custom spawn permissions are set.", ephemeral=True); return
    if target.id in SERVER_CONFIGS[guild_id]["spawn_allowed_ids"]:
        SERVER_CONFIGS[guild_id]["spawn_allowed_ids"].remove(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} can no longer use `/spawn`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} did not have custom permission.", ephemeral=True)

@config_group.command(name="view_spawn_permissions", description="View who has custom permission to use /spawn.")
async def view_spawn_permissions(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    allowed_ids = SERVER_CONFIGS.get(guild_id, {}).get("spawn_allowed_ids", [])
    desc = "**Users with `Manage Server` permission can always use `/spawn`.**\n\n**Custom Permissions:**\n"
    desc += "\n".join([f"- <@&{entity_id}> / <@{entity_id}>" for entity_id in allowed_ids]) if allowed_ids else "None set."
    embed = discord.Embed(title="`/spawn` Command Permissions", description=desc, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)
bot.tree.add_command(config_group)

# --- 6. BOT STARTUP EVENT ---

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})'); print('------')
    load_configs()
    load_prefix_weights()
    load_card_names()
    load_cards()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e: print(e)
    timed_spawn_checker.start()

# --- 7. RUN THE BOT ---
bot.run(TOKEN)