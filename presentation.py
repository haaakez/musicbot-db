"""Update now-playing ratings and voice statuses without touching playback."""
import asyncio
import datetime
import logging
import sqlite3
import time

import discord

from preferences import read_settings

log = logging.getLogger(__name__)


class TrackPresentation:
    def __init__(self, bot, sync, db_name):
        self.bot = bot
        self.sync = sync
        self.db_name = db_name
        self.messages = {}
        self.fingerprints = {}
        self.statuses = {}  # channel ID -> (channel object, status set by this bot)
        self.retry_after = {}
        self.lock = asyncio.Lock()

    def build_embed(self, track, plays, song_id, show_ratings):
        title = discord.utils.escape_markdown(track.title[:300])
        embed = discord.Embed(color=0x2b2d31)
        source = getattr(track, 'source', '') or 'music'
        embed.set_author(name=f'{source} · started playing')
        playing = f'**[{title}]({track.uri})**' if track.uri else f'**{title}**'
        embed.add_field(name='playing', value=playing, inline=False)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        seconds = max(0, int(track.length or 0) // 1000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = 'live' if track.is_stream else (f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}')
        embed.add_field(name='length', value=duration if track.length or track.is_stream else 'unknown', inline=True)
        embed.add_field(name='plays', value=str(plays), inline=True)
        requester = getattr(track.extras, 'requester_name', None)
        if requester:
            embed.set_footer(text=f'requested by {requester}', icon_url=getattr(track.extras, 'requester_avatar', None))
        else:
            embed.set_footer(text='radio')
        started = getattr(track.extras, 'started_at', None)
        if started:
            embed.timestamp = datetime.datetime.fromisoformat(started)
        if show_ratings and song_id is not None:
            with sqlite3.connect(self.db_name, timeout=30) as conn:
                rows = conn.execute('''SELECT u.user_id, u.username, r.score FROM users u
                    LEFT JOIN ratings r ON r.user_id=u.user_id AND r.song_id=?
                    ORDER BY u.username COLLATE NOCASE, u.user_id''', (song_id,)).fetchall()
            lines = [f"{discord.utils.escape_markdown((name or f'user {uid}')[:80])}: "
                     f"**{score if score is not None else '—'}**" for uid, name, score in rows]
            if not lines:
                lines = ['no ratings yet']
            chunks, chunk = [], ''
            for line in lines:
                if len(chunk) + len(line) + 1 > 1000:
                    chunks.append(chunk)
                    chunk = ''
                chunk += ('\n' if chunk else '') + line
            if chunk:
                chunks.append(chunk)
            for i, value in enumerate(chunks[:5]):
                embed.add_field(name='ratings' if i == 0 else 'ratings (continued)', value=value, inline=False)
        elif show_ratings:
            embed.add_field(name='ratings', value='this track is not in the library', inline=False)
        return embed

    def remember(self, guild_id, message, track, song_id, plays, embed):
        self.messages[guild_id] = (message, track, song_id, plays)
        self.fingerprints[guild_id] = embed.to_dict()

    async def set_status(self, channel, status):
        if channel is None:
            return
        previous = self.statuses.get(channel.id)
        if (previous and previous[1] == status) or (status is None and not previous):
            return
        if self.retry_after.get(channel.id, 0) > time.monotonic():
            return
        try:
            await channel.edit(status=status, reason='music bot now playing')
        except discord.Forbidden:
            self.retry_after[channel.id] = time.monotonic() + 60
            log.warning('Cannot update voice status in %s: grant Set Voice Channel Status.', channel.id)
        except discord.HTTPException as error:
            self.retry_after[channel.id] = time.monotonic() + 60
            log.warning('Voice status update failed in %s: %s', channel.id, error)
        else:
            self.retry_after.pop(channel.id, None)
            if status is None:
                self.statuses.pop(channel.id, None)
            else:
                self.statuses[channel.id] = (channel, status)

    async def refresh(self, guild_id):
        async with self.lock:
            guild = self.bot.get_guild(guild_id)
            player = guild.voice_client if guild else None
            settings = await asyncio.to_thread(read_settings, self.db_name, guild_id)
            track = player.current if player else None
            connected = [player] if player and player.connected else []
            connected.extend(p for _, p in self.sync.groups.get(guild_id, []) if p.connected)
            status = track.title[:500] if settings['voice_status'] and track else None
            active_ids = {p.channel.id for p in connected if p.channel}
            for channel_id, (channel, _) in list(self.statuses.items()):
                if channel.guild.id == guild_id and channel_id not in active_ids:
                    await self.set_status(channel, None)
            await asyncio.gather(*(self.set_status(p.channel, status) for p in connected))
            entry = self.messages.get(guild_id)
            if not entry or not track or entry[1].encoded != track.encoded:
                return
            message, remembered_track, song_id, plays = entry
            embed = await asyncio.to_thread(self.build_embed, remembered_track, plays, song_id, settings['embed_ratings'])
            if self.fingerprints.get(guild_id) != embed.to_dict():
                try:
                    await message.edit(embed=embed)
                except discord.NotFound:
                    self.messages.pop(guild_id, None)
                    self.fingerprints.pop(guild_id, None)
                except discord.HTTPException:
                    log.exception('Could not update now-playing ratings in %s', guild_id)
                else:
                    self.fingerprints[guild_id] = embed.to_dict()

    async def clear(self, guild_id):
        self.messages.pop(guild_id, None)
        self.fingerprints.pop(guild_id, None)
        await self.clear_statuses(guild_id)

    async def clear_followers(self, guild_id):
        async with self.lock:
            for _, player in self.sync.groups.get(guild_id, []):
                await self.set_status(player.channel, None)

    async def clear_statuses(self, guild_id):
        async with self.lock:
            for channel, _ in list(self.statuses.values()):
                if channel.guild.id == guild_id:
                    await self.set_status(channel, None)

    async def maintain(self):
        while True:
            await asyncio.sleep(5)
            guild_ids = {guild.id for guild in self.bot.guilds} | set(self.messages)
            for guild_id in guild_ids:
                try:
                    await self.refresh(guild_id)
                except Exception:
                    log.exception('Could not refresh track display in %s', guild_id)
