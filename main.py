

import discord
from discord.ext import commands
from discord.ui import Button, View
import yt_dlp
import asyncio
import random
import datetime
import sqlite3
import os
import re

# --- Database Setup ---
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
    
    # Check for migration
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
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA journal_mode = WAL')
    conn.commit()
    conn.close()

setup_db()

# --- Settings ---
TOKEN = os.environ.get('DISCORD_TOKEN')
if not TOKEN and os.path.isfile('token.txt'):
    with open('token.txt', 'r', encoding='utf-8') as token_file:
        TOKEN = token_file.read().strip()

if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN environment variable or token.txt file is required')

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True 
# ⚠️ REQUIRED: Enable "Server Members Intent" in Developer Portal for this to work
intents.members = True 

bot = commands.Bot(command_prefix='b!', intents=intents)
bot.remove_command('help') 

# Global variables
queues = {}             
now_playing_msgs = {}
radio_active = {}       
played_history = {}     
veto_pool = {}          

DOWNLOAD_DIR = 'downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# yt-dlp options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': f'{DOWNLOAD_DIR}/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch', 
    'source_address': '0.0.0.0',
    'ffmpeg_location': os.environ.get('FFMPEG_PATH', 'ffmpeg'),
    'cachedir': False,
}

ffmpeg_options = {
    'options': '-vn -nostdin -af loudnorm=I=-16:LRA=11:TP=-1.5',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
}

# Ensure discord.FFmpegPCMAudio uses the ffmpeg binary provided by environment (set by nix-shell)
ffmpeg_options['executable'] = os.environ.get('FFMPEG_PATH', 'ffmpeg')

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# --- Search Helpers ---

def is_url(query):
    return query.startswith('http://') or query.startswith('https://')


def _normalize_text(text: str) -> str:
    return re.sub(r'[^a-z0-9 ]+', '', text.lower()).strip()


def _contains_keywords(text: str, keywords: list[str]) -> bool:
    return any(word in text for word in keywords)


def _is_remix_query(query: str) -> bool:
    return _contains_keywords(_normalize_text(query), [
        'remix', 'cover', 'karaoke', 'live', 'parody', 'instrumental', 'edit'
    ])


def _search_youtube_best_match(query: str) -> str:
    search_term = query
    if not _is_remix_query(query):
        search_term = f"{query} official audio"

    try:
        info = ytdl.extract_info(f"ytsearch5:{search_term}", download=False)
    except Exception:
        return f"ytsearch1:{query}"

    entries = info.get('entries') or []
    normalized_query = _normalize_text(query)
    best_entry = None
    best_score = -999

    penalty_terms = ['remix', 'cover', 'karaoke', 'live', 'parody', 'instrumental', 'reaction', 'bootleg']
    bonus_terms = ['official audio', 'official video', 'music video', 'audio', 'lyrics']

    for entry in entries:
        title = entry.get('title', '')
        title_norm = _normalize_text(title)
        if not title_norm:
            continue

        score = 0
        if title_norm == normalized_query:
            score += 100
        if title_norm.startswith(normalized_query):
            score += 50
        if normalized_query in title_norm:
            score += 30
        if _contains_keywords(title_norm, ['official audio', 'official video', 'music video']):
            score += 25
        if _contains_keywords(title_norm, ['audio', 'lyrics']):
            score += 10
        if _contains_keywords(title_norm, penalty_terms) and not _is_remix_query(query):
            score -= 50
        if _contains_keywords(title_norm, ['live', 'acoustic']) and not _is_remix_query(query):
            score -= 20
        if any(word in title_norm for word in normalized_query.split()):
            score += 5

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry and best_entry.get('webpage_url'):
        return best_entry['webpage_url']
    return f"ytsearch1:{query}"


def resolve_search_query(query):
    query = query.strip()
    if not query:
        return None

    if is_url(query):
        return query

    exact_match = run_query(
        "SELECT url FROM songs WHERE LOWER(title) = ? AND url IS NOT NULL", 
        (query.lower(),),
        fetch=True
    )
    if exact_match and exact_match[0][0]:
        return exact_match[0][0]

    fuzzy_match = run_query(
        "SELECT url FROM songs WHERE LOWER(title) LIKE ? AND url IS NOT NULL LIMIT 1", 
        (f"%{query.lower()}%",),
        fetch=True
    )
    if fuzzy_match and fuzzy_match[0][0]:
        return fuzzy_match[0][0]

    return _search_youtube_best_match(query)


# --- DB Helper ---
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

# --- Text File Import ---
def import_songs_from_txt():
    if not os.path.exists('songs.txt'):
        print("[Import] No 'songs.txt' file found. Skipping.")
        return 0

    print("[Import] checking songs.txt...")

    with open('songs.txt', 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if not lines:
        print("[Import] File is empty.")
        return 0

    count = 0
    for line in lines:
        song_name = line.strip()
        if song_name:
            existing = run_query("SELECT song_id FROM songs WHERE LOWER(title) = ?", (song_name.lower(),), fetch=True)
            if not existing:
                run_query("INSERT INTO songs (title, url, play_count) VALUES (?, NULL, 0)", (song_name,))
                count += 1
            else:
                print(f"[Import] Skipped duplicate: {song_name}")
    
    with open('songs.txt', 'w', encoding='utf-8') as f:
        pass
    
    if count > 0:
        print(f"[Import] SUCCESS: Added {count} new songs to database.")
    else:
        print("[Import] No new songs added.")
    
    return count

# --- UI View for Ratings ---
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
            await interaction.response.send_message(f"You banned {self.title}. Skipping...", ephemeral=False)
            if interaction.guild.voice_client:
                interaction.guild.voice_client.stop()
        else:
            await interaction.response.send_message(f"You {label} for {self.title}", ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.primary)
    async def ban_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, -1)

    @discord.ui.button(label="0", style=discord.ButtonStyle.primary)
    async def zero_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 0)

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def one_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 1)
        
    @discord.ui.button(label="2", style=discord.ButtonStyle.primary)
    async def two_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def three_button(self, interaction: discord.Interaction, button: Button):
        await self.save_rating(interaction, 3)

# --- Player Logic ---

class YTDLSource:
    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        filename = os.path.abspath(filename)
        per_play_opts = ffmpeg_options.copy()

        if not stream:
            per_play_opts.pop('before_options', None)
            per_play_opts['options'] = '-vn -nostdin -hide_banner'

        print(f"[YTDL] source={url} local={not stream} file={filename} ffmpeg_opts={per_play_opts}")
        source = await discord.FFmpegOpusAudio.from_probe(
            filename,
            method='fallback',
            executable=per_play_opts.get('executable'),
            before_options=per_play_opts.get('before_options'),
            options=per_play_opts.get('options')
        )

        source.title = data.get('title')
        source.url = data.get('url')
        source.web_url = data.get('webpage_url')
        source.uploader = data.get('uploader')
        source.thumbnail = data.get('thumbnail')
        source.duration = data.get('duration')
        return source

def create_embed(player, play_count):
    duration = str(datetime.timedelta(seconds=player.duration)) if player.duration else "Unknown"
    embed = discord.Embed(
        title=player.title, 
        url=player.web_url, 
        description=f"**Artist:** {player.uploader}\n**Time:** {duration}\n**Total Plays:** {play_count}", 
        color=discord.Color.blue()
    )
    if player.thumbnail: embed.set_thumbnail(url=player.thumbnail)
    return embed

def check_queue(ctx):
    guild_id = ctx.guild.id
    
    if guild_id in queues and queues[guild_id]:
        next_url = queues[guild_id].pop(0)
        bot.loop.create_task(play_next_song(ctx, next_url))
        return

    if radio_active.get(guild_id, False):
        bot.loop.create_task(find_and_play_random_song(ctx))
        return

    bot.loop.create_task(cleanup_player_message(guild_id))

async def cleanup_player_message(guild_id):
    if guild_id in now_playing_msgs:
        try: await now_playing_msgs[guild_id].delete()
        except: pass
        del now_playing_msgs[guild_id]

async def play_next_song(ctx, url_or_search):
    voice_client = ctx.guild.voice_client
    if not voice_client: return

    try:
        # Download the track to a local file first to avoid remote 403/forbidden errors
        player = await YTDLSource.from_url(url_or_search, loop=bot.loop, stream=False)

        voice_client.play(player, after=lambda e: check_queue(ctx))
        
        current_play_count = 0
        song_id = None
        existing_id = run_query("SELECT song_id, play_count FROM songs WHERE url = ?", (player.web_url,), fetch=True)
        if existing_id:
            current_play_count = (existing_id[0][1] or 0) + 1
            song_id = existing_id[0][0]
            run_query("UPDATE songs SET title = ?, play_count = ?, last_played = CURRENT_TIMESTAMP WHERE url = ?", 
                      (player.title, current_play_count, player.web_url))
        else:
            manual_match = run_query(
                "SELECT song_id, play_count FROM songs WHERE LOWER(title) = ? AND url IS NULL LIMIT 1",
                (player.title.lower(),),
                fetch=True
            )
            if manual_match:
                current_play_count = (manual_match[0][1] or 0) + 1
                song_id = manual_match[0][0]
                run_query("UPDATE songs SET url = ?, play_count = ?, last_played = CURRENT_TIMESTAMP WHERE song_id = ?", 
                          (player.web_url, current_play_count, song_id))

        if song_id is None:
            embed = discord.Embed(
                title=player.title,
                description=f"Playing: {player.title}",
                color=discord.Color.blue()
            )
            await cleanup_player_message(ctx.guild.id)
            msg = await ctx.send(embed=embed)
            now_playing_msgs[ctx.guild.id] = msg
            return

        embed = create_embed(player, current_play_count)
        view = RatingView(song_id, player.title)

        guild_id = ctx.guild.id
        await cleanup_player_message(guild_id)

        msg = await ctx.send(embed=embed, view=view)
        now_playing_msgs[guild_id] = msg

    except Exception as e:
        await ctx.send(f"Error playing track: {e}")
        check_queue(ctx)

async def find_and_play_random_song(ctx):
    guild_id = ctx.guild.id
    if not ctx.voice_client or not ctx.voice_client.channel:
        radio_active[guild_id] = False
        return

    current_user_ids = [m.id for m in ctx.voice_client.channel.members if not m.bot]
    
    if not current_user_ids:
        await ctx.send("No one is listening. Stopping radio.")
        radio_active[guild_id] = False
        return

    if guild_id not in played_history: played_history[guild_id] = set()
    if guild_id not in veto_pool: veto_pool[guild_id] = set()

    already_played = played_history[guild_id]

    placeholders = ','.join('?' for _ in current_user_ids)
    ban_query = f'SELECT DISTINCT song_id FROM ratings WHERE score = -1 AND user_id IN ({placeholders})'
    params = tuple(current_user_ids)
    banned_song_ids = {row[0] for row in run_query(ban_query, params, fetch=True)}

    picked_from_veto = False
    chosen_data = None
    
    # 50% Redemption Chance
    if random.random() < 0.5 and len(veto_pool[guild_id]) > 0:
        veto_candidates = list(veto_pool[guild_id])
        random.shuffle(veto_candidates)
        
        for v_id in veto_candidates:
            if v_id in banned_song_ids: continue 
            if v_id in already_played: continue

            s_data = run_query("SELECT song_id, title, url, play_count FROM songs WHERE song_id = ?", (v_id,), fetch=True)
            if s_data:
                sid, stitle, surl, splay = s_data[0]
                chosen_data = {'id': sid, 'url': surl, 'title': stitle, 'score': 0} 
                picked_from_veto = True
                veto_pool[guild_id].remove(v_id)
                break
    
    # Normal Selection
    if not chosen_data:
        all_songs = run_query("SELECT song_id, title, url, play_count FROM songs", fetch=True)
        if not all_songs:
            await ctx.send("Database empty.")
            radio_active[guild_id] = False
            return

        valid_songs_data = []
        valid_songs_weights = []
        
        for song in all_songs:
            song_id, title, url, play_count = song
            play_count = play_count or 0
            
            if song_id in banned_song_ids:
                veto_pool[guild_id].add(song_id)
                continue
            
            if song_id in already_played:
                continue

            score_query = f'SELECT score FROM ratings WHERE song_id = ? AND user_id IN ({placeholders})'
            score_params = (song_id, *current_user_ids)
            ratings = [r[0] for r in run_query(score_query, score_params, fetch=True)]
            user_rating_avg = sum(ratings) if ratings else 0
            
            play_weight = 100 / (play_count + 1)
            rating_bonus = user_rating_avg * 10 
            final_weight = max(1, play_weight + rating_bonus)
            
            valid_songs_data.append({'id': song_id, 'url': url, 'title': title, 'score': user_rating_avg})
            valid_songs_weights.append(final_weight)

        if not valid_songs_data:
            if len(already_played) > 0:
                await ctx.send("Ran out of music! Session complete.")
            else:
                await ctx.send("No valid music found (everything is banned).")
            radio_active[guild_id] = False
            if ctx.voice_client: await ctx.voice_client.disconnect()
            return

        chosen_data = random.choices(valid_songs_data, weights=valid_songs_weights, k=1)[0]

    played_history[guild_id].add(chosen_data['id'])
    
    final_url = chosen_data['url']
    if final_url is None:
        await ctx.send(f"First time playing {chosen_data['title']}. Searching link...")
        try:
            info = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch1:{chosen_data['title']}", download=False))
            if 'entries' in info and len(info['entries']) > 0:
                final_url = info['entries'][0]['webpage_url']
                real_title = info['entries'][0]['title']
                run_query("UPDATE songs SET url = ?, title = ? WHERE song_id = ?", 
                          (final_url, real_title, chosen_data['id']))
            else:
                await ctx.send(f"Could not find '{chosen_data['title']}'. Skipping.")
                return await find_and_play_random_song(ctx)
        except Exception as e:
            print(e)
            return await find_and_play_random_song(ctx)

    if picked_from_veto:
        await ctx.send(f"Redemption: Playing previously banned song {chosen_data['title']}")
    else:
        await ctx.send(f"Radio: Playing {chosen_data['title']} (Rating: {chosen_data['score']})")
    
    await play_next_song(ctx, final_url)

# --- Events ---

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    import_songs_from_txt()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    if member.guild.voice_client:
        bot_channel = member.guild.voice_client.channel
        if before.channel == bot_channel and after.channel != bot_channel:
            print(f"[Spectator] {member.name} left. Adjusting bans next track.")

# --- Commands ---

@bot.command()
async def help(ctx):
    """custom help command."""
    embed = discord.Embed(title="bot commands", color=discord.Color.blue())
    
    commands_list = (
        f"{'b!play <query> / b!p <query>':<35} : play song or add to queue\n"
        f"{'b!skip / b!s':<35} : skip current song\n"
        f"{'b!stop / b!st':<35} : stop music and clear queue\n"
        f"{'b!leave / b!l':<35} : disconnect the bot\n"
        f"{'b!queue / b!q':<35} : show current queue\n"
        f"{'b!pause / b!pa':<35} : pause current song\n"
        f"{'b!resume / b!re':<35} : resume paused song\n"
        f"{'b!np':<35} : show now playing\n"
        f"{'b!radio / b!r':<35} : start radio mode\n"
        f"{'b!library [page]':<35} : show song database table\n"
        f"{'b!add <query>':<35} : add song (link or name)\n"
        f"{'b!rate <val> <name>':<35} : rate song by name (-1 to 3)\n"
        f"{'b!import':<35} : import songs.txt file"
    )
    
    embed.description = f"```text\n{commands_list}```"
    await ctx.send(embed=embed)

@bot.command(aliases=['p'])
async def play(ctx, *, search: str):
    if not ctx.message.author.voice:
        return await ctx.send("join a voice channel first.")
    if not ctx.voice_client:
        await ctx.message.author.voice.channel.connect()

    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = []

    resolved_query = resolve_search_query(search)
    if resolved_query is None:
        return await ctx.send("please provide a valid song name or url.")

    if ctx.voice_client.is_playing():
        queues[guild_id].append(resolved_query)
        await ctx.send(f'added to queue: {search}', delete_after=5)
    else:
        await play_next_song(ctx, resolved_query)

@bot.command(aliases=['s'])
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("skipped.", delete_after=5)

@bot.command(aliases=['st'])
async def stop(ctx):
    if ctx.guild.id in queues: queues[ctx.guild.id].clear()
    radio_active[ctx.guild.id] = False
    if ctx.guild.id in played_history: played_history[ctx.guild.id].clear()
    if ctx.guild.id in veto_pool: veto_pool[ctx.guild.id].clear()

    if ctx.voice_client: ctx.voice_client.stop()
    await cleanup_player_message(ctx.guild.id)
    await ctx.send("Stopped.", delete_after=5)

@bot.command(name='np')
async def now_playing(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        return await ctx.send("nothing is playing right now.")
    source = ctx.voice_client.source
    title = getattr(source, 'title', None) or getattr(source, 'title', 'unknown')
    await ctx.send(f"now playing: {title}")

@bot.command(aliases=['q'])
async def queue(ctx):
    queue_list = queues.get(ctx.guild.id, [])
    if not queue_list:
        return await ctx.send("Queue is empty.")
    preview = queue_list[:10]
    lines = [f"{idx+1}. {item}" for idx, item in enumerate(preview)]
    if len(queue_list) > 10:
        lines.append(f"...and {len(queue_list)-10} more")
    await ctx.send("Queue:\n" + "\n".join(lines))

@bot.command(aliases=['pa'])
async def pause(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        return await ctx.send("nothing is playing right now.")
    ctx.voice_client.pause()
    await ctx.send("paused.")

@bot.command(aliases=['re'])
async def resume(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_paused():
        return await ctx.send("nothing is paused.")
    ctx.voice_client.resume()
    await ctx.send("resumed.")

@bot.command(aliases=['l'])
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in queues: queues[guild_id].clear()
    radio_active[guild_id] = False
    if guild_id in played_history: played_history[guild_id].clear()
    if guild_id in veto_pool: veto_pool[guild_id].clear()

    await cleanup_player_message(guild_id)
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Disconnected.", delete_after=5)

@bot.command(aliases=['r'])
async def radio(ctx):
    if not ctx.message.author.voice: return await ctx.send("join voice first.")
    if not ctx.voice_client: await ctx.message.author.voice.channel.connect()
    
    played_history[ctx.guild.id] = set()
    veto_pool[ctx.guild.id] = set()
    
    radio_active[ctx.guild.id] = True
    await find_and_play_random_song(ctx)

@bot.command()
async def rate(ctx, score: int, *, query: str):
    if score not in [-1, 0, 1, 2, 3]:
        return await ctx.send("Score must be one of: -1, 0, 1, 2, 3")

    search_term = f"%{query}%"
    results = run_query("SELECT song_id, title FROM songs WHERE title LIKE ?", (search_term,), fetch=True)

    if not results:
        return await ctx.send(f"Could not find any song matching '{query}' in the database.")

    best_match = results[0]
    song_id = best_match[0]
    song_title = best_match[1]
    user_id = ctx.author.id
    username = ctx.author.name

    run_query("INSERT OR REPLACE INTO ratings (user_id, song_id, username, score) VALUES (?, ?, ?, ?)", 
              (user_id, song_id, username, score))
    run_query("UPDATE ratings SET username = ? WHERE user_id = ? AND username IS NULL", (username, user_id))

    label = "Banned" if score == -1 else f"Rated {score}/3"
    await ctx.send(f"Rating saved. You {label} for {song_title}.")

@bot.command()
async def library(ctx, page: int = 1):
    PER_PAGE = 10 
    offset = (page - 1) * PER_PAGE

    total = run_query("SELECT COUNT(*) FROM songs", fetch=True)[0][0]
    pages = (total // PER_PAGE) + 1
    
    if page < 1 or page > pages:
        return await ctx.send(f"Invalid page. Total pages: {pages}")

    # Rater columns
    rater_rows = run_query("SELECT DISTINCT user_id FROM ratings", fetch=True)
    all_rater_ids = [r[0] for r in rater_rows] if rater_rows else []

    # FETCH NAME LOGIC: Tries Cache -> then API -> then fallback to ID
    rater_map = {} 
    for uid in all_rater_ids:
        member = ctx.guild.get_member(uid)
        if not member:
            try:
                member = await ctx.guild.fetch_member(uid)
            except:
                member = None
        
        if member:
            name = member.display_name[:3] 
        else:
            name = str(uid)[:3] 
        rater_map[uid] = name

    # Get songs
    songs = run_query("""
        SELECT song_id, title, play_count 
        FROM songs 
        ORDER BY play_count DESC 
        LIMIT ? OFFSET ?
    """, (PER_PAGE, offset), fetch=True)

    if not songs:
        return await ctx.send("The database is empty.")

    # Header - Title reduced to 18 chars, formatted strictly
    user_headers = " | ".join([name.center(3) for name in rater_map.values()])
    header = f"ID  | {'Title':<18} | Plays | {user_headers}"
    divider = "-" * len(header)

    rows_text = ""
    for song in songs:
        s_id, title, plays = song
        
        # Reduced width to prevent wrapping (18 chars max)
        if len(title) > 18:
            display_title = title[:16] + ".."
        else:
            display_title = title.ljust(18)
        
        ratings_raw = run_query("SELECT user_id, score FROM ratings WHERE song_id = ?", (s_id,), fetch=True)
        song_ratings = {r[0]: r[1] for r in ratings_raw}

        user_cols = []
        for uid in all_rater_ids:
            score = song_ratings.get(uid, None)
            if score is None:
                col = " . " 
            elif score == -1:
                col = " X " 
            else:
                col = f" {score} " 
            user_cols.append(col)
        
        rating_str = " | ".join(user_cols)
        row_num = str(offset + songs.index(song) + 1).zfill(2)
        
        rows_text += f"{row_num}  | {display_title} | {str(plays).center(5)} | {rating_str}\n"

    final_output = f"```text\n{header}\n{divider}\n{rows_text}```"

    embed = discord.Embed(title=f"Library (Page {page}/{pages})", color=discord.Color.blue())
    embed.description = final_output
    embed.set_footer(text="Key: X = Banned, . = Not Rated")
    
    await ctx.send(embed=embed)

@bot.command()
async def add(ctx, *, query: str):
    if query.startswith("http"):
        try:
            existing = run_query("SELECT song_id FROM songs WHERE url = ?", (query,), fetch=True)
            if existing: return await ctx.send("This link is already in the database.")
            run_query("INSERT INTO songs (title, url, play_count) VALUES (?, ?, 0)", ("Unknown Title", query))
            await ctx.send(f"Added link: <{query}>")
        except Exception as e:
            await ctx.send(f"Error adding link: {e}")
    else:
        try:
            existing = run_query("SELECT song_id FROM songs WHERE LOWER(title) = ?", (query.lower(),), fetch=True)
            if existing: return await ctx.send(f"Song '{query}' is already in the database.")
            run_query("INSERT INTO songs (title, url, play_count) VALUES (?, NULL, 0)", (query,))
            await ctx.send(f"Added song name: **{query}**")
        except Exception as e:
            await ctx.send(f"Error adding name: {e}")

@bot.command(name="import")
async def import_songs(ctx):
    """Manually triggers the songs.txt import."""
    count = import_songs_from_txt()
    if count > 0:
        await ctx.send(f"Imported {count} new songs from songs.txt.")
    else:
        await ctx.send("No new songs found in songs.txt (or file is missing).")

bot.run(TOKEN)

