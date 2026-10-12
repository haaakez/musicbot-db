# Music for the friend group

A Discord music bot with a shared song library, personal ratings, and a small web page for managing them. The idea is simple: join voice, start radio, and let it pick music that fits the people who are actually listening.

The bot uses Discord.py and Wavelink. Lavalink handles the audio, and both the bot and dashboard use the same SQLite database. A library of 1,400 songs and five friends does not need a separate database server.

## How radio picks music

Each person can rate a song from **0 to 3**, or **veto** it with **−1**. A missing rating is different from 0.

`b!minrating 2` sets your minimum. Radio uses the highest minimum among the current listeners, and **every existing listener rating must meet it**. For example, Bob rates a song 1, John 3, and Anna 2: it is blocked at a minimum of 2 and allowed at a minimum of 1. An average cannot override someone's low rating.

You can decide how missing ratings work:

- `b!unrated on`: allow songs some listeners have not rated. This is the default, and even a completely unrated song can play.
- `b!unrated off`: require a qualifying rating from every listener.

The setting is saved per Discord server. Any current listener's veto blocks the song regardless of the minimum or unrated setting. People outside the listening channels do not affect radio.

Among eligible songs, radio gives extra weight to songs with fewer lifetime plays and higher ratings. The average is used for that weighting, not eligibility. It avoids selecting the same library song twice during a radio session. Previously vetoed songs can get another chance when the listener who blocked them leaves.

Manual requests play before the next radio selection. They bypass radio's rating filters. Radio stops when the session has exhausted eligible songs, or after up to 20 unsuccessful searches in a selection. Restarting `b!radio` clears the session history. Bots leave after the whole listening group has been empty for three minutes.

## Getting it running

You'll need Python 3.11 or newer, Java compatible with your Lavalink release, a Lavalink v4 jar, and a Discord bot account. Download the jar from [Lavalink's releases](https://github.com/lavalink-devs/Lavalink/releases) and save it here as `Lavalink.jar`.

In the Discord Developer Portal, enable **Message Content Intent** and **Server Members Intent**. Invite the bot with permission to view its channels, send messages, embed links, connect to voice, and speak.

Install the Python dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

On Nix, `nix-shell` supplies Python, Java 17 and native dependencies. Check your chosen Lavalink release's Java requirement; use a newer JDK if needed.

Copy `.env.example` to `.env` and fill in your token and passwords. The files do not load `.env` automatically; source it in each terminal:

```bash
cp .env.example .env
# Edit .env before continuing.
source .env
java -jar Lavalink.jar
```

Start the bot in another terminal:

```bash
source venv/bin/activate
source .env
python main.py
```

`token.txt` still works for the main bot if neither `DISCORD_TOKEN` nor `DISCORD_BOT_TOKEN` is set. Use a different, long Lavalink password in `.env`; both Python and Java need the same value. Lavalink listens on localhost by default.

## Adding and playing music

```text
b!add artist - song name
b!add https://www.youtube.com/watch?v=...
b!play song name
b!radio
```

You can also put one song title per line in `songs.txt`. It is imported on startup and emptied afterward. Keep a copy of the source list if you want one.

A library entry can start with only a title. When radio resolves it, the first search result supplies its URL and title. Check ambiguous names in the dashboard to avoid the wrong recording. Manual playback does not automatically save a new song to the library; use `b!add` for that.

Playlist links queue every returned track. An ordinary text search queues its first result.

## Commands you'll actually use

| Command | What it does |
| --- | --- |
| `b!play <name or link>` / `b!p` | Play a request, or append it to the queue |
| `b!queue [page]` / `b!q` | Show the current track and upcoming tracks, ten per page |
| `b!remove <number>` | Remove an upcoming track using its queue number |
| `b!move <from> <to>` | Move an upcoming track to another position |
| `b!shuffle` | Shuffle upcoming tracks |
| `b!clear` | Clear upcoming tracks and loop history; leave the current track playing |
| `b!skip` / `b!s` | Skip the current track, including a looping track |
| `b!pause` / `b!resume` | Pause or resume playback |
| `b!seek <seconds>` | Jump to a position in a seekable song |
| `b!loop track\|queue\|off` | Repeat a track, repeat the queue, or disable looping |
| `b!np` | Show the current track |
| `b!stop` | Stop radio and playback, clear the queue and session history |
| `b!leave` | Disconnect the primary bot and its companions |
| `b!minrating <0–3>` | Set your minimum for radio |
| `b!unrated on\|off` | Allow or block missing listener ratings |
| `b!rate <−1–3> <title or #id>` | Save or replace your rating; ambiguous titles need an ID |
| `b!library [page]` | Browse the shared library in Discord |
| `b!add <title or link>` | Add a song to the shared library |
| `b!sync <channel ID> [second channel ID]` | Connect one or two companion bots |
| `b!unsync` | Disconnect companions while the primary keeps playing |
| `b!help` | Show the command list |

Playback controls require you to be in a listening channel. Rating a song later is allowed; pressing Veto only skips playback if you are listening. During sync, people in any linked channel can control the primary bot.

## Listening in up to three voice channels

Discord permits one voice connection per bot account per server. To listen in three channels, create **three distinct bot accounts** and invite all three to the server. Enable Server Members Intent on the companions too. They do not need Message Content Intent because they don't process commands.

Put the two extra tokens in `.env`:

```bash
export DISCORD_SYNC_TOKEN_2='second-bot-token'
export DISCORD_SYNC_TOKEN_3='third-bot-token'
```

Restart `main.py`; it runs the primary and both companions in one process. Start playback or radio in the primary channel, then run:

```text
b!sync 123456789012345678 234567890123456789
```

Use Discord's Developer Mode to copy channel IDs. One ID adds one companion; two IDs add two. Calling sync again replaces the existing linked channels.

The primary owns the queue. Companions follow its song, pause state, volume and playback position. Radio includes listeners across all linked channels and counts each Discord user once. Companion playback does not multiply library play counts.

Sync checks run every two seconds and correct seekable tracks when drift exceeds 750 ms. This is approximate synchronization: network delays can make channel starts differ, and live streams cannot be aligned by seeking. `b!unsync` removes the companions; `b!leave` disconnects everyone. These connections still need a live Discord/Lavalink check on your setup.

## More music platforms

YouTube, SoundCloud, Bandcamp, Twitch, Vimeo and HTTP audio URLs are configured already. Prefix a search with `scsearch:` for SoundCloud, or `ytsearch:` / `ytmsearch:` for YouTube.

The configuration also installs [LavaSrc](https://github.com/topi314/LavaSrc). Spotify and Apple Music are opt-in: set `SPOTIFY_ENABLED=true` with `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, or `APPLE_MUSIC_ENABLED=true` with `APPLE_MUSIC_TOKEN`. Export them in the Java terminal and restart Lavalink.

Once configured, paste supported links into `b!play`, including playlists, or use `spsearch:` and `amsearch:`. These platforms supply metadata; LavaSrc finds playable audio through its configured providers, here YouTube. The matched recording can differ. They don't provide direct Spotify or Apple Music audio.

Deezer and other LavaSrc sources need additional source-specific configuration and credentials; follow the plugin's documentation before enabling them. Source availability depends on upstream services and your network.

## Managing ratings in the browser

```bash
source venv/bin/activate
source .env
python webapp.py
```

Open **http://127.0.0.1:5000**. Set `ADMIN_USERNAME`, `ADMIN_PASSWORD` and a long, persistent `FLASK_SECRET_KEY` first. The fallback login is `admin` / `password` for local development only.

The grid shows 50 songs per page with a column for each known participant. Names are stored separately from ratings, so clearing someone's last rating keeps their column. The bot caches Discord usernames when it starts and when people rate songs. The dashboard also supports `DISCORD_TOKEN`, `DISCORD_BOT_TOKEN`, or `token.txt` for looking up missing names. **Refresh names** updates the cache; if Discord is unavailable, existing names are kept.

**—** means unrated; selecting it clears that rating. Changes save without reloading and show **Saved** with an **Undo** button. A conflicting edit from another tab or the bot is rejected so undo does not overwrite a different current rating. Averages, rating coverage, progress and listening previews update immediately. Apply the view again to refresh which rows are shown and their sort order.

Each person's progress is their rated songs out of the entire library, including vetoes. Click **Title**, **Plays**, **Last played**, or **Rated** to sort; click again to reverse the order. Filters and pagination retain the selected listeners.

The built-in views are:

- **Everyone likes:** every known participant rated the song 2 or 3.
- **Missing my rating:** select yourself under **Me**; the view shows songs you haven't rated. Your selection is remembered for the session.
- **Needs review:** missing ratings, ratings differing by at least 2, or songs without a resolved link.

You can also name and save the current filters, sort order, identity and listener selection. Custom saved views stay in that browser's local storage; they are not shared between friends or devices.

**Listening preview** checks the friends you select against their highest minimum. It names anyone who blocks a song because of a veto, low score, or missing rating when unrated songs are blocked. This previews rating eligibility only, not link availability or radio session history, and does not change the bot's active listeners or unrated setting.

Use the **Language / Язык** selector for English or Russian. It covers the library, login, song editor, and inline messages; song titles and Discord usernames stay as supplied. The selection is remembered in your browser session, including after logout. The displayed average excludes vetoes and is informational.

This is still a shared admin dashboard: anyone logged in can edit anyone's ratings, thresholds and songs. It does not yet have individual accounts.

For friends to access it, run Gunicorn behind an HTTPS reverse proxy, rather than exposing Flask's development server:

```bash
gunicorn --workers 1 --bind 127.0.0.1:5000 webapp:app
```

Keep the same working directory as the bot so they open the same database. Direct home hosting needs inbound access through your router, HTTPS and a reachable public address. A free dynamic DNS hostname can handle changing IPs. Keep Lavalink's port private.

Back up `music_bot.db` with SQLite's backup API while the bot is running, or stop both applications before copying it. Don't commit tokens, `.env`, the database or downloaded plugin jars.

## What I'd add next

The next useful changes would be individual dashboard accounts, a configurable five-person roster, CSV import/export with duplicate merging, and a broken-link repair screen. Artist cooldowns and recent listening history would make radio feel less repetitive across sessions. A Discord “rate my unrated songs” flow would help fill the library without everyone working through a spreadsheet.
