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
import shutil

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
OWNER_ID = 803113397213462588
MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL = 10, 30
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.realpath(__file__)))
print(f"Using data directory: {DATA_DIR}")

# --- File Paths ---
CLAIMS_CSV_FILE = os.path.join(DATA_DIR, "card_claims.csv")
INVENTORY_CSV_FILE = os.path.join(DATA_DIR, "user_inventories.csv")
CONFIG_FILE = os.path.join(DATA_DIR, "server_configs.json")
SPAWN_HISTORY_CSV_FILE = os.path.join(DATA_DIR, "spawn_history.csv")
STEAL_LOG_CSV_FILE = os.path.join(DATA_DIR, "steal_log.csv")

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PREFIX_WEIGHTS_CSV_FILE = os.path.join(SCRIPT_DIR, "prefix_weights.csv")
CARD_NAMES_CSV_FILE = os.path.join(SCRIPT_DIR, "card_names.csv")
CARDS_PATH = os.path.join(SCRIPT_DIR, "cards")
THUMBNAILS_PATH = os.path.join(SCRIPT_DIR, "thumbnails")

# --- REBALANCED GAMEPLAY CONSTANTS ---
RARITY_VALUES = {
    "C": 1, "UC": 2, "R": 10, "UR": 20, "SR": 30, "L": 40, "K": 50,
    "CR": 80, "IT": 80, "D": 80, "OMN": 80, "P": 80, "RTX": 80
}
STEALABLE_RARITIES = ["R", "UR", "SR", "L", "K", "CR", "IT", "D", "OMN", "P", "RTX"]
MAX_RARITY_VALUE_DIFFERENCE = 70
BASE_STEAL_CHANCE = 50.0
MIN_STEAL_CHANCE = 5.0
MAX_NORMAL_STEAL_CHANCE = 75.0 
STOLEN_BONUS_CHANCE = 15.0
ABSOLUTE_MAX_STEAL_CHANCE = 95.0 
STEAL_COOLDOWN_HOURS = 1
DAILY_SPAWN_LIMIT = 2 # A card can only spawn this many times per day per server

# --- Bot & Global Variables ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="$", intents=intents)
ALL_CARDS, PREFIX_WEIGHTS, CARD_ANSWERS, SERVER_CONFIGS = [], {}, {}, {}
CARD_RARITY_MAP = {}
RECENTLY_SPAWNED = defaultdict(lambda: deque(maxlen=10))

# --- 2. HELPER FUNCTIONS ---
def ensure_data_files_exist():
    if not os.path.exists(INVENTORY_CSV_FILE):
        with open(INVENTORY_CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["user_id", "username", "card_name", "is_stolen", "unique_id"])
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f: json.dump({}, f)
    if not os.path.exists(CLAIMS_CSV_FILE):
        with open(CLAIMS_CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["timestamp", "user_id", "username", "card_name"])
    if not os.path.exists(SPAWN_HISTORY_CSV_FILE):
        with open(SPAWN_HISTORY_CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["timestamp", "guild_id", "card_name"])
    if not os.path.exists(STEAL_LOG_CSV_FILE):
        with open(STEAL_LOG_CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["unique_id", "original_owner_id"])

def safe_atomic_write_json(filepath, data):
    temp_file = filepath + ".tmp"
    with open(temp_file, 'w') as f: json.dump(data, f, indent=4)
    shutil.move(temp_file, filepath)

def safe_atomic_write_csv(filepath, lines):
    temp_file = filepath + ".tmp"
    with open(temp_file, 'w', newline='', encoding='utf-8') as f_out:
        writer = csv.writer(f_out); writer.writerows(lines)
    shutil.move(temp_file, filepath)

def load_configs():
    global SERVER_CONFIGS
    try:
        with open(CONFIG_FILE, 'r') as f: SERVER_CONFIGS = json.load(f)
        print(f"Loaded configs for {len(SERVER_CONFIGS)} server(s).")
    except (FileNotFoundError, json.JSONDecodeError):
        SERVER_CONFIGS = {}; print("No config file found.")

def save_configs():
    safe_atomic_write_json(CONFIG_FILE, SERVER_CONFIGS)

async def send_approval_dm(guild: discord.Guild) -> bool:
    try:
        owner = await bot.fetch_user(OWNER_ID)
        embed = discord.Embed(title="Server Approval Request", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Server Name", value=guild.name, inline=False)
        embed.add_field(name="Server ID", value=str(guild.id), inline=False)
        embed.add_field(name="Members", value=guild.member_count, inline=False)
        embed.set_footer(text=f"Server Owner: {guild.owner}")
        await owner.send(embed=embed, view=ApprovalView())
        print(f"Sent approval request DM to owner for guild {guild.id}.")
        return True
    except discord.Forbidden:
        print(f"CRITICAL: Could not DM owner with ID {OWNER_ID}. Check their privacy settings.")
    except discord.NotFound:
        print(f"CRITICAL: Could not find owner with ID {OWNER_ID}. Is the ID correct?")
    return False

def add_card_to_inventory(user: discord.User, card_name: str, is_stolen: bool = False, unique_id: str = None) -> str:
    if unique_id is None:
        unique_id = f"{card_name}-{datetime.now(timezone.utc).timestamp()}-{random.randint(1000,9999)}"
    with open(INVENTORY_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([user.id, user.name, card_name, 'True' if is_stolen else '', unique_id])
    print(f"Added '{card_name}' (ID: {unique_id}, Stolen: {is_stolen}) to {user.name}'s inventory.")
    return unique_id

def remove_card_from_inventory(user_id: int, card_name_to_remove: str) -> dict:
    lines, card_removed, removed_card_data = [], False, None
    try:
        with open(INVENTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f_in: lines = list(csv.reader(f_in))
    except FileNotFoundError: return None
    new_lines = [lines[0]] if lines else []
    for row in lines[1:]:
        if not card_removed and len(row) >= 5 and str(user_id) == row[0] and card_name_to_remove.lower() == row[2].lower():
            card_removed = True
            removed_card_data = {"user_id": row[0], "username": row[1], "card_name": row[2], "is_stolen": row[3], "unique_id": row[4]}
        else:
            new_lines.append(row)
    if card_removed: safe_atomic_write_csv(INVENTORY_CSV_FILE, new_lines)
    return removed_card_data

def get_user_inventory(user_id: int) -> list:
    inventory = []
    try:
        with open(INVENTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('user_id') and row['user_id'].strip().isdigit() and int(row['user_id']) == user_id:
                    is_stolen_raw = row.get('is_stolen')
                    is_stolen_value = is_stolen_raw.strip().lower() == 'true' if is_stolen_raw else False
                    inventory.append({"name": row['card_name'], "is_stolen": is_stolen_value, "unique_id": row.get('unique_id')})
    except (FileNotFoundError, ValueError, KeyError): return []
    return inventory

def log_original_owner(unique_id: str, owner_id: int):
    with open(STEAL_LOG_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([unique_id, owner_id])

def get_original_owner(unique_id: str) -> int:
    if not unique_id: return None
    try:
        with open(STEAL_LOG_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('unique_id') == unique_id:
                    return int(row.get('original_owner_id'))
    except (FileNotFoundError, KeyError, ValueError): return None
    return None

# --- 3. CORE LOADING FUNCTIONS ---
def load_spawn_history():
    global RECENTLY_SPAWNED
    try:
        with open(SPAWN_HISTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('guild_id') and row.get('card_name'):
                    RECENTLY_SPAWNED[int(row['guild_id'])].append(row['card_name'])
        print(f"Loaded spawn history for {len(RECENTLY_SPAWNED)} server(s).")
    except (FileNotFoundError, KeyError, ValueError): print("No spawn history file found.")

def load_cards():
    print("Loading and verifying cards...")
    if not all(os.path.isdir(p) for p in [CARDS_PATH, THUMBNAILS_PATH]):
        print(f"FATAL ERROR: 'cards' or 'thumbnails' directory not found."); return
    for filename in os.listdir(CARDS_PATH):
        if not (filename.endswith(".png") and '_' in filename): continue
        if filename not in CARD_ANSWERS: continue
        
        full_path = os.path.join(CARDS_PATH, filename)
        thumb_path = os.path.join(THUMBNAILS_PATH, f"{filename.replace('.png', '')}_thumb.png")

        if not os.path.exists(full_path):
            continue
            
        if not os.path.exists(thumb_path):
            print(f"  [MISSING THUMBNAIL] Skipping '{filename}'. Expected thumbnail not found at: {thumb_path}")
            continue

        prefix, answers, main_name = filename.split('_', 1)[0], CARD_ANSWERS[filename], CARD_ANSWERS[filename][0]
        weight = PREFIX_WEIGHTS.get(prefix, 1)
        card_info = {"main_name": main_name, "all_answers": answers, "weight": weight, "full_path": full_path, "thumb_path": thumb_path}
        ALL_CARDS.append(card_info)
        CARD_RARITY_MAP[main_name] = prefix
    print(f"Successfully loaded and verified {len(ALL_CARDS)} card files.")

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

def log_card_claim(user: discord.User, card_name: str):
    with open(CLAIMS_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), user.id, user.name, card_name])
    print(f"Logged claim: {user.name} claimed {card_name}")

def log_spawn(guild_id: int, card_name: str):
    with open(SPAWN_HISTORY_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), guild_id, card_name])
    print(f"Logged spawn: '{card_name}' in guild {guild_id}")

# --- 4. DISCORD UI COMPONENTS ---
class ApprovalView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="persistent_approve")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        try:
            guild_id_str = next(field.value for field in interaction.message.embeds[0].fields if field.name == "Server ID")
            guild_id = int(guild_id_str)
        except (IndexError, StopIteration, ValueError):
            await interaction.response.send_message("Error: Could not find a valid Guild ID in the message.", ephemeral=True); return

        SERVER_CONFIGS.setdefault(guild_id_str, {})['is_approved'] = True
        save_configs()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content=f"✅ Server `{guild_id_str}` has been **approved**.", view=self)

        if guild := bot.get_guild(guild_id):
            channel = guild.system_channel or next((ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), None)
            if channel:
                try: await channel.send("🎉 This server has been approved by the bot owner! BlitzDex is now fully functional.")
                except discord.Forbidden: pass

    @ui.button(label="Deny & Leave", style=discord.ButtonStyle.danger, custom_id="persistent_deny")
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        try:
            guild_id_str = next(field.value for field in interaction.message.embeds[0].fields if field.name == "Server ID")
            guild_id = int(guild_id_str)
        except (IndexError, StopIteration, ValueError):
            await interaction.response.send_message("Error: Could not find a valid Guild ID in the message.", ephemeral=True); return

        if guild := bot.get_guild(guild_id):
            try: await guild.owner.send(f"Hello! Your request to add BlitzDex to '{guild.name}' has been denied. The bot will now leave.")
            except discord.Forbidden: print(f"Could not DM owner of guild {guild_id_str}.")
            await guild.leave()
            print(f"Denied and left guild {guild_id_str}.")
        if guild_id_str in SERVER_CONFIGS:
            del SERVER_CONFIGS[guild_id_str]
            save_configs()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content=f"❌ Server `{guild_id_str}` has been **denied** and the bot has left.", view=self)

class GuessingModal(ui.Modal, title="Guess the Card!"):
    def __init__(self, spawn_view):
        super().__init__(); self.spawn_view = spawn_view
    guess = ui.TextInput(label="Card Name", placeholder="Type your guess here...")
    async def on_submit(self, interaction: discord.Interaction):
        user_guess = self.guess.value.strip().lower()
        if user_guess in self.spawn_view.correct_answers_list:
            if self.spawn_view.claimed:
                await interaction.response.send_message("Someone just beat you to it!", ephemeral=True); return
            self.spawn_view.claimed = True; self.spawn_view.stop()
            for child in self.spawn_view.children: child.disabled = True
            await self.spawn_view.message.edit(view=self.spawn_view)
            main_name = self.spawn_view.main_display_name
            await interaction.response.send_message(f"✅ Correct! {interaction.user.mention} guessed **{main_name}**!", ephemeral=True)
            log_card_claim(interaction.user, main_name)
            unique_id = add_card_to_inventory(interaction.user, main_name, is_stolen=False)
            log_original_owner(unique_id, interaction.user.id)
            embed = discord.Embed(title="Card Claimed!", description=f"**{main_name}** was claimed by {interaction.user.mention}!", color=discord.Color.green())
            with open(self.spawn_view.full_card_path, 'rb') as f:
                await interaction.channel.send(content=interaction.user.mention, embed=embed, file=discord.File(f))
        else:
            self.spawn_view.guessers[interaction.user.id] += 1
            tries_left = 3 - self.spawn_view.guessers[interaction.user.id]
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

class StealConfirmView(ui.View):
    def __init__(self, thief: discord.Member, victim: discord.Member, target_card: dict, leveraged_card: dict, interaction: discord.Interaction):
        super().__init__(timeout=60.0)
        self.thief, self.victim, self.target_card, self.leveraged_card = thief, victim, target_card, leveraged_card
        self.original_interaction = interaction
    async def on_timeout(self):
        for item in self.children: item.disabled = True
        try: await self.original_interaction.edit_original_response(content="Steal attempt timed out.", view=self)
        except discord.NotFound: pass
    @ui.button(label="Confirm Steal", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.thief.id:
            await interaction.response.send_message("This is not your decision.", ephemeral=True); return
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(view=self)
        
        leverage_prefix = CARD_RARITY_MAP.get(self.leveraged_card['name'])
        target_prefix = CARD_RARITY_MAP.get(self.target_card['name'])
        target_value = RARITY_VALUES.get(target_prefix, 0)
        
        base_chance = 0.0

        if leverage_prefix == "R" and target_value >= RARITY_VALUES["L"]:
            base_chance = 5.0
        elif leverage_prefix == "UR" and target_value >= RARITY_VALUES["L"]:
            base_chance = 15.0
        elif leverage_prefix == "SR" and target_value >= RARITY_VALUES["L"]:
            base_chance = 20.0
        else:
            leverage_value = RARITY_VALUES.get(leverage_prefix, 0)
            diff = leverage_value - target_value
            
            calculated_chance = BASE_STEAL_CHANCE
            if diff > 0:
                advantage_range = MAX_NORMAL_STEAL_CHANCE - BASE_STEAL_CHANCE
                calculated_chance += (diff / MAX_RARITY_VALUE_DIFFERENCE) * advantage_range
            elif diff < 0:
                disadvantage_range = BASE_STEAL_CHANCE - MIN_STEAL_CHANCE
                calculated_chance -= (abs(diff) / MAX_RARITY_VALUE_DIFFERENCE) * disadvantage_range
            base_chance = max(MIN_STEAL_CHANCE, min(calculated_chance, MAX_NORMAL_STEAL_CHANCE))

        final_chance = base_chance
        if get_original_owner(self.target_card.get('unique_id')) == self.thief.id:
            final_chance += STOLEN_BONUS_CHANCE
        final_chance = min(final_chance, ABSOLUTE_MAX_STEAL_CHANCE)
        
        roll = random.uniform(0, 100)

        if roll <= final_chance:
            if removed_card := remove_card_from_inventory(self.victim.id, self.target_card['name']):
                add_card_to_inventory(self.thief, removed_card['card_name'], is_stolen=True, unique_id=removed_card['unique_id'])
                embed = discord.Embed(title="Steal Successful!", color=discord.Color.green(), description=f"({roll:.1f} rolled, ≤ {final_chance:.1f} needed)\n{self.thief.mention} stole **{self.target_card['name']}** from {self.victim.mention}!")
            else:
                 embed = discord.Embed(title="Steal Error!", color=discord.Color.yellow(), description="The card vanished from the victim's inventory.")
        else:
            remove_card_from_inventory(self.thief.id, self.leveraged_card['name'])
            embed = discord.Embed(title="Steal Failed!", color=discord.Color.red(), description=f"({roll:.1f} rolled, ≤ {final_chance:.1f} needed)\n{self.thief.mention} failed and lost their **{self.leveraged_card['name']}**!")
        
        await interaction.followup.send(embed=embed)
    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.thief.id:
            await interaction.response.send_message("This is not your decision.", ephemeral=True); return
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content="Steal attempt cancelled.", view=self)

class LeverageSelect(ui.Select):
    def __init__(self, thief_inv: list, victim: discord.Member, target_card: dict):
        self.thief_inv, self.victim, self.target_card = thief_inv, victim, target_card
        
        unique_names = sorted({card['name'] for card in thief_inv})
        options = [discord.SelectOption(label=name) for name in unique_names]
        
        super().__init__(placeholder="Choose an eligible card to risk...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        leveraged_card = next(c for c in self.thief_inv if c['name'] == self.values[0])
        
        leverage_prefix = CARD_RARITY_MAP.get(leveraged_card['name'])
        target_prefix = CARD_RARITY_MAP.get(self.target_card['name'])
        target_value = RARITY_VALUES.get(target_prefix, 0)
        
        base_chance = 0.0

        if leverage_prefix == "R" and target_value >= RARITY_VALUES["L"]:
            base_chance = 5.0
        elif leverage_prefix == "UR" and target_value >= RARITY_VALUES["L"]:
            base_chance = 15.0
        elif leverage_prefix == "SR" and target_value >= RARITY_VALUES["L"]:
            base_chance = 20.0
        else:
            leverage_value = RARITY_VALUES.get(leverage_prefix, 0)
            diff = leverage_value - target_value
            
            calculated_chance = BASE_STEAL_CHANCE
            if diff > 0:
                advantage_range = MAX_NORMAL_STEAL_CHANCE - BASE_STEAL_CHANCE
                calculated_chance += (diff / MAX_RARITY_VALUE_DIFFERENCE) * advantage_range
            elif diff < 0:
                disadvantage_range = BASE_STEAL_CHANCE - MIN_STEAL_CHANCE
                calculated_chance -= (abs(diff) / MAX_RARITY_VALUE_DIFFERENCE) * disadvantage_range
            base_chance = max(MIN_STEAL_CHANCE, min(calculated_chance, MAX_NORMAL_STEAL_CHANCE))

        final_chance = base_chance
        bonus_text = ""
        if get_original_owner(self.target_card.get('unique_id')) == interaction.user.id:
            final_chance += STOLEN_BONUS_CHANCE
            bonus_text = f" (+{STOLEN_BONUS_CHANCE}% Owner Bonus)"
        
        final_chance = min(final_chance, ABSOLUTE_MAX_STEAL_CHANCE)
        
        embed = discord.Embed(title="Confirm Steal Attempt", color=discord.Color.orange())
        embed.add_field(name="Target", value=f"Stealing **{self.target_card['name']}** from {self.victim.mention}", inline=False)
        embed.add_field(name="Your Risk", value=f"Leveraging **{leveraged_card['name']}**. You will lose this if you fail.", inline=False)
        embed.add_field(name="Estimated Success Chance", value=f"**~{final_chance:.1f}%**{bonus_text}", inline=False)
        view = StealConfirmView(interaction.user, self.victim, self.target_card, leveraged_card, interaction)
        await interaction.response.edit_message(content=None, embed=embed, view=view)

class LeverageSelectView(ui.View):
    def __init__(self, thief_inv: list, victim: discord.Member, target_card: dict):
        super().__init__(timeout=180.0)
        self.add_item(LeverageSelect(thief_inv, victim, target_card))

# --- 5. SPAWN LOGIC ---
def get_daily_spawn_counts(guild_id: int) -> defaultdict:
    """Counts how many times each card has spawned today in a specific guild."""
    counts = defaultdict(int)
    today = datetime.now(timezone.utc).date()
    try:
        with open(SPAWN_HISTORY_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('guild_id') == str(guild_id):
                    try:
                        spawn_time = datetime.fromisoformat(row['timestamp'])
                        if spawn_time.date() == today:
                            counts[row['card_name']] += 1
                    except (ValueError, TypeError):
                        continue
    except FileNotFoundError:
        pass
    return counts

async def do_spawn(source, guild_id: int, specific_card_name: str = None):
    if not ALL_CARDS:
        if isinstance(source, discord.Interaction): await source.followup.send("Card data isn't loaded.", ephemeral=True)
        return
        
    chosen_card = None
    if specific_card_name:
        chosen_card = next((card for card in ALL_CARDS if card['main_name'].lower() == specific_card_name.lower()), None)
    else:
        history = RECENTLY_SPAWNED[guild_id]
        daily_counts = get_daily_spawn_counts(guild_id)

        eligible_cards = [
            card for card in ALL_CARDS 
            if card['main_name'] not in history 
            and daily_counts[card['main_name']] < DAILY_SPAWN_LIMIT
        ]

        if not eligible_cards:
            print(f"Warning: All cards for guild {guild_id} have hit their daily spawn limit. Spawning from recently-spawned filtered pool only.")
            eligible_cards = [card for card in ALL_CARDS if card['main_name'] not in history]

        if not eligible_cards:
            eligible_cards = ALL_CARDS

        eligible_weights = [card['weight'] for card in eligible_cards]
        chosen_card = random.choices(eligible_cards, weights=eligible_weights, k=1)[0]
            
    if not chosen_card:
        if isinstance(source, discord.Interaction): await source.followup.send(f"Could not find a card to spawn.", ephemeral=True)
        return
        
    RECENTLY_SPAWNED[guild_id].append(chosen_card['main_name'])
    log_spawn(guild_id, chosen_card['main_name'])
    embed = discord.Embed(title="A Wild Card Has Appeared!", description="Click the button and guess its name!", color=discord.Color.blue())
    view = SpawnView(main_display_name=chosen_card['main_name'], correct_answers_list=chosen_card['all_answers'], full_card_path=chosen_card['full_path'])
    try:
        with open(chosen_card['thumb_path'], 'rb') as f:
            picture = discord.File(f)
            message = None
            if isinstance(source, discord.Interaction):
                message = await source.followup.send(embed=embed, file=picture, view=view, wait=True)
            else:
                message = await source.send(embed=embed, file=picture, view=view)
            view.message = message
    except Exception as e:
        print(f"An error occurred during do_spawn message sending: {e}")

@tasks.loop(seconds=30)
async def timed_spawn_checker():
    now = datetime.now(timezone.utc)
    for guild_id_str, config in list(SERVER_CONFIGS.items()):
        if not config.get('is_approved', False): continue
        try:
            channel_id, next_spawn_time_str = config.get("spawn_channel_id"), config.get("next_spawn_time")
            if not (channel_id and next_spawn_time_str): continue
            if now >= datetime.fromisoformat(next_spawn_time_str):
                channel, guild_id = bot.get_channel(channel_id), int(guild_id_str)
                if channel:
                    await do_spawn(channel, guild_id)
                    next_interval = random.randint(MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL)
                    SERVER_CONFIGS[guild_id_str]["next_spawn_time"] = (datetime.now(timezone.utc) + timedelta(minutes=next_interval)).isoformat()
        except Exception:
            print(f"--- UNHANDLED EXCEPTION FOR SERVER {guild_id_str} ---"); traceback.print_exc()
    save_configs()

# --- 6. COMMANDS & CHECKS ---
async def is_server_approved(interaction: discord.Interaction) -> bool:
    guild_id = str(interaction.guild.id)
    if not SERVER_CONFIGS.get(guild_id, {}).get('is_approved', False):
        await interaction.response.send_message(
            "This server is pending approval from the bot owner.\n"
            "An administrator with `Manage Server` permissions can use `/request_approval` to send a notification.", 
            ephemeral=True
        )
        return False
    return True

def has_spawn_permission(interaction: discord.Interaction) -> bool:
    user, guild_id = interaction.user, str(interaction.guild.id)
    config = SERVER_CONFIGS.get(guild_id, {})
    if user.id in config.get("banned_admin_ids", []):
        return False
    if user.guild_permissions.manage_guild:
        return True
    allowed_ids = config.get("spawn_allowed_ids", [])
    if user.id in allowed_ids or any(role.id in allowed_ids for role in user.roles):
        return True
    return False

async def is_banned_bot_admin(interaction: discord.Interaction) -> bool:
    """Checks if a user is on the bot admin ban list."""
    banned_ids = SERVER_CONFIGS.get(str(interaction.guild.id), {}).get("banned_admin_ids", [])
    if interaction.user.id in banned_ids:
        await interaction.response.send_message("❌ You are currently banned from using this bot's admin commands.", ephemeral=True)
        return True
    return False

@bot.tree.command(name="request_approval", description="Sends an approval request to the bot owner (Manage Server perms required).")
async def request_approval(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You must have `Manage Server` permissions to use this command.", ephemeral=True)
        return
    if SERVER_CONFIGS.get(guild_id, {}).get('is_approved', False):
        await interaction.response.send_message("✅ This server has already been approved!", ephemeral=True)
        return
    success = await send_approval_dm(interaction.guild)
    if success:
        await interaction.response.send_message("✅ A new approval request has been sent to the bot owner.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ I was unable to send a message to my owner. Please ask them to check my console logs.", ephemeral=True)

@bot.tree.command(name="ping", description="Replies with the bot's latency.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! `({round(bot.latency * 1000)}ms)`")

@bot.tree.command(name="spawn", description="Manually spawns a random card.")
async def manual_spawn(interaction: discord.Interaction):
    if not await is_server_approved(interaction): return
    if not has_spawn_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True); return
    await interaction.response.defer()
    await do_spawn(interaction, interaction.guild.id)

@bot.tree.command(name="spawn_card", description="Manually spawns a specific card.")
@app_commands.describe(card_name="The name of the card to spawn.")
async def specific_spawn(interaction: discord.Interaction, card_name: str):
    if not await is_server_approved(interaction): return
    if not has_spawn_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True); return
    await interaction.response.defer()
    await do_spawn(interaction, interaction.guild.id, specific_card_name=card_name)
@specific_spawn.autocomplete('card_name')
async def specific_spawn_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    all_card_names = [card['main_name'] for card in ALL_CARDS]
    return [app_commands.Choice(name=name, value=name) for name in all_card_names if current.lower() in name.lower()][:25]

@bot.tree.command(name="inventory", description="Check your or another user's card inventory.")
@app_commands.describe(user="The user whose inventory you want to see.")
async def inventory(interaction: discord.Interaction, user: discord.Member = None):
    if not await is_server_approved(interaction): return
    target_user, inv = user or interaction.user, get_user_inventory((user or interaction.user).id)
    embed = discord.Embed(title=f"{target_user.display_name}'s Inventory", color=discord.Color.blurple())
    desc = f"Total cards to collect: {len(CARD_ANSWERS)}\n\n"
    if not inv: desc += "This inventory is empty."
    else:
        counts = defaultdict(lambda: {'clean': 0, 'stolen': 0})
        for card in inv: counts[card['name']]['stolen' if card['is_stolen'] else 'clean'] += 1
        card_list = "".join(f"**{name}**" + (f" `x{data['clean']}`" if data['clean'] > 0 else "") + (f" 훔 `x{data['stolen']}`" if data['stolen'] > 0 else "") + "\n" for name, data in sorted(counts.items()))
        desc += f"**Unique Cards: {len(counts)}**\n\n{card_list}"
    embed.description = desc
    embed.set_thumbnail(url=target_user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="give", description="Give one of your cards to another user.")
@app_commands.describe(user="The user you want to give a card to.", card_name="The name of the card you are giving.")
async def give(interaction: discord.Interaction, user: discord.Member, card_name: str):
    if not await is_server_approved(interaction): return
    if user.bot or user == interaction.user:
        await interaction.response.send_message("You can't give cards to yourself or a bot.", ephemeral=True); return

    removed_card = remove_card_from_inventory(interaction.user.id, card_name)

    if removed_card:
        card_unique_id = removed_card.get('unique_id')
        original_owner_id = get_original_owner(card_unique_id)
        
        was_stolen = str(removed_card.get('is_stolen')).lower() == 'true'

        is_now_stolen = False
        if was_stolen:
            if original_owner_id is not None and original_owner_id == user.id:
                is_now_stolen = False
            else:
                is_now_stolen = True
        
        add_card_to_inventory(user, card_name, is_stolen=is_now_stolen, unique_id=card_unique_id)

        msg = f"You have given **{card_name}** to {user.mention}."
        if was_stolen and not is_now_stolen:
            msg += " As they are the original owner, its stolen status has been cleansed."
        
        await interaction.response.send_message(msg)
    else: 
        await interaction.response.send_message(f"You don't have **{card_name}** to give.", ephemeral=True)
@give.autocomplete('card_name')
async def give_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    unique_names = sorted({card['name'] for card in get_user_inventory(interaction.user.id)})
    return [app_commands.Choice(name=name, value=name) for name in unique_names if current.lower() in name.lower()][:25]

@bot.tree.command(name="steal", description="Attempt to steal a card from another user.")
@app_commands.describe(victim="The user you want to steal from.", card_name="The name of the card you want to steal.")
async def steal(interaction: discord.Interaction, victim: discord.Member, card_name: str):
    if not await is_server_approved(interaction): return
    thief = interaction.user
    if victim.bot or victim.id == thief.id:
        await interaction.response.send_message("You cannot steal from bots or yourself.", ephemeral=True); return
    guild_id = str(interaction.guild.id); config = SERVER_CONFIGS.get(guild_id, {}); immune_ids = config.get("steal_immune_ids", [])
    if victim.id in immune_ids or any(role.id in immune_ids for role in victim.roles):
        await interaction.response.send_message(f"{victim.display_name} is immune to stealing.", ephemeral=True); return
    if not (thief.id in immune_ids or any(role.id in immune_ids for role in thief.roles)):
        now = datetime.now(timezone.utc); one_hour_ago = now - timedelta(hours=STEAL_COOLDOWN_HOURS)
        recent_timestamps = [t for t in config.get('steal_timestamps', []) if datetime.fromisoformat(t) > one_hour_ago]
        if len(recent_timestamps) >= 2:
            SERVER_CONFIGS.setdefault(guild_id, {})['steal_timestamps'] = recent_timestamps; save_configs()
            await interaction.response.send_message(f"The server-wide steal command is on cooldown (Max 2 uses per hour).", ephemeral=True); return
        recent_timestamps.append(now.isoformat())
        SERVER_CONFIGS.setdefault(guild_id, {})['steal_timestamps'] = recent_timestamps; save_configs()
    
    victim_inv = get_user_inventory(victim.id)
    target_card = next((card for card in victim_inv if card['name'].lower() == card_name.lower()), None)
    if not target_card:
        await interaction.response.send_message(f"{victim.display_name} does not have that card.", ephemeral=True); return
    
    if CARD_RARITY_MAP.get(target_card['name']) not in STEALABLE_RARITIES:
        await interaction.response.send_message("This card's rarity is too low to be stolen (must be R or above).", ephemeral=True); return
    
    thief_inv = get_user_inventory(thief.id)
    eligible_leverage_cards = [card for card in thief_inv if CARD_RARITY_MAP.get(card['name']) in STEALABLE_RARITIES]
    if not eligible_leverage_cards:
        await interaction.response.send_message("You have no cards of high enough rarity (R or above) to leverage for a steal.", ephemeral=True); return
    
    view = LeverageSelectView(eligible_leverage_cards, victim, target_card)
    await interaction.response.send_message("Choose an eligible card from your inventory to risk for this steal attempt.", view=view, ephemeral=True)
@steal.autocomplete('card_name')
async def steal_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not (victim_user := getattr(interaction.namespace, 'victim', None)): return []
    victim_inv = get_user_inventory(victim_user.id)
    unique_stealable_names = sorted({c['name'] for c in victim_inv if CARD_RARITY_MAP.get(c['name']) in STEALABLE_RARITIES})
    return [app_commands.Choice(name=name, value=name) for name in unique_stealable_names if current.lower() in name.lower()][:25]

card_group = app_commands.Group(name="card", description="Commands related to viewing your cards.")
@card_group.command(name="view", description="View a specific card you own.")
@app_commands.describe(card_name="The name of the card you want to see.")
async def card_view(interaction: discord.Interaction, card_name: str):
    if not await is_server_approved(interaction): return
    if card_name not in [c['name'] for c in get_user_inventory(interaction.user.id)]:
        await interaction.response.send_message("You do not own that card.", ephemeral=True); return
    card_to_show = next((card for card in ALL_CARDS if card['main_name'] == card_name), None)
    if not card_to_show:
        await interaction.response.send_message("Error finding that card's image.", ephemeral=True); return
    embed = discord.Embed(title=f"{interaction.user.display_name} is viewing:", description=f"**{card_name}**", color=discord.Color.dark_gold())
    with open(card_to_show['full_path'], 'rb') as f:
        await interaction.response.send_message(embed=embed, file=discord.File(f))
@card_view.autocomplete('card_name')
async def card_view_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    unique_names = sorted({card['name'] for card in get_user_inventory(interaction.user.id)})
    return [app_commands.Choice(name=name, value=name) for name in unique_names if current.lower() in name.lower()][:25]
bot.tree.add_command(card_group)

config_group = app_commands.Group(name="config", description="Admin commands for this server.", default_permissions=discord.Permissions(manage_guild=True))
@config_group.command(name="spawn_channel", description="Set the channel where cards will automatically spawn.")
@app_commands.describe(channel="The text channel to set for spawns.")
async def set_spawn_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    SERVER_CONFIGS.setdefault(guild_id, {})["spawn_channel_id"] = channel.id
    if "next_spawn_time" not in SERVER_CONFIGS[guild_id]:
        first_interval = random.randint(MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL)
        SERVER_CONFIGS[guild_id]["next_spawn_time"] = (datetime.now(timezone.utc) + timedelta(minutes=first_interval)).isoformat()
        await interaction.response.send_message(f"✅ Spawn channel set. First card in ~{first_interval} minutes.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Spawn channel updated to {channel.mention}.", ephemeral=True)
    save_configs()

@config_group.command(name="allow_spawn", description="Allow a user or role to use the /spawn command.")
@app_commands.describe(target="The user or role to grant permission to.")
async def allow_spawn(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    allowed_list = SERVER_CONFIGS.setdefault(guild_id, {}).setdefault("spawn_allowed_ids", [])
    if target.id not in allowed_list:
        allowed_list.append(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} can now use `/spawn`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} already has permission.", ephemeral=True)

@config_group.command(name="deny_spawn", description="Revoke a user or role's permission to use /spawn.")
@app_commands.describe(target="The user or role to revoke permission from.")
async def deny_spawn(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    if target.id in SERVER_CONFIGS.get(guild_id, {}).get("spawn_allowed_ids", []):
        SERVER_CONFIGS[guild_id]["spawn_allowed_ids"].remove(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} can no longer use `/spawn`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} did not have custom permission.", ephemeral=True)

@config_group.command(name="view_spawn_permissions", description="View who has custom permission to use /spawn.")
async def view_spawn_permissions(interaction: discord.Interaction):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    allowed_ids = SERVER_CONFIGS.get(guild_id, {}).get("spawn_allowed_ids", [])
    desc = "**Users with `Manage Server` can always use `/spawn`.**\n\n**Custom Permissions:**\n" + ("\n".join([f"- <@&{entity_id}> / <@{entity_id}>" for entity_id in allowed_ids]) if allowed_ids else "None set.")
    embed = discord.Embed(title="`/spawn` Command Permissions", description=desc, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
@config_group.command(name="allow_steal_immunity", description="Make a user or role immune to the /steal command.")
@app_commands.describe(target="The user or role to make immune.")
async def allow_steal_immunity(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    immune_list = SERVER_CONFIGS.setdefault(guild_id, {}).setdefault("steal_immune_ids", [])
    if target.id not in immune_list:
        immune_list.append(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} is now immune to `/steal`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} is already immune.", ephemeral=True)

@config_group.command(name="deny_steal_immunity", description="Remove a user or role's immunity to /steal.")
@app_commands.describe(target="The user or role to make vulnerable again.")
async def deny_steal_immunity(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    if target.id in SERVER_CONFIGS.get(guild_id, {}).get("steal_immune_ids", []):
        SERVER_CONFIGS[guild_id]["steal_immune_ids"].remove(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} is no longer immune to `/steal`.", ephemeral=True)
    else: await interaction.response.send_message(f"⚠️ {target.mention} was not immune.", ephemeral=True)

@config_group.command(name="view_steal_immunity", description="View who is immune to the /steal command.")
async def view_steal_immunity(interaction: discord.Interaction):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    immune_ids = SERVER_CONFIGS.get(guild_id, {}).get("steal_immune_ids", [])
    desc = "**Users/Roles Immune to Stealing:**\n" + ("\n".join([f"- <@&{entity_id}> / <@{entity_id}>" for entity_id in immune_ids]) if immune_ids else "None set.")
    embed = discord.Embed(title="`/steal` Immunity List", description=desc, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="ban_admin", description="Ban an admin from using bot commands.")
@app_commands.describe(target="The admin to ban from using bot commands.")
async def ban_admin(interaction: discord.Interaction, target: discord.Member):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    if not target.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ This command can only be used on users who have `Manage Server` permissions.", ephemeral=True); return
    if target.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot ban yourself.", ephemeral=True); return
    if target.id == interaction.guild.owner_id:
        await interaction.response.send_message("❌ You cannot ban the server owner.", ephemeral=True); return
    guild_id = str(interaction.guild.id)
    banned_list = SERVER_CONFIGS.setdefault(guild_id, {}).setdefault("banned_admin_ids", [])
    if target.id in banned_list:
        await interaction.response.send_message(f"⚠️ {target.mention} is already banned from using bot commands.", ephemeral=True)
    else:
        banned_list.append(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} has been **banned** from using bot admin commands.", ephemeral=True)

@config_group.command(name="unban_admin", description="Unban an admin, allowing them to use bot commands again.")
@app_commands.describe(target="The admin to unban.")
async def unban_admin(interaction: discord.Interaction, target: discord.Member):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    banned_list = SERVER_CONFIGS.get(guild_id, {}).get("banned_admin_ids", [])
    if target.id not in banned_list:
        await interaction.response.send_message(f"⚠️ {target.mention} is not currently banned.", ephemeral=True)
    else:
        banned_list.remove(target.id)
        save_configs()
        await interaction.response.send_message(f"✅ {target.mention} has been **unbanned** and can now use bot admin commands.", ephemeral=True)

@config_group.command(name="view_banned_admins", description="View the list of admins banned from using bot commands.")
async def view_banned_admins(interaction: discord.Interaction):
    if not await is_server_approved(interaction): return
    if await is_banned_bot_admin(interaction): return
    guild_id = str(interaction.guild.id)
    banned_ids = SERVER_CONFIGS.get(guild_id, {}).get("banned_admin_ids", [])
    desc = "**Admins banned from using bot commands:**\n"
    if banned_ids:
        desc += "\n".join([f"- <@{user_id}>" for user_id in banned_ids])
    else:
        desc += "None."
    embed = discord.Embed(title="Bot Admin Ban List", description=desc, color=discord.Color.red())
    await interaction.response.send_message(embed=embed, ephemeral=True)
bot.tree.add_command(config_group)

# --- 7. BOT EVENTS & ERROR HANDLING ---
@bot.event
async def on_guild_join(guild: discord.Guild):
    guild_id_str = str(guild.id)
    print(f"Joined new guild: {guild.name} ({guild_id_str})")
    SERVER_CONFIGS[guild_id_str] = { "is_approved": False }
    save_configs()
    await send_approval_dm(guild)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.TransformerError):
        await interaction.response.send_message(
            f"❌ I couldn't understand the value you provided for one of the options. Please make sure you select an item from the list (e.g., a user, channel, or role mention).",
            ephemeral=True
        )
    else:
        print(f"Unhandled command error for {getattr(interaction.command, 'name', 'unknown_command')}: {error}")
        traceback.print_exc()
        if not interaction.response.is_done():
            await interaction.response.send_message("An unexpected error occurred. I've notified my developer.", ephemeral=True)
        else:
            await interaction.followup.send("An unexpected error occurred. I've notified my developer.", ephemeral=True)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})'); print('------')
    ensure_data_files_exist()
    load_configs(); load_prefix_weights(); load_card_names(); load_cards(); load_spawn_history()
    bot.add_view(ApprovalView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e: print(e)
    timed_spawn_checker.start()

# --- 8. RUN THE BOT ---
bot.run(TOKEN)