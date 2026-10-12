"""Companion Discord clients that follow one primary player's playback."""
import asyncio
import functools
import logging
import os

import discord
import wavelink

log = logging.getLogger(__name__)


class Companion(discord.Client):
    def __init__(self, number):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        super().__init__(intents=intents)
        self.number = number
        self.node = None
        self.connect_lock = asyncio.Lock()

    async def on_ready(self):
        async with self.connect_lock:
            if self.node is None:
                self.node = wavelink.Node(
                    uri=os.environ.get('LAVALINK_URI', 'http://localhost:2333'),
                    password=os.environ.get('LAVALINK_PASSWORD', 'youshallnotpass'),
                    identifier=f'companion-{self.number}')
                connected = await wavelink.Pool.connect(nodes=[self.node], client=self)
                if self.node.identifier not in connected:
                    self.node = None
                    log.error('Companion %s could not connect to Lavalink', self.number)
        log.info('Sync companion %s is ready', self.number)


class SyncPlayback:
    def __init__(self, primary, tokens):
        self.primary = primary
        self.tokens = tokens
        self.clients = [Companion(i + 1) for i in range(len(tokens))]
        self.groups = {}  # guild -> [(client, player)]
        self.locks = {}
        self.tasks = []
        self.before_leave = None

    def listeners(self, guild_id):
        guild = self.primary.get_guild(guild_id)
        players = [guild.voice_client] if guild and guild.voice_client else []
        players.extend(player for _, player in self.groups.get(guild_id, []))
        return {member.id for player in players if player.connected and player.channel
                for member in player.channel.members if not member.bot}

    def contains(self, guild_id, channel_id):
        guild = self.primary.get_guild(guild_id)
        players = [guild.voice_client] if guild and guild.voice_client else []
        players.extend(player for _, player in self.groups.get(guild_id, []))
        return any(player.channel and player.channel.id == channel_id for player in players)

    async def join(self, guild_id, channels):
        if len(channels) > len(self.clients):
            raise ValueError('Configure DISCORD_SYNC_TOKEN_2 and optionally DISCORD_SYNC_TOKEN_3 first.')
        primary = self.primary.get_guild(guild_id).voice_client
        if not primary:
            raise ValueError('Start b!play or b!radio in the primary channel first.')
        ids = [channel.id for channel in channels]
        if len(set(ids)) != len(ids) or primary.channel.id in ids:
            raise ValueError('Choose distinct voice channels, excluding the primary channel.')
        targets = []
        for client, channel in zip(self.clients, channels):
            guild = client.get_guild(guild_id)
            if not client.is_ready() or client.node is None or guild is None:
                raise ValueError('A companion is not ready or has not been invited to this server.')
            target = guild.get_channel(channel.id)
            if target is None:
                raise ValueError('A companion cannot see that channel.')
            targets.append((client, target))
        await self.leave(guild_id)
        self.groups[guild_id] = []
        try:
            for client, target in targets:
                player = await target.connect(cls=functools.partial(wavelink.Player, nodes=[client.node]))
                player.autoplay = wavelink.AutoPlayMode.disabled
                self.groups[guild_id].append((client, player))
            await self.refresh(guild_id, restart=True)
        except Exception:
            await self.leave(guild_id)
            raise

    async def leave(self, guild_id):
        async with self.locks.setdefault(guild_id, asyncio.Lock()):
            if self.before_leave:
                await self.before_leave(guild_id)
            followers = self.groups.pop(guild_id, [])
            results = await asyncio.gather(*(player.disconnect() for _, player in followers), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.warning('Companion disconnect failed: %s', result)

    async def refresh(self, guild_id, restart=False):
        async with self.locks.setdefault(guild_id, asyncio.Lock()):
            guild = self.primary.get_guild(guild_id)
            primary = guild.voice_client if guild else None
            if not primary or not primary.connected:
                return
            async def follow(player):
                if not player.connected:
                    return
                track = primary.current
                if track is None:
                    if player.current:
                        await player.stop()
                    return
                if restart or player.current is None or player.current.encoded != track.encoded:
                    await player.play(track, start=primary.position if track.is_seekable else 0,
                                      paused=primary.paused, volume=primary.volume, filters=primary.filters)
                else:
                    if player.paused != primary.paused:
                        await player.pause(primary.paused)
                    if player.volume != primary.volume:
                        await player.set_volume(primary.volume)
                    if track.is_seekable and abs(player.position - primary.position) > 750:
                        await player.seek(primary.position)
            results = await asyncio.gather(*(follow(player) for _, player in self.groups.get(guild_id, [])),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.warning('Companion sync failed: %s', result)

    async def maintain(self):
        while True:
            await asyncio.sleep(2)
            for guild_id in list(self.groups):
                guild = self.primary.get_guild(guild_id)
                if not guild or not guild.voice_client or not guild.voice_client.connected:
                    await self.leave(guild_id)
                else:
                    try:
                        await self.refresh(guild_id)
                    except Exception:
                        log.exception('Could not refresh sync group %s', guild_id)

    async def start(self):
        self.tasks = [asyncio.create_task(client.start(token)) for client, token in zip(self.clients, self.tokens)]
        self.tasks.append(asyncio.create_task(self.maintain()))
        # Surface failed logins without terminating the working primary bot.
        for task in self.tasks:
            task.add_done_callback(self.report_failure)

    @staticmethod
    def report_failure(task):
        if not task.cancelled() and task.exception():
            log.error('Sync task stopped: %s', task.exception())

    async def close(self):
        for guild_id in list(self.groups):
            await self.leave(guild_id)
        await asyncio.gather(*(client.close() for client in self.clients), return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
