"""Checkpoint playback and restore it after a process or connection restart."""
import asyncio
from contextlib import closing
import json
import copy
import functools
import sqlite3
import time

import wavelink
from radio import eligible


def setup_recovery(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS playback_sessions (
        guild_id INTEGER PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL)''')


def write_session(database, guild_id, state):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        if state is None:
            conn.execute('DELETE FROM playback_sessions WHERE guild_id=?', (guild_id,))
        else:
            conn.execute('''INSERT INTO playback_sessions VALUES(?,?,?) ON CONFLICT(guild_id)
                DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload''',
                         (guild_id, time.time(), json.dumps(state)))


def read_sessions(database):
    with closing(sqlite3.connect(database, timeout=30)) as conn:
        return {gid: json.loads(payload) for gid,payload in conn.execute(
            'SELECT guild_id,payload FROM playback_sessions WHERE updated_at>?', (time.time()-86400,))}


def saved_radio_allowed(database, guild_id, song_id, listeners):
    with closing(sqlite3.connect(database,timeout=30)) as conn:
        scores = dict(conn.execute('SELECT user_id,score FROM ratings WHERE song_id=?',(song_id,)))
        limits = dict(conn.execute('SELECT user_id,min_rating FROM user_thresholds'))
        row = conn.execute('SELECT ignore_unrated FROM guild_settings WHERE guild_id=?',(guild_id,)).fetchone()
    return eligible(scores,listeners,limits,bool(row[0]) if row else True)


class PlaybackRecovery:
    def __init__(self, bot, sync, database, radio, history, channels, trackers, play, locks, waiting):
        self.bot, self.sync, self.database = bot, sync, database
        self.radio, self.history, self.channels, self.trackers = radio, history, channels, trackers
        self.play, self.locks, self.waiting = play, locks, waiting
        self.pending = {}
        self.disabled = set()
        self.retry_at = {}
        self.attempts = {}
        self.companion_pending = {}
        self.companion_retry_at = {}

    async def load(self):
        self.pending = await asyncio.to_thread(read_sessions, self.database)

    async def forget(self, guild_id):
        self.disabled.add(guild_id)
        self.pending.pop(guild_id, None)
        self.companion_pending.pop(guild_id, None)
        await asyncio.to_thread(write_session, self.database, guild_id, None)

    def enable(self, guild_id):
        self.disabled.discard(guild_id)
        self.pending.pop(guild_id, None)
        self.attempts.pop(guild_id, None)

    async def checkpoint(self, guild):
        player = guild.voice_client
        if guild.id in self.disabled or guild.id in self.pending or not player or not player.connected:
            return
        if getattr(player.node.status, 'name', '') != 'CONNECTED':
            return
        current = player.current
        tracks = list(player.queue)
        if not current and not tracks and not self.radio.get(guild.id):
            await asyncio.to_thread(write_session, self.database, guild.id, None)
            return
        tracker = self.trackers.get(guild.id, {})
        if not current or tracker.get('key') != getattr(current.extras, 'playback_id', None):
            tracker = {}
        state = {'voice': player.channel.id, 'text': getattr(self.channels.get(guild.id), 'id', None),
                 'current': copy.deepcopy(current.raw_data) if current else None, 'position': player.position,
                 'paused': player.paused, 'volume': player.volume, 'loop': player.queue.mode.name,
                 'queue': [copy.deepcopy(track.raw_data) for track in tracks], 'radio': bool(self.radio.get(guild.id)),
                 'history': list(self.history.get(guild.id, set())),
                 'companions': self.companion_pending.get(guild.id) or [p.channel.id for _,p in self.sync.groups.get(guild.id, []) if p.channel],
                 'history_id': tracker.get('history_id'), 'heard_ms': tracker.get('heard_ms', 0)}
        state['queue_history'] = []
        if player.queue.mode == wavelink.QueueMode.loop_all and player.queue.history:
            for track in player.queue.history:
                data = copy.deepcopy(track.raw_data)
                data['userData'] = dict(track.extras)
                state['queue_history'].append(data)
        # raw_data may not reflect assignments to extras, so store those explicitly.
        if current:
            state['current']['userData'] = dict(current.extras)
        for data,track in zip(state['queue'],tracks):
            data['userData'] = dict(track.extras)
        await asyncio.to_thread(write_session, self.database, guild.id, state)

    async def disconnected(self):
        saved = await asyncio.to_thread(read_sessions, self.database)
        for gid,state in saved.items():
            if gid not in self.disabled:
                self.pending[gid] = state
                self.attempts.pop(gid, None)

    async def restore(self, guild_id, state):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(state['voice'])
        channels = [channel] + [guild.get_channel(cid) for cid in state['companions']]
        if channel is None or not any(not member.bot for target in channels if target for member in getattr(target,'members',[])):
            return  # Keep the checkpoint until listeners return.
        async with self.locks.setdefault(guild_id, asyncio.Lock()):
            if guild_id not in self.pending or guild_id in self.disabled:
                return
            if guild.voice_client:
                # Let an already-resumed Lavalink session continue without replaying it.
                info = await guild.voice_client.node.fetch_player_info(guild_id)
                if info and info.track and info.state.connected:
                    self.pending.pop(guild_id, None)
                    return
                await self.sync.leave(guild_id)
                await guild.voice_client.disconnect()
            player = await channel.connect(cls=functools.partial(wavelink.Player,nodes=[wavelink.Pool.get_node('primary')]))
            self.radio[guild_id] = state['radio']
            if state['radio'] and not state['current'] and not state['queue']:
                self.waiting[guild_id] = 'Waiting for eligible tracks'
            self.history[guild_id] = set(state['history'])
            if state['text']:
                text = guild.get_channel(state['text'])
                if text: self.channels[guild_id] = text
            player.queue.reset()
            for data in state['queue']:
                await player.queue.put_wait(wavelink.Playable(data))
            player.queue.mode = getattr(wavelink.QueueMode, state['loop'], wavelink.QueueMode.normal)
            await player.set_volume(state['volume'])
            current = state['current']
            if current and current.get('userData',{}).get('radio_track'):
                song_id = current['userData'].get('song_id')
                listeners = {member.id for target in channels if target for member in getattr(target,'members',[]) if not member.bot}
                if not await asyncio.to_thread(saved_radio_allowed,self.database,guild_id,song_id,listeners):
                    current = None
                    self.waiting[guild_id] = 'Waiting for eligible tracks'
            if current:
                track = wavelink.Playable(current)
                position = state['position'] if track.is_seekable else 0
                if track.length:
                    position = min(position, max(0,track.length-1000))
                track.extras = {**dict(track.extras), 'recovery_position': position,
                                'recovery_history_id': state.get('history_id'),
                                'recovery_heard_ms': state.get('heard_ms',0)}
                await self.play(player,track)
                await player.pause(state['paused'])
            elif not player.queue.is_empty:
                await self.play(player,player.queue.get())
            if state.get('queue_history') and player.queue.history is not None:
                player.queue.history.clear()
                await player.queue.history.put_wait([wavelink.Playable(data) for data in state['queue_history']])
            companions = [guild.get_channel(cid) for cid in state['companions']]
            if companions and all(companions):
                try:
                    await self.sync.join(guild_id,companions)
                except Exception as error:
                    self.companion_pending[guild_id] = state['companions']
                    print(f'could not restore companion channels; will retry: {error}')
            self.pending.pop(guild_id, None)
            print(f'playback restored in {guild_id}')

    async def maintain(self):
        while True:
            if self.bot.is_ready() and any(getattr(node.status,'name','') == 'CONNECTED'
                                          for node in wavelink.Pool.nodes.values() if node.identifier=='primary'):
                for gid,state in list(self.pending.items()):
                    if time.monotonic() < self.retry_at.get(gid,0) or self.attempts.get(gid,0)>=5:
                        continue
                    self.retry_at[gid] = time.monotonic()+30
                    try:
                        await self.restore(gid,state)
                    except Exception as error:
                        self.attempts[gid] = self.attempts.get(gid,0)+1
                        print(f'playback recovery failed in {gid}: {error}')
                for gid,ids in list(self.companion_pending.items()):
                    if time.monotonic() < self.companion_retry_at.get(gid,0):
                        continue
                    self.companion_retry_at[gid] = time.monotonic()+30
                    guild = self.bot.get_guild(gid)
                    if not guild or not guild.voice_client:
                        continue
                    targets = [guild.get_channel(cid) for cid in ids]
                    if not all(targets):
                        self.companion_pending.pop(gid,None)
                        continue
                    try:
                        await self.sync.join(gid,targets)
                        self.companion_pending.pop(gid,None)
                    except Exception as error:
                        print(f'companion recovery still waiting: {error}')
                for guild in self.bot.guilds:
                    try:
                        await self.checkpoint(guild)
                    except Exception as error:
                        print(f'could not checkpoint playback: {error}')
            await asyncio.sleep(5)
