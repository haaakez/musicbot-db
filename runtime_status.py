"""Publish a heartbeat for dashboard status without sharing Discord clients."""
import asyncio
import json
import sqlite3
import time


def publish_status(database, guild_id, payload):
    with sqlite3.connect(database, timeout=30) as conn:
        conn.execute('''INSERT INTO bot_runtime(guild_id, updated_at, payload) VALUES(?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload''',
                     (guild_id, time.time(), json.dumps(payload)))


async def maintain_status(bot, sync, database, radio_active, radio_waiting, retry):
    retry_at = {}
    while True:
        for guild in bot.guilds:
            try:
                player = guild.voice_client
                connected = bool(player and player.connected)
                track = player.current if connected else None
                node_connected = bool(player and getattr(player.node.status, 'name', '') == 'CONNECTED')
                if not player:
                    import wavelink
                    node_connected = any(getattr(node.status, 'name', '') == 'CONNECTED' for node in wavelink.Pool.nodes.values())
                await asyncio.to_thread(publish_status, database, guild.id, {
                    'discord': bot.is_ready(), 'lavalink': node_connected, 'voice': connected,
                    'channel': player.channel.name if connected and player.channel else None,
                    'encoded': track.encoded if track else None, 'title': track.title if track else None, 'url': track.uri if track else None,
                    'source': track.source if track else None, 'paused': bool(player and player.paused),
                    'radio': bool(radio_active.get(guild.id)), 'waiting': radio_waiting.get(guild.id) if radio_active.get(guild.id) else None,
                    'listeners': len(sync.listeners(guild.id)) if connected else 0,
                    'synced': sum(p.connected for _, p in sync.groups.get(guild.id, [])),
                })
                if (connected and node_connected and radio_active.get(guild.id) and radio_waiting.get(guild.id)
                        and not player.playing and not track and time.monotonic() >= retry_at.get(guild.id, 0)):
                    retry_at[guild.id] = time.monotonic() + 15
                    await retry(guild, player.channel, None)
            except Exception as error:
                print(f'could not update dashboard status for {guild.id}: {error}')
        await asyncio.sleep(5)
