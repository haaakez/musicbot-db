# Discord Music Bot & Web Dashboard

A Wavelink-powered Discord music bot featuring an endless smart radio, personalized rating thresholds, and a Flask web interface for database management.

## Features
- **Lavalink Engine:** Low-latency playback with LavaDSPX volume normalization.
- **Smart Radio:** Endless queue that mathematically prioritizes unplayed and highly-rated tracks.
- **Personal Limits:** Users set minimum ratings; the bot strictly enforces the highest limit in the voice channel.
- **Web Dashboard:** A Flask interface (`localhost:5000`) to manage songs, ratings, and thresholds.

## Requirements
- Python 3.11+
- Java 17+ (for Lavalink)
- `Lavalink.jar` (v4+) configured with YouTube and LavaDSPX plugins.
- Discord Bot Token (with **Server Members Intent** enabled).

## Quick Start

1. **Tokens:** Create a `token.txt` file with your Discord bot token. 
   *(Note: Set `DISCORD_BOT_TOKEN` in your environment so the web app can fetch usernames).*
2. **Start Lavalink:**
   `java -jar Lavalink.jar`
3. **Start the Bot:**
   `python main.py`
4. **Start the Dashboard:**
   `python webapp.py`