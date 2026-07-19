# Discord Music Bot with Web Admin

A simple Discord music bot using `discord.py`, `yt_dlp`, and `ffmpeg`, with a Flask web admin interface for managing the song database.

## Requirements

- Python 3.11+ or compatible environment
- `ffmpeg` installed
- Discord bot token

## Setup

1. Create a `token.txt` file in the project root containing your Discord bot token, or set `DISCORD_TOKEN` in the environment.
2. Install dependencies via your own environment like the nix shell.
3. Run the bot with:

    python main.py

4. Run the web admin app with:

    python webapp.py
