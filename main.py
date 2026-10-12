import discord
from discord.ext import commands
from discord.ui import Button, View
import wavelink
import asyncio
import random
import datetime
import sqlite3
import os
import re
import difflib
import math
import functools
from radio import eligible, weight
from identities import setup_users, remember_user
from sync_playback import SyncPlayback

DB_NAME = "music_bot.db"

def setup_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS songs (
                    song_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    url TEXT UNIQUE,
                    play_count INTEGER DEFAULT 0,
                    last_played TIMESTAMP
                )''')

    c.execute("PRAGMA table_info(songs)")
    columns = [info[1] for info in c.fetchall()]
    if 'play_count' not in columns:
        c.execute("ALTER TABLE songs ADD COLUMN play_count INTEGER DEFAULT 0")
    if 'last_played' not in columns:
        c.execute("ALTER TABLE songs ADD COLUMN last_played TIMESTAMP")

    c.execute('''CREATE TABLE IF NOT EXISTS ratings (
                    user_id INTEGER,
                    song_id INTEGER,
                    score INTEGER,
                    username TEXT,
                    PRIMARY KEY (user_id, song_id),
                    FOREIGN KEY(song_id) REFERENCES songs(song_id)
                )''')
    c.execute('PRAGMA table_info(ratings)')
    rating_columns = [row[1] for row in c.fetchall()]
    if 'username' not in rating_columns:
        c.execute('ALTER TABLE ratings ADD COLUMN username TEXT')

    c.execute('''CREATE TABLE IF NOT EXISTS user_thresholds (
                    user_id INTEGER PRIMARY KEY,
                    min_rating REAL DEFAULT 0.0
                )''')

    c.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY, ignore_unrated INTEGER NOT NULL DEFAULT 1
    )''')
    setup_users(conn)
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA journal_mode = WAL')
    conn.commit()
    conn.close()

setup_db()

TOKEN = os.environ.get('DISCORD_TOKEN') or os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN and os.path.isfile('token.txt'):
    with open('token.txt', 'r', encoding='utf-8') as token_file:
        TOKEN = token_file.read().strip()

if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN environment variable or token.txt file is required')

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='b!', intents=intents)
bot.remove_command('help')
sync_tokens = [os.environ[name] for name in ('DISCORD_SYNC_TOKEN_2', 'DISCORD_SYNC_TOKEN_3') if os.environ.get(name)]
if len(set([TOKEN, *sync_tokens])) != 1 + len(sync_tokens):
    raise RuntimeError('Each sync bot needs a different Discord bot token')
sync_playback = SyncPlayback(bot, sync_tokens)

now_playing_msgs = {}
radio_active = {}
played_history = {}
veto_pool = {}
selection_locks = {}
bound_channels = {}

EMBED_BG = 0x2b2d31
EMBED_RED = 0xED4245

class NormalizedFilters(wavelink.Filters):
    @property
    def payload(self) -> dict:
        data = super().payload
        if "pluginFilters" not in data:
            data["pluginFilters"] = {}
        data["pluginFilters"]["normalization"] = {
            "maxAmplitude": 0.4,
            "adaptive": True
        }
        return data

def run_query(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute('PRAGMA foreign_keys = ON')
    c = conn.cursor()
    try:
        c.execute(query, params)
        if fetch:
            result = c.fetchall()
        else:
            conn.commit()
            result = None
    except sqlite3.IntegrityError:
        result = None
    except Exception as e:
        print(f"DB Error: {e}")
        result = None
    finally:
        conn.close()
    return result

def is_url(query):
    return query.startswith('http://') or query.startswith('https://')

def _normalize_text(text: str) -> str:
    return " ".join("".join(c for c in text.casefold() if c.isalnum() or c.isspace()).split())

def _fuzzy_match_song_in_db(query: str, threshold: float = 0.6) -> str | None:
    if not query: return None
    normalized_query = _normalize_text(query)
    if not normalized_query: return None

    rows = run_query("SELECT title, url FROM songs WHERE url IS NOT NULL", fetch=True)
    if not rows: return None

    best_url = None
    best_score = 0.0
    for row in rows:
        title, url = row[0], row[1]
        if not title or not url: continue
        title_norm = _normalize_text(title)
        if not title_norm: continue
        score = difflib.SequenceMatcher(None, normalized_query, title_norm).ratio()
        if normalized_query == title_norm: score = max(score, 0.95)
        elif title_norm.startswith(normalized_query): score = max(score, 0.8)
        elif normalized_query in title_norm: score = max(score, 0.65)

        if score > best_score:
            best_score = score
            best_url = url

    if best_score >= threshold:
        return best_url
    return None

def resolve_search_query(query):
    query = query.strip()
    if not query: return None
    if re.match(r"^(ytsearch|ytmsearch|scsearch|spsearch|amsearch|dzsearch):", query):
        return query
    if is_url(query): return query

    exact_match = run_query("SELECT url FROM songs WHERE LOWER(title) = ? AND url IS NOT NULL", (query.lower(),), fetch=True)
    if exact_match and exact_match[0][0]: return exact_match[0][0]

    fuzzy_match = run_query("SELECT url FROM songs WHERE LOWER(title) LIKE ? AND url IS NOT NULL LIMIT 1", (f"%{query.lower()}%",), fetch=True)
    if fuzzy_match and fuzzy_match[0][0]: return fuzzy_match[0][0]

    db_fuzzy = _fuzzy_match_song_in_db(query)
    if db_fuzzy: return db_fuzzy

    return query

async def search_tracks(query):
    source = None if re.match(r"^(ytsearch|ytmsearch|scsearch|spsearch|amsearch|dzsearch):", query) else wavelink.TrackSource.YouTubeMusic
    return await wavelink.Playable.search(query, source=source, node=wavelink.Pool.get_node("primary"))


def import_songs_from_txt():
    if not os.path.exists('songs.txt'): return 0
    with open('songs.txt', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    if not lines: return 0

    count = 0
    for line in lines:
        song_name = line.strip()
        if song_name:
            existing = run_query("SELECT song_id FROM songs WHERE LOWER(title) = ?", (song_name.lower(),), fetch=True)
            if not existing:
                run_query("INSERT INTO songs (title, url, play_count) VALUES (?, NULL, 0)", (song_name,))
                count += 1
    with open('songs.txt', 'w', encoding='utf-8') as f: pass
    return count

class RatingView(View):
    def __init__(self, song_id, title):
        super().__init__(timeout=None)
        self.song_id = song_id
        self.title = title

    async def save_rating(self, interaction: discord.Interaction, score: int):
        user_id = interaction.user.id
        await asyncio.to_thread(remember_user, DB_NAME, user_id, interaction.user.name)
        run_query("INSERT OR REPLACE INTO ratings (user_id, song_id, username, score) VALUES (?, ?, ?, ?)",
                  (user_id, self.song_id, interaction.user.name, score))

        label = f"rated {score}"

        if score == -1 and interaction.guild.voice_client and interaction.user.voice and sync_playback.contains(interaction.guild.id, interaction.user.voice.channel.id):
            await interaction.response.send_message(embed=discord.Embed(description=f"you rated **{self.title}** -1. skipping…", color=EMBED_RED), ephemeral=False)
            vc: wavelink.Player = interaction.guild.voice_client
            if vc:
                await vc.skip(force=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(description=f"you {label} for **{self.title}**", color=EMBED_BG), ephemeral=True)

    @discord.ui.button(label="-1", style=discord.ButtonStyle.danger)
    async def ban_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, -1)
    @discord.ui.button(label="0", style=discord.ButtonStyle.secondary)
    async def zero_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 0)
    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary)
    async def one_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 1)
    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary)
    async def two_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 2)
    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def three_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 3)

def create_embed(track: wavelink.Playable, play_count: int = None):
    desc = f"Started playing **[{track.title}]({track.uri})**"
    embed = discord.Embed(description=desc, color=EMBED_BG)
    if play_count is not None:
        duration = str(datetime.timedelta(milliseconds=track.length)) if track.length else "Unknown"
        embed.set_footer(text=f"Length: {duration} • Plays: {play_count}")
    return embed

async def cleanup_player_message(guild_id):
    if guild_id in now_playing_msgs:
        try: await now_playing_msgs[guild_id].delete()
        except: pass
        del now_playing_msgs[guild_id]

def load_radio_data(user_ids, guild_id):
    placeholders = ','.join('?' for _ in user_ids)
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        songs = conn.execute("SELECT song_id, title, url, play_count FROM songs").fetchall()
        rows = conn.execute(
            f"SELECT song_id, user_id, score FROM ratings WHERE user_id IN ({placeholders})",
            tuple(user_ids)).fetchall()
        thresholds = conn.execute(
            f"SELECT min_rating FROM user_thresholds WHERE user_id IN ({placeholders})",
            tuple(user_ids)).fetchall()
        setting = conn.execute("SELECT ignore_unrated FROM guild_settings WHERE guild_id = ?",
                               (guild_id,)).fetchone()
    ratings = {}
    for song_id, user_id, score in rows:
        ratings.setdefault(song_id, {})[user_id] = score
    return songs, ratings, max((row[0] for row in thresholds), default=0), bool(setting[0]) if setting else True


async def find_and_play_random_song(guild: discord.Guild, voice_channel, text_channel):
    async with selection_locks.setdefault(guild.id, asyncio.Lock()):
        if guild.voice_client and guild.voice_client.playing:
            return
        await select_radio_song(guild, voice_channel, text_channel)


async def select_radio_song(guild: discord.Guild, voice_channel, text_channel):
    guild_id = guild.id
    if not voice_channel:
        radio_active[guild_id] = False
        return
    user_ids = sorted(sync_playback.listeners(guild_id))
    if not user_ids:
        radio_active[guild_id] = False
        if guild.voice_client:
            await guild.voice_client.disconnect()
        return
    songs, ratings, threshold, ignore_unrated = await asyncio.to_thread(load_radio_data, user_ids, guild_id)
    history = played_history.setdefault(guild_id, set())
    veto = veto_pool.setdefault(guild_id, set())
    candidates = []
    for song in songs:
        scores = ratings.get(song[0], {})
        if -1 in scores.values():
            veto.add(song[0])
        if song[0] not in history and eligible(scores, user_ids, threshold, ignore_unrated):
            candidates.append(song)
    # Iteration keeps a library full of broken links from building a recursive call stack.
    for _ in range(min(20, len(candidates))):
        returned = [song for song in candidates if song[0] in veto]
        pool = returned if returned and random.random() < 0.5 else candidates
        song = random.choices(pool, weights=[weight(x[3], ratings.get(x[0], {})) for x in pool], k=1)[0]
        song_id, title, url, _ = song
        candidates.remove(song)
        try:
            tracks = await search_tracks(url or title)
            if not tracks:
                history.add(song_id)
                continue
            track = tracks[0]
            if not url:
                await asyncio.to_thread(run_query, "UPDATE songs SET url = ?, title = ? WHERE song_id = ?",
                                        (track.uri, track.title, song_id))
            vc = guild.voice_client
            if not vc or not radio_active.get(guild_id):
                return
            history.add(song_id)
            veto.discard(song_id)
            await vc.queue.put_wait(track)
            if not vc.playing:
                await vc.play(vc.queue.get())
            return
        except Exception as exc:
            history.add(song_id)
            print(f"Radio could not play song {song_id}: {exc}")
    radio_active[guild_id] = False
    if text_channel:
        await text_channel.send("Radio stopped: no playable songs left matching listeners' ratings. "
                                "Try b!unrated on, lower the minimum, or restart b!radio.")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    rows = run_query('SELECT user_id FROM users', fetch=True) or []
    for (user_id,) in rows:
        user = bot.get_user(user_id)
        if user:
            await asyncio.to_thread(remember_user, DB_NAME, user_id, user.name)
    count = import_songs_from_txt()
    if count > 0: print(f"Imported {count} songs on boot.")

    nodes = [wavelink.Node(uri=os.environ.get("LAVALINK_URI", "http://localhost:2333"), password=os.environ.get("LAVALINK_PASSWORD", "youshallnotpass"), identifier="primary")]
    if "primary" not in wavelink.Pool.nodes:
        await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=1500)

@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"Wavelink Node {payload.node.identifier} is ready!")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    if member.guild.voice_client:
        bot_channel = member.guild.voice_client.channel
        if before.channel and sync_playback.contains(member.guild.id, before.channel.id) and after.channel != before.channel:
            await asyncio.sleep(180)
            members = sync_playback.listeners(member.guild.id)
            if not members:
                guild = member.guild
                channel = bound_channels.get(guild.id)
                if channel:
                    embed = discord.Embed(description="No one has been listening for the past 3 minutes, leaving", color=EMBED_RED)
                    await channel.send(embed=embed)
                radio_active[guild.id] = False
                if guild.voice_client:
                    await guild.voice_client.disconnect()

@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    track = payload.track
    if not player or not track: return

    guild_id = player.guild.id
    if player is not bot.get_guild(guild_id).voice_client:
        return
    await sync_playback.refresh(guild_id, restart=True)
    channel = bound_channels.get(guild_id)

    current_play_count = 0
    song_id = None

    existing_id = run_query("SELECT song_id, play_count FROM songs WHERE url = ?", (track.uri,), fetch=True)
    if existing_id:
        current_play_count = (existing_id[0][1] or 0) + 1
        song_id = existing_id[0][0]
        run_query("UPDATE songs SET title = ?, play_count = ?, last_played = CURRENT_TIMESTAMP WHERE url = ?",
                  (track.title, current_play_count, track.uri))
    else:
        manual_match = run_query("SELECT song_id, play_count FROM songs WHERE LOWER(title) = ? AND url IS NULL LIMIT 1", (track.title.lower(),), fetch=True)
        if manual_match:
            current_play_count = (manual_match[0][1] or 0) + 1
            song_id = manual_match[0][0]
            run_query("UPDATE songs SET url = ?, play_count = ?, last_played = CURRENT_TIMESTAMP WHERE song_id = ?",
                      (track.uri, current_play_count, song_id))

    if channel:
        await cleanup_player_message(guild_id)
        embed = create_embed(track, current_play_count)

        if song_id is not None:
            view = RatingView(song_id, track.title)
            msg = await channel.send(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed)

        now_playing_msgs[guild_id] = msg

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    if not payload.player:
        return
    async with selection_locks.setdefault(payload.player.guild.id, asyncio.Lock()):
        await advance_queue(payload)


async def advance_queue(payload):
    player = payload.player
    if not player or player is not bot.get_guild(player.guild.id).voice_client: return

    if str(payload.reason).upper() == "REPLACED": return
    if str(payload.reason).upper() == "LOADFAILED":
        player.queue.mode = wavelink.QueueMode.normal
        player.queue.loaded = None

    if player.queue.mode == wavelink.QueueMode.loop_all and player.queue.is_empty and player.queue.history:
        await player.queue.put_wait(list(player.queue.history))
        player.queue.history.clear()
    if (player.queue.mode == wavelink.QueueMode.loop and player.queue.loaded is not None) or not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)
    else:
        guild_id = player.guild.id
        channel = bound_channels.get(guild_id)
        if radio_active.get(guild_id, False):
            await select_radio_song(player.guild, player.channel, channel)
        else:
            await cleanup_player_message(guild_id)

@bot.command()
async def help(ctx):
    commands_list = (
        "`b!play <query>` (Aliases: `b!p`) - Play a song or add it to the queue\n"
        "`b!skip` (Aliases: `b!s`) - Skip the current song\n"
        "`b!stop` (Aliases: `b!st`) - Stop the music and clear the queue\n"
        "`b!leave` (Aliases: `b!l`) - Disconnect the bot\n"
        "`b!queue [page]` / `b!remove <number>` / `b!move <from> <to>`\n"
        "`b!shuffle` / `b!clear` / `b!seek <seconds>`\n"
        "`b!unrated on|off` - Allow or block missing listener ratings\n"
        "`b!loop [mode]` (Aliases: `b!lp`) - Toggle track/queue looping\n"
        "`b!pause` (Aliases: `b!pa`) - Pause the current song\n"
        "`b!resume` (Aliases: `b!re`) - Resume paused song\n"
        "`b!np` - Show what is currently playing\n"
        "`b!radio` (Aliases: `b!r`) - Start endless radio mode\n"
        "`b!minrating <score>` (Aliases: `b!mr`) - Set your personal radio rating threshold\n"
        "`b!sync <channel ID> [second ID]` / `b!unsync` - Link voice channels\n"
        "`b!library [page]` - Show song database table\n"
        "`b!rate <val> <name>` - Rate song by name (-1 to 3)\n"
    )
    embed = discord.Embed(title="Commands", description=commands_list, color=EMBED_BG)
    await ctx.send(embed=embed)

@bot.command(aliases=['p'])
async def play(ctx, *, search: str):
    if not ctx.message.author.voice:
        return await ctx.send(embed=discord.Embed(description="You must be in a voice channel first.", color=EMBED_RED))

    bound_channels[ctx.guild.id] = ctx.channel

    vc: wavelink.Player = ctx.voice_client
    if not vc: vc = await ctx.author.voice.channel.connect(cls=functools.partial(wavelink.Player, nodes=[wavelink.Pool.get_node("primary")]))

    filters = NormalizedFilters()
    await vc.set_filters(filters)

    resolved_query = resolve_search_query(search)
    if not resolved_query:
        return await ctx.send(embed=discord.Embed(description="Please provide a valid song name or url.", color=EMBED_RED))

    try:
        tracks = await search_tracks(resolved_query)
        if not tracks:
            return await ctx.send(embed=discord.Embed(description="Could not find that track.", color=EMBED_RED))
    except Exception as e:
        return await ctx.send(embed=discord.Embed(description=f"Error searching for track: {e}", color=EMBED_RED))

    items = tracks.tracks if isinstance(tracks, wavelink.Playlist) else [tracks[0]]
    track = items[0]
    async with selection_locks.setdefault(ctx.guild.id, asyncio.Lock()):
        await vc.queue.put_wait(items)
        if not vc.playing:
            await vc.play(vc.queue.get())
    embed = discord.Embed(description=f"Queued {len(items)} track(s), starting with **{track.title}**", color=EMBED_BG)
    await ctx.send(embed=embed, delete_after=10)

@bot.command(aliases=['s'])
async def skip(ctx):
    vc: wavelink.Player = ctx.voice_client
    if vc and vc.playing:
        await vc.skip(force=True)
        await ctx.send(embed=discord.Embed(description="Skipped the current track", color=EMBED_BG))

@bot.command(aliases=['st'])
async def stop(ctx):
    vc: wavelink.Player = ctx.voice_client
    radio_active[ctx.guild.id] = False

    if ctx.guild.id in played_history: played_history[ctx.guild.id].clear()
    if ctx.guild.id in veto_pool: veto_pool[ctx.guild.id].clear()

    if vc:
        vc.queue.reset()
        await vc.stop()
        await sync_playback.refresh(ctx.guild.id)

    await cleanup_player_message(ctx.guild.id)
    await ctx.send(embed=discord.Embed(description="Stopped the music and cleared the queue", color=EMBED_BG))

@bot.command(name='np')
async def now_playing(ctx):
    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.playing:
        return await ctx.send(embed=discord.Embed(description="Nothing is playing right now.", color=EMBED_BG))

    track = vc.current
    embed = discord.Embed(description=f"Now playing **[{track.title}]({track.uri})**", color=EMBED_BG)
    await ctx.send(embed=embed)

@bot.command(aliases=['q'])
async def queue(ctx, page: int = 1):
    vc = ctx.voice_client
    if not vc:
        return await ctx.send("The bot is not connected.")
    tracks = list(vc.queue)
    pages = max(1, math.ceil(len(tracks) / 10))
    if not 1 <= page <= pages:
        return await ctx.send(f"Choose a page from 1 to {pages}.")
    lines = []
    if vc.current:
        lines.append(f"Now: **{discord.utils.escape_markdown(vc.current.title)}**")
    for index, track in enumerate(tracks[(page - 1) * 10:page * 10], (page - 1) * 10 + 1):
        title = discord.utils.escape_markdown(track.title[:150])
        lines.append(f"**{index}.** {title}")
    if not tracks:
        lines.append("No upcoming tracks. Radio will choose next if enabled.")
    embed = discord.Embed(title=f"Queue · {page}/{pages}", description="\n".join(lines), color=EMBED_BG)
    embed.set_footer(text=f"{len(tracks)} upcoming · Loop: {vc.queue.mode.name}")
    await ctx.send(embed=embed)


@bot.command()
async def remove(ctx, position: int):
    vc = ctx.voice_client
    if not vc or not 1 <= position <= len(vc.queue):
        return await ctx.send("Use a track number from b!queue.")
    track = vc.queue.peek(position - 1)
    vc.queue.delete(position - 1)
    await ctx.send(f"Removed **{discord.utils.escape_markdown(track.title)}**.")


@bot.command()
async def move(ctx, source: int, destination: int):
    vc = ctx.voice_client
    if not vc or not (1 <= source <= len(vc.queue) and 1 <= destination <= len(vc.queue)):
        return await ctx.send("Both numbers must refer to upcoming tracks in b!queue.")
    track = vc.queue.peek(source - 1)
    vc.queue.delete(source - 1)
    vc.queue.put_at(destination - 1, track)
    await ctx.send(f"Moved track {source} to {destination}.")


@bot.command()
async def shuffle(ctx):
    if ctx.voice_client:
        ctx.voice_client.queue.shuffle()
        await ctx.send("Shuffled the upcoming tracks.")


@bot.command()
async def clear(ctx):
    if ctx.voice_client:
        ctx.voice_client.queue.reset()
        await ctx.send("Cleared the queue and turned off looping. The current song keeps playing.")


@bot.command()
async def seek(ctx, seconds: int):
    vc = ctx.voice_client
    if not vc or not vc.current or not vc.current.is_seekable:
        return await ctx.send("No seekable song is playing.")
    if not 0 <= seconds * 1000 < vc.current.length:
        return await ctx.send("Choose a position within the song, in seconds.")
    await vc.seek(seconds * 1000)
    await sync_playback.refresh(ctx.guild.id)
    await ctx.send(f"Jumped to {seconds} seconds.")


@bot.command()
async def unrated(ctx, mode: str = ''):
    if mode.lower() not in ('on', 'off'):
        return await ctx.send("b!unrated on allows missing ratings; b!unrated off requires every listener to rate the song.")
    run_query("INSERT INTO guild_settings (guild_id, ignore_unrated) VALUES (?, ?) "
              "ON CONFLICT(guild_id) DO UPDATE SET ignore_unrated=excluded.ignore_unrated",
              (ctx.guild.id, int(mode.lower() == 'on')))
    await ctx.send(f"Allow unrated songs: {mode.lower()}. Applies to future radio selections.")


@bot.command(aliases=['lp'])
async def loop(ctx, mode: str = None):
    vc: wavelink.Player = ctx.voice_client
    if not vc:
        return await ctx.send(embed=discord.Embed(description="Nothing is playing right now.", color=EMBED_RED))

    if mode in ['track', 'song', 'current', '1']:
        vc.queue.mode = wavelink.QueueMode.loop
        msg = "Looping the current track"
    elif mode in ['queue', 'all', 'playlist']:
        vc.queue.mode = wavelink.QueueMode.loop_all
        msg = "Looping the entire queue"
    elif mode in ['off', 'disable', 'stop', 'normal']:
        vc.queue.mode = wavelink.QueueMode.normal
        msg = "Looping disabled"
    else:
        if vc.queue.mode == wavelink.QueueMode.normal:
            vc.queue.mode = wavelink.QueueMode.loop
            msg = "Looping the current track"
        elif vc.queue.mode == wavelink.QueueMode.loop:
            vc.queue.mode = wavelink.QueueMode.loop_all
            msg = "Looping the entire queue"
        else:
            vc.queue.mode = wavelink.QueueMode.normal
            msg = "Looping disabled"

    await ctx.send(embed=discord.Embed(description=msg, color=EMBED_BG))

@bot.command(aliases=['pa'])
async def pause(ctx):
    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.playing:
        return await ctx.send(embed=discord.Embed(description="Nothing is playing right now.", color=EMBED_RED))
    await vc.pause(True)
    await sync_playback.refresh(ctx.guild.id)
    await ctx.send(embed=discord.Embed(description="Paused the current track", color=EMBED_BG))

@bot.command(aliases=['re'])
async def resume(ctx):
    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.paused:
        return await ctx.send(embed=discord.Embed(description="Nothing is paused right now.", color=EMBED_RED))
    await vc.pause(False)
    await sync_playback.refresh(ctx.guild.id)
    await ctx.send(embed=discord.Embed(description="Resumed the current track", color=EMBED_BG))

@bot.command(aliases=['l'])
async def leave(ctx):
    vc: wavelink.Player = ctx.voice_client
    radio_active[ctx.guild.id] = False
    if ctx.guild.id in played_history: played_history[ctx.guild.id].clear()
    if ctx.guild.id in veto_pool: veto_pool[ctx.guild.id].clear()

    await cleanup_player_message(ctx.guild.id)
    await sync_playback.leave(ctx.guild.id)
    if vc:
        await vc.disconnect()
        await ctx.send(embed=discord.Embed(description="Disconnected from the voice channel", color=EMBED_BG))

@bot.command(aliases=['r'])
async def radio(ctx):
    if not ctx.message.author.voice:
        return await ctx.send(embed=discord.Embed(description="Join a voice channel first.", color=EMBED_RED))

    bound_channels[ctx.guild.id] = ctx.channel
    vc: wavelink.Player = ctx.voice_client
    if not vc: await ctx.message.author.voice.channel.connect(cls=functools.partial(wavelink.Player, nodes=[wavelink.Pool.get_node("primary")]))

    played_history[ctx.guild.id] = set()
    veto_pool[ctx.guild.id] = set()
    radio_active[ctx.guild.id] = True

    await ctx.send(embed=discord.Embed(description="**Started Radio Mode**", color=EMBED_BG))

    vc = ctx.voice_client
    if not vc.playing:
        await find_and_play_random_song(ctx.guild, ctx.author.voice.channel, ctx.channel)

@bot.command(aliases=['mr'])
async def minrating(ctx, score: float):
    if not math.isfinite(score) or score < 0.0 or score > 3.0:
        return await ctx.send(embed=discord.Embed(description="Score must be between 0.0 and 3.0.", color=EMBED_RED))

    await asyncio.to_thread(remember_user, DB_NAME, ctx.author.id, ctx.author.name)
    run_query("INSERT OR REPLACE INTO user_thresholds (user_id, min_rating) VALUES (?, ?)", (ctx.author.id, score))
    await ctx.send(embed=discord.Embed(description=f"Your personal radio rating threshold is set to **{score}**", color=EMBED_BG))

@bot.command()
async def rate(ctx, score: int, *, query: str):
    if score not in [-1, 0, 1, 2, 3]:
        return await ctx.send(embed=discord.Embed(description="Score must be between -1 and 3.", color=EMBED_RED))

    search_term = f"%{query}%"
    results = run_query("SELECT song_id, title FROM songs WHERE title LIKE ?", (search_term,), fetch=True)
    if query.startswith("#") and query[1:].isdigit():
        results = run_query("SELECT song_id, title FROM songs WHERE song_id = ?", (int(query[1:]),), fetch=True)
    if not results:
        return await ctx.send(embed=discord.Embed(description=f"Could not find any song matching **{query}**.", color=EMBED_RED))

    if len(results) > 1:
        options = "\n".join(f"#{sid}: {title}" for sid, title in results[:10])
        return await ctx.send(f"Several songs match. Use b!rate <score> #<id>:\n{options}")
    best_match = results[0]
    song_id, song_title = best_match[0], best_match[1]
    user_id, username = ctx.author.id, ctx.author.name
    await asyncio.to_thread(remember_user, DB_NAME, user_id, username)

    run_query("INSERT OR REPLACE INTO ratings (user_id, song_id, username, score) VALUES (?, ?, ?, ?)", (user_id, song_id, username, score))
    run_query("UPDATE ratings SET username = ? WHERE user_id = ? AND username IS NULL", (username, user_id))

    label = f"rated {score}"
    await ctx.send(embed=discord.Embed(description=f"you {label} for **{song_title}**", color=EMBED_BG))

@bot.command()
async def library(ctx, page: int = 1):
    PER_PAGE = 10
    offset = (page - 1) * PER_PAGE

    total_query = run_query("SELECT COUNT(*) FROM songs", fetch=True)
    total = total_query[0][0] if total_query else 0
    pages = max(1, math.ceil(total / PER_PAGE))

    if page < 1 or page > pages:
        return await ctx.send(embed=discord.Embed(description=f"Invalid page. Total pages: {pages}", color=EMBED_RED))

    rater_rows = run_query("SELECT DISTINCT user_id FROM ratings", fetch=True)
    all_rater_ids = [r[0] for r in rater_rows] if rater_rows else []

    rater_map = {}
    for uid in all_rater_ids:
        member = ctx.guild.get_member(uid)
        if not member:
            try: member = await ctx.guild.fetch_member(uid)
            except: member = None
        rater_map[uid] = member.display_name[:3] if member else str(uid)[:3]

    songs = run_query("SELECT song_id, title, play_count FROM songs ORDER BY play_count DESC LIMIT ? OFFSET ?", (PER_PAGE, offset), fetch=True)
    if not songs:
        return await ctx.send(embed=discord.Embed(description="Database is empty.", color=EMBED_RED))

    user_headers = " | ".join([name.center(3) for name in rater_map.values()])
    header = f"ID  | {'Title':<18} | Plays | {user_headers}"
    divider = "-" * len(header)

    rows_text = ""
    for idx, song in enumerate(songs):
        s_id, title, plays = song
        display_title = title[:16] + ".." if len(title) > 18 else title.ljust(18)

        ratings_raw = run_query("SELECT user_id, score FROM ratings WHERE song_id = ?", (s_id,), fetch=True)
        song_ratings = {r[0]: r[1] for r in ratings_raw}

        user_cols = []
        for uid in all_rater_ids:
            score = song_ratings.get(uid, None)
            if score is None: col = " . "
            elif score == -1: col = "-1 "
            else: col = f" {score} "
            user_cols.append(col)

        rating_str = " | ".join(user_cols)
        row_num = str(s_id).zfill(2)
        rows_text += f"{row_num}  | {display_title} | {str(plays).center(5)} | {rating_str}\n"

    embed = discord.Embed(
        title=f"Library (Page {page}/{pages})",
        description=f"```text\n{header}\n{divider}\n{rows_text}```",
        color=EMBED_BG
    )
    embed.set_footer(text="-1 = blocked while you listen, . = unrated")
    await ctx.send(embed=embed)

@bot.command()
async def add(ctx, *, query: str):
    if query.startswith("http"):
        try:
            existing = run_query("SELECT song_id FROM songs WHERE url = ?", (query,), fetch=True)
            if existing: return await ctx.send(embed=discord.Embed(description="This link is already in the database.", color=EMBED_RED))
            run_query("INSERT INTO songs (title, url, play_count) VALUES (?, ?, 0)", ("Unknown Title", query))
            await ctx.send(embed=discord.Embed(description=f"Added link: {query}", color=EMBED_BG))
        except Exception as e:
            await ctx.send(embed=discord.Embed(description=f"Error adding link: {e}", color=EMBED_RED))
    else:
        try:
            existing = run_query("SELECT song_id FROM songs WHERE LOWER(title) = ?", (query.lower(),), fetch=True)
            if existing: return await ctx.send(embed=discord.Embed(description=f"Song **{query}** is already in the database.", color=EMBED_RED))
            run_query("INSERT INTO songs (title, url, play_count) VALUES (?, NULL, 0)", (query,))
            await ctx.send(embed=discord.Embed(description=f"Added song: **{query}**", color=EMBED_BG))
        except Exception as e:
            await ctx.send(embed=discord.Embed(description=f"Error adding name: {e}", color=EMBED_RED))

@bot.command(name='sync')
async def sync_channels(ctx, *channels: discord.VoiceChannel):
    if not 1 <= len(channels) <= 2:
        return await ctx.send("Use b!sync <channel ID> [second channel ID] to add up to two companions.")
    try:
        await sync_playback.join(ctx.guild.id, channels)
    except (ValueError, discord.DiscordException, wavelink.WavelinkException) as exc:
        return await ctx.send(f"Could not link channels: {exc}")
    await ctx.send("Linked voice channels. Playback and the queue are controlled by the primary bot.")


@bot.command()
async def unsync(ctx):
    await sync_playback.leave(ctx.guild.id)
    await ctx.send("Disconnected the companions. The primary bot keeps playing.")


@bot.check
async def check_listener(ctx):
    if ctx.guild is None:
        return False
    controls = {'play', 'radio', 'skip', 'stop', 'leave', 'loop', 'pause', 'resume',
                'remove', 'move', 'shuffle', 'clear', 'seek', 'unrated', 'sync', 'unsync'}
    if ctx.command.name in controls:
        voice = ctx.author.voice
        if not voice or (ctx.voice_client and not sync_playback.contains(ctx.guild.id, voice.channel.id)):
            raise commands.CheckFailure("Join a voice channel linked to the bot to control playback.")
    return True


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.UserInputError):
        await ctx.send(f"Check the arguments. Usage: b!{ctx.command.qualified_name} {ctx.command.signature}")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(str(error))
    else:
        print(f"Command error: {error}")
        await ctx.send("That command failed. Check the bot log for details.")


async def main():
    async with bot:
        await sync_playback.start()
        try:
            await bot.start(TOKEN)
        finally:
            await sync_playback.close()


if __name__ == '__main__':
    asyncio.run(main())
