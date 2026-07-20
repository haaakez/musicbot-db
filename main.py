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
                
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA journal_mode = WAL')
    conn.commit()
    conn.close()

setup_db()

TOKEN = os.environ.get('DISCORD_TOKEN')
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

now_playing_msgs = {}
radio_active = {}       
played_history = {}     
veto_pool = {}          
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
    return re.sub(r'[^a-z0-9 ]+', '', text.lower()).strip()

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
    if is_url(query): return query

    exact_match = run_query("SELECT url FROM songs WHERE LOWER(title) = ? AND url IS NOT NULL", (query.lower(),), fetch=True)
    if exact_match and exact_match[0][0]: return exact_match[0][0]

    fuzzy_match = run_query("SELECT url FROM songs WHERE LOWER(title) LIKE ? AND url IS NOT NULL LIMIT 1", (f"%{query.lower()}%",), fetch=True)
    if fuzzy_match and fuzzy_match[0][0]: return fuzzy_match[0][0]

    db_fuzzy = _fuzzy_match_song_in_db(query)
    if db_fuzzy: return db_fuzzy

    return query 

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
        run_query("INSERT OR REPLACE INTO ratings (user_id, song_id, score) VALUES (?, ?, ?)", 
                  (user_id, self.song_id, score))
        
        label = "Banned" if score == -1 else f"Rated {score}"
        
        if score == -1:
            await interaction.response.send_message(embed=discord.Embed(description=f"You banned **{self.title}**. Skipping...", color=EMBED_RED), ephemeral=False)
            vc: wavelink.Player = interaction.guild.voice_client
            if vc:
                await vc.skip(force=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(description=f"You {label} for **{self.title}**", color=EMBED_BG), ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
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

async def find_and_play_random_song(guild: discord.Guild, voice_channel, text_channel):
    guild_id = guild.id
    if not voice_channel:
        radio_active[guild_id] = False
        return

    current_user_ids = [m.id for m in voice_channel.members if not m.bot]
    
    if not current_user_ids:
        if text_channel: 
            embed = discord.Embed(description="No one has been listening for the past 3 minutes, leaving", color=EMBED_RED)
            await text_channel.send(embed=embed)
        radio_active[guild_id] = False
        vc = guild.voice_client
        if vc:
            await vc.disconnect()
        return

    if guild_id not in played_history: played_history[guild_id] = set()
    if guild_id not in veto_pool: veto_pool[guild_id] = set()

    already_played = played_history[guild_id]
    placeholders = ','.join('?' for _ in current_user_ids)
    
    ban_query = f'SELECT DISTINCT song_id FROM ratings WHERE score = -1 AND user_id IN ({placeholders})'
    banned_song_ids = {row[0] for row in run_query(ban_query, tuple(current_user_ids), fetch=True)}

    threshold_query = f'SELECT min_rating FROM user_thresholds WHERE user_id IN ({placeholders})'
    threshold_rows = run_query(threshold_query, tuple(current_user_ids), fetch=True)
    max_threshold = max([r[0] for r in threshold_rows]) if threshold_rows else 0.0

    picked_from_veto = False
    chosen_data = None
    
    if random.random() < 0.5 and len(veto_pool[guild_id]) > 0:
        veto_candidates = list(veto_pool[guild_id])
        random.shuffle(veto_candidates)
        
        for v_id in veto_candidates:
            if v_id in banned_song_ids or v_id in already_played: continue
            
            ratings = [r[0] for r in run_query(f'SELECT score FROM ratings WHERE song_id = ? AND user_id IN ({placeholders})', (v_id, *current_user_ids), fetch=True)]
            clean_ratings = [r for r in ratings if r >= 0]
            true_avg = sum(clean_ratings) / len(clean_ratings) if clean_ratings else 0.0
            
            if true_avg < max_threshold: continue
            
            s_data = run_query("SELECT song_id, title, url, play_count FROM songs WHERE song_id = ?", (v_id,), fetch=True)
            if s_data:
                chosen_data = {'id': s_data[0][0], 'url': s_data[0][2], 'title': s_data[0][1], 'score': true_avg} 
                picked_from_veto = True
                veto_pool[guild_id].remove(v_id)
                break
    
    if not chosen_data:
        all_songs = run_query("SELECT song_id, title, url, play_count FROM songs", fetch=True)
        if not all_songs:
            if text_channel: await text_channel.send(embed=discord.Embed(description="Database empty.", color=EMBED_RED))
            radio_active[guild_id] = False
            return

        valid_songs_data, valid_songs_weights = [], []
        for song in all_songs:
            song_id, title, url, play_count = song
            play_count = play_count or 0
            
            if song_id in banned_song_ids:
                veto_pool[guild_id].add(song_id)
                continue
            if song_id in already_played: continue

            ratings = [r[0] for r in run_query(f'SELECT score FROM ratings WHERE song_id = ? AND user_id IN ({placeholders})', (song_id, *current_user_ids), fetch=True)]
            clean_ratings = [r for r in ratings if r >= 0]
            true_avg = sum(clean_ratings) / len(clean_ratings) if clean_ratings else 0.0
            
            if true_avg < max_threshold: continue

            play_weight = 100 / (play_count + 1)
            final_weight = max(1, play_weight + (true_avg * 10))
            
            valid_songs_data.append({'id': song_id, 'url': url, 'title': title, 'score': true_avg})
            valid_songs_weights.append(final_weight)

        if not valid_songs_data:
            if text_channel: 
                msg = "Ran out of music matching constraints." if len(already_played) > 0 else "No valid music found matching the active rating thresholds."
                await text_channel.send(embed=discord.Embed(description=msg, color=EMBED_RED))
            radio_active[guild_id] = False
            vc = guild.voice_client
            if vc: await vc.disconnect()
            return

        chosen_data = random.choices(valid_songs_data, weights=valid_songs_weights, k=1)[0]

    played_history[guild_id].add(chosen_data['id'])
    search_query = chosen_data['url'] if chosen_data['url'] else chosen_data['title']

    try:
        tracks = await wavelink.Playable.search(search_query)
        if not tracks:
            return await find_and_play_random_song(guild, voice_channel, text_channel)
        
        track = tracks[0]
        if not chosen_data['url']:
            run_query("UPDATE songs SET url = ?, title = ? WHERE song_id = ?", (track.uri, track.title, chosen_data['id']))

        vc: wavelink.Player = guild.voice_client
        if vc:
            await vc.queue.put_wait(track)
            if not vc.playing:
                await vc.play(vc.queue.get())
    except Exception as e:
        print(f"Radio Search Error: {e}")
        await find_and_play_random_song(guild, voice_channel, text_channel)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    count = import_songs_from_txt()
    if count > 0: print(f"Imported {count} songs on boot.")
    
    nodes = [wavelink.Node(uri="http://localhost:2333", password="youshallnotpass")]
    await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)

@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"Wavelink Node {payload.node.identifier} is ready!")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    if member.guild.voice_client:
        bot_channel = member.guild.voice_client.channel
        if before.channel == bot_channel and after.channel != bot_channel:
            await asyncio.sleep(5)
            members = [m for m in bot_channel.members if not m.bot]
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
    player = payload.player
    if not player: return

    if payload.reason in ["REPLACED", "STOPPED"]: return

    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)
    else:
        guild_id = player.guild.id
        channel = bound_channels.get(guild_id)
        if radio_active.get(guild_id, False):
            await find_and_play_random_song(player.guild, player.channel, channel)
        else:
            await cleanup_player_message(guild_id)

@bot.command()
async def help(ctx):
    commands_list = (
        "`b!play <query>` (Aliases: `b!p`) - Play a song or add it to the queue\n"
        "`b!skip` (Aliases: `b!s`) - Skip the current song\n"
        "`b!stop` (Aliases: `b!st`) - Stop the music and clear the queue\n"
        "`b!leave` (Aliases: `b!l`) - Disconnect the bot\n"
        "`b!queue` (Aliases: `b!q`) - Show current queue\n"
        "`b!loop [mode]` (Aliases: `b!lp`) - Toggle track/queue looping\n"
        "`b!pause` (Aliases: `b!pa`) - Pause the current song\n"
        "`b!resume` (Aliases: `b!re`) - Resume paused song\n"
        "`b!np` - Show what is currently playing\n"
        "`b!radio` (Aliases: `b!r`) - Start endless radio mode\n"
        "`b!minrating <score>` (Aliases: `b!mr`) - Set your personal radio rating threshold\n"
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
    if not vc: vc = await ctx.author.voice.channel.connect(cls=wavelink.Player)
    
    filters = NormalizedFilters()
    await vc.set_filters(filters)

    resolved_query = resolve_search_query(search)
    if not resolved_query: 
        return await ctx.send(embed=discord.Embed(description="Please provide a valid song name or url.", color=EMBED_RED))

    try:
        tracks = await wavelink.Playable.search(resolved_query)
        if not tracks: 
            return await ctx.send(embed=discord.Embed(description="Could not find that track.", color=EMBED_RED))
    except Exception as e:
        return await ctx.send(embed=discord.Embed(description=f"Error searching for track: {e}", color=EMBED_RED))
        
    track = tracks[0]
    await vc.queue.put_wait(track)

    if not vc.playing:
        await vc.play(vc.queue.get())
    else:
        embed = discord.Embed(description=f"Added to queue **[{track.title}]({track.uri})**", color=EMBED_BG)
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
        vc.queue.clear()
        await vc.stop()
        
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
async def queue(ctx):
    vc: wavelink.Player = ctx.voice_client
    if not vc or vc.queue.is_empty:
        return await ctx.send(embed=discord.Embed(description="Queue is empty.", color=EMBED_BG))
        
    queue_list = list(vc.queue)
    preview = queue_list[:10]
    
    lines = [f"**{idx+1}.** [{track.title}]({track.uri})" for idx, track in enumerate(preview)]
    if len(queue_list) > 10: 
        lines.append(f"\n*...and {len(queue_list)-10} more*")
        
    embed = discord.Embed(title="Queue", description="\n".join(lines), color=EMBED_BG)
    await ctx.send(embed=embed)

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
    await ctx.send(embed=discord.Embed(description="Paused the current track", color=EMBED_BG))

@bot.command(aliases=['re'])
async def resume(ctx):
    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.paused: 
        return await ctx.send(embed=discord.Embed(description="Nothing is paused right now.", color=EMBED_RED))
    await vc.pause(False)
    await ctx.send(embed=discord.Embed(description="Resumed the current track", color=EMBED_BG))

@bot.command(aliases=['l'])
async def leave(ctx):
    vc: wavelink.Player = ctx.voice_client
    radio_active[ctx.guild.id] = False
    if ctx.guild.id in played_history: played_history[ctx.guild.id].clear()
    if ctx.guild.id in veto_pool: veto_pool[ctx.guild.id].clear()

    await cleanup_player_message(ctx.guild.id)
    if vc:
        await vc.disconnect()
        await ctx.send(embed=discord.Embed(description="Disconnected from the voice channel", color=EMBED_BG))

@bot.command(aliases=['r'])
async def radio(ctx):
    if not ctx.message.author.voice: 
        return await ctx.send(embed=discord.Embed(description="Join a voice channel first.", color=EMBED_RED))
    
    bound_channels[ctx.guild.id] = ctx.channel
    vc: wavelink.Player = ctx.voice_client
    if not vc: await ctx.message.author.voice.channel.connect(cls=wavelink.Player)
    
    played_history[ctx.guild.id] = set()
    veto_pool[ctx.guild.id] = set()
    radio_active[ctx.guild.id] = True
    
    await ctx.send(embed=discord.Embed(description="**Started Radio Mode**", color=EMBED_BG))
    
    vc = ctx.voice_client
    if not vc.playing:
        await find_and_play_random_song(ctx.guild, ctx.author.voice.channel, ctx.channel)

@bot.command(aliases=['mr'])
async def minrating(ctx, score: float):
    if score < 0.0 or score > 3.0:
        return await ctx.send(embed=discord.Embed(description="Score must be between 0.0 and 3.0.", color=EMBED_RED))
        
    run_query("INSERT OR REPLACE INTO user_thresholds (user_id, min_rating) VALUES (?, ?)", (ctx.author.id, score))
    await ctx.send(embed=discord.Embed(description=f"Your personal radio rating threshold is set to **{score}**", color=EMBED_BG))

@bot.command()
async def rate(ctx, score: int, *, query: str):
    if score not in [-1, 0, 1, 2, 3]: 
        return await ctx.send(embed=discord.Embed(description="Score must be between -1 and 3.", color=EMBED_RED))

    search_term = f"%{query}%"
    results = run_query("SELECT song_id, title FROM songs WHERE title LIKE ?", (search_term,), fetch=True)
    if not results: 
        return await ctx.send(embed=discord.Embed(description=f"Could not find any song matching **{query}**.", color=EMBED_RED))

    best_match = results[0]
    song_id, song_title = best_match[0], best_match[1]
    user_id, username = ctx.author.id, ctx.author.name

    run_query("INSERT OR REPLACE INTO ratings (user_id, song_id, username, score) VALUES (?, ?, ?, ?)", (user_id, song_id, username, score))
    run_query("UPDATE ratings SET username = ? WHERE user_id = ? AND username IS NULL", (username, user_id))

    label = "Banned" if score == -1 else f"Rated {score}/3"
    await ctx.send(embed=discord.Embed(description=f"You {label} for **{song_title}**", color=EMBED_BG))

@bot.command()
async def library(ctx, page: int = 1):
    PER_PAGE = 10 
    offset = (page - 1) * PER_PAGE

    total_query = run_query("SELECT COUNT(*) FROM songs", fetch=True)
    total = total_query[0][0] if total_query else 0
    pages = (total // PER_PAGE) + 1
    
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
            elif score == -1: col = " X " 
            else: col = f" {score} " 
            user_cols.append(col)
        
        rating_str = " | ".join(user_cols)
        row_num = str(offset + idx + 1).zfill(2)
        rows_text += f"{row_num}  | {display_title} | {str(plays).center(5)} | {rating_str}\n"

    embed = discord.Embed(
        title=f"Library (Page {page}/{pages})", 
        description=f"```text\n{header}\n{divider}\n{rows_text}```", 
        color=EMBED_BG
    )
    embed.set_footer(text="Key: X = Banned, . = Not Rated")
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

bot.run(TOKEN)