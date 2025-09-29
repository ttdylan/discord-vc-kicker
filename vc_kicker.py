# file: vc_kicker.py
from dotenv import load_dotenv
load_dotenv()  # keep this above the os.getenv calls

import os
import asyncio
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Set

import discord
from discord.ext import tasks

# Users who have manually disabled enforcement (via !stepoff)
suppressed_users: Set[int] = set()


# ── Env ────────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
TZ_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
# This user follows the global BLOCK_WINDOWS; everyone else ignores them.
SCHEDULED_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))


# ── Schedule (edit as you like) ────────────────────────────────────────────────
# days: 0=Mon ... 6=Sun; times are "HH:MM" 24h, supports crossing midnight.
BLOCK_WINDOWS = [
    {"days": [0,1,2,3,4,5,6], "start": "07:00", "end": "15:00"},  # 7:00 AM – 3:00 PM
    {"days": [0,1,2,3,4,5,6], "start": "00:00", "end": "06:30"},  # midnight – 6:30 AM
]

# ── State (per-user) ───────────────────────────────────────────────────────────
enabled_users: Set[int] = set()   
enabled_users.add(SCHEDULED_USER_ID)              # users who opted in
grace_untils: Dict[int, datetime] = {}          # user_id -> grace end (local tz)
# Users who are opted-in until a specific time (local tz). If missing -> indefinite.
opt_untils: Dict[int, datetime] = {}


# ── Time helpers ───────────────────────────────────────────────────────────────
tz = ZoneInfo(TZ_NAME)

def fmt12(dt: datetime) -> str:
    return dt.strftime("%a %I:%M %p")

class SteponUntilView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only the person who issued !steponme can use this selector
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("🙅 This selector isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="Choose when you want stepping to stop…",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="30 minutes", value="30m", description="Stop after 30 minutes"),
            discord.SelectOption(label="1 hour", value="1h", description="Stop after 1 hour"),
            discord.SelectOption(label="2 hours", value="2h", description="Stop after 2 hours"),
            discord.SelectOption(label="End of today", value="eod", description="Stop at 11:59 PM today"),
            discord.SelectOption(label="End of current block", value="block", description="Stop when block ends"),
            discord.SelectOption(label="Indefinite (manual)", value="inf", description="Stay on until you opt out"),
        ]
    )
    async def select_until(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        now = datetime.now(tz)
        local = now.astimezone(tz)

        # Compute 'until' based on choice
        if choice == "30m":
            until = local + timedelta(minutes=30)
        elif choice == "1h":
            until = local + timedelta(hours=1)
        elif choice == "2h":
            until = local + timedelta(hours=2)
        elif choice == "eod":
            until = local.replace(hour=23, minute=59, second=0, microsecond=0)
        elif choice == "block":
            nxt = next_change(local)
            # If we’re currently blocked, "next_change" means block end; otherwise next block start.
            until = nxt if nxt else (local + timedelta(hours=8))
        elif choice == "inf":
            until = None
        else:
            until = None

        # inside SteponUntilView.select_until after computing 'until'
        uid = interaction.user.id
        enabled_users.add(uid)
        grace_untils.pop(uid, None)  # cancel any grace

        if until is not None:
            opt_untils[uid] = until
            print(f"[INFO] Set personal until for {uid} -> {until.isoformat()}")
        else:
            # Indefinite: explicitly remove any previous until
            if uid in opt_untils:
                print(f"[INFO] Clearing personal until for {uid}")
            opt_untils.pop(uid, None)


        # Enforce immediately if we’re in a block
        # Enforce immediately: if they're in ANY voice channel, disconnect now
        # Immediate enforcement after opting-in:
        kicked_msg = ""
        member = interaction.guild.get_member(uid)
        if member and member.voice and member.voice.channel:
            if await disconnect_if_needed(member, reason="activation (!steponme)"):
                kicked_msg = " — and you were disconnected immediately."



        until_str = "indefinitely (use !stepoff to opt out)" if not until else fmt12(until)
        await interaction.response.edit_message(
            content=(
                f"✅ **{interaction.user.display_name}** is now opted-in.\n"
                f"Stepping will remain active **until {until_str}** in {TZ_NAME}{kicked_msg}."
            ),
            view=None
        )


def parse_hhmm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(hour=h, minute=m, tzinfo=None)

def is_blocked_now(now: datetime) -> bool:
    """Return True if the local time falls within any BLOCK_WINDOWS."""
    local = now.astimezone(tz)
    weekday = local.weekday()
    for w in BLOCK_WINDOWS:
        if weekday not in w["days"]:
            continue
        start = parse_hhmm(w["start"])
        end = parse_hhmm(w["end"])
        start_dt = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        end_dt = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

        if start <= end:
            # same-day window (e.g., 07:00–12:00)
            if start_dt <= local < end_dt:
                return True
        else:
            # crosses midnight (e.g., 22:30–06:30)
            if local >= start_dt or local < end_dt:
                return True
    return False

def is_enforcement_active_for(user_id: int, now: datetime) -> bool:
    """User opted-in (and not expired), no grace.
    Rules:
    - If user has a personal 'until' in the future => ALWAYS enforce until then (ignores schedule).
    - Else, if user == SCHEDULED_USER_ID => enforce only in BLOCK_WINDOWS.
    - Else (everyone else) => enforce while opted-in (ignores schedule).
    """
    if user_id not in enabled_users:
        return False

    local = now.astimezone(tz)

    # Personal window
    until = opt_untils.get(user_id)
    if until:
        if local >= until:
            # expire personal window
            opt_untils.pop(user_id, None)
            # fall through to schedule rules (for SCHEDULED_USER_ID) or general rule (others)
        else:
            # active personal window overrides everything
            g = grace_untils.get(user_id)
            return not (g and local < g)

    # No personal window; honor grace if present
    g = grace_untils.get(user_id)
    if g and local < g:
        return False

    # Scheduled user: only during global block windows
    if user_id == SCHEDULED_USER_ID:
        return is_blocked_now(local)

    # Everyone else: enforce while opted-in
    return True





def next_change(now: datetime) -> Optional[datetime]:
    """Return the next datetime when *global* block state changes (start or end)."""
    local = now.astimezone(tz)
    weekday = local.weekday()
    candidates = []

    for offset in range(7):  # look up to a week ahead
        day = (weekday + offset) % 7
        day_date = (local + timedelta(days=offset)).date()

        for w in BLOCK_WINDOWS:
            if day not in w["days"]:
                continue
            start = parse_hhmm(w["start"])
            end = parse_hhmm(w["end"])
            start_dt = datetime.combine(day_date, start, tz)
            end_dt = datetime.combine(day_date, end, tz)

            if start <= end:
                candidates.extend([start_dt, end_dt])
            else:
                candidates.append(start_dt)
                candidates.append(end_dt + timedelta(days=1))

    # Only future times
    candidates = [c for c in candidates if c > local]
    return min(candidates) if candidates else None

def ensure_scheduled_user_state(now: datetime) -> None:
    """If we're inside a block, auto-opt the scheduled user in until the block ends."""
    local = now.astimezone(tz)
    # Always keep you in enabled_users so is_enforcement_active_for can apply schedule logic

    if is_blocked_now(local):
        # End of the CURRENT block = next_change(now)
        end = next_change(local)
        if end:
            opt_untils[SCHEDULED_USER_ID] = end
    else:
        # Outside a block: clear the auto 'until' so you fall back to schedule next time
        opt_untils.pop(SCHEDULED_USER_ID, None)


# ── Discord client ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.members = True          # needed to move members
intents.voice_states = True     # detect voice join/leave
intents.message_content = True  # for prefix commands
client = discord.Client(intents=intents)

async def disconnect_if_needed(member: discord.Member, reason: str) -> bool:
    """Disconnect a member if they're in voice. No DMs."""
    if not member or member.bot or not member.voice or not member.voice.channel:
        return False

    vch = member.voice.channel  # capture before move
    try:
        await member.move_to(None, reason=reason)  # Requires "Move Members" permission
        print(f"[INFO] Disconnected {member.display_name} from {vch} ({reason})")
        return True
    except discord.Forbidden:
        print(f"[WARN] Missing 'Move Members' permission to disconnect {member.display_name} in {vch}.")
    except discord.HTTPException as e:
        print(f"[WARN] Failed to move {member.display_name}: {e}")
    return False

def make_commands_embed(author_name: str) -> discord.Embed:
    desc = (
        "**Here’s what you can do:**\n"
        "• `!steponme` — activate stepping and pick how long it should last (selector below)\n"
        "• `!dontsteponme` — start a 5-minute grace (no kicks) for yourself\n"
        "• `!stepstatus` — show your current state, grace (if any), and end time\n"
        "• `!stepoff` — opt out (stop stepping) for yourself\n"
        "• `!debugme` — (optional) print debug info just for you\n"
    )
    emb = discord.Embed(
        title=f"Hi {author_name}! Choose how long stepping should stay on:",
        description=desc,
        color=0x5865F2,
    )
    emb.set_footer(text="Tip: you can change or pause later with !dontsteponme / !stepoff")
    return emb


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (tz={TZ_NAME})")

    if not periodic_enforcer.is_running():
        periodic_enforcer.start()




@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not member or member.bot:
        return
    
    if after.channel is not None and is_enforcement_active_for(member.id, datetime.now(tz)):
        await disconnect_if_needed(member, reason="blocked window (join)")


@client.event
async def on_connect():
    print("[INFO] Connected to Discord gateway")

@client.event
async def on_resumed():
    print("[INFO] Session resumed after a hiccup")


@tasks.loop(seconds=15)
async def periodic_enforcer():
    now = datetime.now(tz)
    
    for guild in client.guilds:
        for uid in list(enabled_users):
            member = guild.get_member(uid)
            if member and is_enforcement_active_for(uid, now):
                kicked = await disconnect_if_needed(member, reason="periodic enforcement")
                if kicked:
                    await asyncio.sleep(1.5)


@periodic_enforcer.before_loop
async def before_periodic_enforcer():
    # Make sure the client is fully ready before the first run
    await client.wait_until_ready()



# ── Commands ─────────────────────────────────────────────────────────────────
def fmt_next_change(now: datetime) -> str:
    nxt = next_change(now)
    return nxt.strftime("%a %I:%M %p") if nxt else "an unknown time"

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content = message.content.strip().lower()
    author: discord.Member = message.author
    now = datetime.now(tz)

    # Opt-in & force reactivation (for yourself only)
# Opt-in & show commands + selector (for yourself only)
    if content == "!steponme":
        view = SteponUntilView(author.id)
        embed = make_commands_embed(author.display_name)
        await message.channel.send(embed=embed, view=view)
        return



    # Start a 5-minute grace (for yourself only)
    if content == "!dontsteponme":
        if author.id not in enabled_users:
            enabled_users.add(author.id)  # auto-opt in if they haven't yet

        grace_untils[author.id] = now + timedelta(minutes=5)
        ends_str = grace_untils[author.id].strftime("%a %I:%M %p")
        await message.channel.send(
            f"🕊️ Grace started for **{author.display_name}** — no stepping until **{ends_str}**."
        )
        return

    # Show your status
    if content == "!stepstatus":
        opted = author.id in enabled_users
        now = datetime.now(tz)
        state = "STEPPED ON" if is_enforcement_active_for(author.id, now) else "NOT BE STEPPED ON"
        g = grace_untils.get(author.id)
        until = opt_untils.get(author.id)
        pieces = [f"opted in: **{opted}**", f"state: **{state}** in {TZ_NAME}"]
        if g and now < g:
            pieces.append(f"grace until **{g.strftime('%I:%M %p')}**")
        if until:
            pieces.append(f"active until **{fmt12(until)}**")
        else:
            if author.id in enabled_users:
                pieces.append("active **indefinitely**")
        await message.channel.send(f"👤 **{author.display_name}** — " + "; ".join(pieces) + ".")
        return


    # Opt-out completely (optional command)
    if content == "!stepoff":
        if author.id in enabled_users:
            enabled_users.remove(author.id)
        grace_untils.pop(author.id, None)
        await message.channel.send(f"✅ **{author.display_name}** has opted out of stepping.")
        return
    
    if content == "!debugme":
        uid = author.id
        now = datetime.now(tz)
        u = opt_untils.get(uid)
        g = grace_untils.get(uid)
        await message.channel.send(
            f"enabled={uid in enabled_users}; "
            f"until={(fmt12(u) if u else 'None')}; "
            f"grace={(fmt12(g) if g and now<g else 'None')}; "
            f"is_blocked_now={is_blocked_now(now)}; "
            f"enforce_now={is_enforcement_active_for(uid, now)}"
        )
    return


# ── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing. Check your .env.")
    client.run(TOKEN)
