# musicbot

a discord music bot with a shared library, personal ratings, radio, playlist queues, playback across up to three voice channels, and a dashboard for managing songs and ratings.

## dashboard

manage songs and ratings, check listening eligibility, view playback status, and configure radio and backups. open “about” for the command reference, also available through `b!help`.

![dashboard](docs/dashboard.png)

## run

needs python 3.11+, lavalink v4 saved as `Lavalink.jar`, its required java version, and a discord bot with message content and server members intents enabled.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# add your tokens and passwords to .env
```

run these in separate terminals:

```bash
# lavalink
source .env
java -jar Lavalink.jar
```

```bash
# bot
source venv/bin/activate
source .env
python main.py
```

```bash
# dashboard
source venv/bin/activate
source .env
python webapp.py
```

open http://127.0.0.1:5000. set `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `FLASK_SECRET_KEY` in `.env`. the local fallback is `admin` / `password`. `token.txt` works when token environment variables are unset. on nix, use `nix-shell` first.

## commands

```text
b!help
b!add <title or link>
b!play <title, link or search>       # b!p
b!radio                             # b!r
b!queue [page]                      # b!q
b!remove <queue number>
b!move <from> <to>
b!shuffle
b!clear
b!skip                              # b!s
b!stop                              # b!st
b!leave                             # b!l
b!pause                             # b!pa
b!resume                            # b!re
b!seek <seconds>
b!loop [track|queue|off]             # b!lp
b!np
b!minrating <0–3>                    # b!mr
b!unrated on|off
b!embedratings on|off
b!voicestatus on|off
b!radioinfo
b!rate <-1–3> <title or #song_id>
b!library [page]
b!sync <channel_id> [second_channel_id]
b!unsync
```

playlist links queue all returned tracks. text searches use the first result. `b!add` saves tracks to the library; `b!play` alone does not. `songs.txt` imports one title per line at startup, then empties the file.

## radio

ratings: **-1, 0, 1, 2, 3**. missing ratings are separate from 0.

radio uses the highest minimum among listeners. every existing rating must meet it. ratings of 1, 3, and 2 pass at a minimum of 1, but fail at 2. **-1 blocks a song while that person is listening.**

`b!unrated on` allows missing ratings; `off` requires everyone to rate the song. manual requests bypass these rules. radio favours less-played songs and cycles through the eligible library. when every matching song has played, it starts another cycle. a single eligible song repeats. unavailable tracks are skipped for five minutes; `b!radio` retries them immediately.

## sync

invite two extra bot accounts with server members intent enabled. add their tokens to `.env` and restart:

```bash
export DISCORD_SYNC_TOKEN_2='second-bot-token'
export DISCORD_SYNC_TOKEN_3='third-bot-token'
```

start playback, then use `b!sync` with one or two channel ids. companions follow the main bot's queue and playback. radio checks everyone across the linked channels. timing is approximate; live streams cannot be aligned by seeking.

## platforms

youtube, soundcloud, bandcamp, twitch, vimeo, and http audio links are configured. lavasrc adds optional spotify and apple music links, using youtube for the actual audio.

set `SPOTIFY_ENABLED=true` with `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, or `APPLE_MUSIC_ENABLED=true` with `APPLE_MUSIC_TOKEN`. export them before starting java and restart lavalink. other sources need their own configuration: [lavasrc docs](https://github.com/topi314/LavaSrc).

```text
b!play ytsearch:<query>
b!play ytmsearch:<query>
b!play scsearch:<query>
b!play spsearch:<query>
b!play amsearch:<query>
```

## dashboard

“bot settings” controls ratings in the now-playing embed, the track in voice-channel status, and whether radio allows unrated songs. settings are saved per server and match the commands above. display updates apply within five seconds. voice status needs the bot’s “set voice channel status” permission.

- english and russian, inline saves with undo, and per-person progress.
- sorting by title, plays, last played, and rating coverage.
- listening preview showing who blocks a track and why.
- “everyone likes”: everyone rated 2 or 3.
- “missing my rating”: tracks the selected person has not rated.
- “needs review”: missing ratings, disagreements, or unresolved links.
- cached usernames with manual refresh.

**—** clears a rating. apply the view again to refresh filtering after edits. the preview does not change the bot's settings. anyone logged in can edit all ratings.

for remote access, use https and a production server:

```bash
gunicorn --workers 1 --bind 127.0.0.1:5000 webapp:app
```

run everything from the same directory. keep lavalink private. back up `music_bot.db` with sqlite's backup api, or stop both apps before copying it.

## audio sources

song searches try a matching soundcloud recording first, then youtube. matching checks the title but can still select a different upload. explicit links, including saved youtube links, always use the supplied platform and recording. search prefixes also select a platform directly.

```text
b!play scsearch:artist title
b!play ytsearch:artist title
b!play ytmsearch:artist title
b!play spsearch:artist title
b!play amsearch:artist title
b!play <soundcloud, bandcamp, spotify or apple music link>
```

soundcloud and bandcamp provide audio directly. spotify and apple music require their credentials in `.env`; lavasrc resolves their metadata through soundcloud, then youtube. youtube tries the patched ios client before mweb and tvhtml5_simply. ratings stay attached to the library song when another source is selected.

youtube signature decoding uses the public yt-cipher service at `https://cipher.kikkia.dev/`. this is an external dependency; audio still streams through lavalink without downloading songs. set `YOUTUBE_CIPHER_URL` and, if needed, `YOUTUBE_CIPHER_PASSWORD` to use your own [yt-cipher instance](https://github.com/kikkia/yt-cipher). restart lavalink after changing these settings. the helper addresses player-script decoding failures; youtube login requirements can still prevent playback.

the youtube plugin is pinned to development build `6579cdfd8fbfffad7c95b72fc07ff81c11d9e4de` for the upstream ios playback fix, with ios tried before mweb. this is newer development code than stable 1.18.2; playback may still fail as youtube changes. to revert, restore dependency version `1.18.2` and the releases repository in `application.yml`, remove the development youtube jar from `plugins`, and restart lavalink.

all playback uses lavadspx adaptive peak attenuation, including radio and synced channels. it reduces peaks above amplitude `0.4` without boosting quiet recordings. this is not measured lufs normalization; songs can still differ in perceived loudness.

## database backups

the bot creates a consistent sqlite snapshot at startup and every six hours, keeping the latest 28 snapshots in `backups/`. snapshots include songs, ratings, usernames, thresholds, and server settings. they are checked before publication; backup failures are logged without stopping playback.

```bash
# create a backup now, including while the bot is running
python backup_db.py
# choose another destination or retention count
python backup_db.py --directory /path/to/backups --keep 28
```

set the backup interval (1–168 hours) under “automatic backups” in the dashboard. changes apply within 30 seconds. `DB_BACKUP_HOURS` supplies the initial default; saved dashboard settings take precedence. `DB_BACKUP_DIR` and `DB_BACKUP_KEEP` in `.env` control destination and retention. automatic backups run while the bot is running. keep a copy on another disk or machine to protect against disk failure.

```bash
# restore: stop the bot and dashboard first, then preserve the current database
python backup_db.py
# replace the example snapshot path with the backup to restore
python - <<'PY'
import sqlite3
from contextlib import closing
with closing(sqlite3.connect('file:backups/music_bot-YYYYMMDDTHHMMSSffffffZ.sqlite3?mode=ro', uri=True)) as source:
    with closing(sqlite3.connect('music_bot.db')) as destination:
        source.backup(destination)
PY
# restart the bot and dashboard
```

now-playing messages show source, artwork when available, duration, play count, and requester details for manual requests. `b!embedratings on|off` or the dashboard setting controls the ratings section. the bot retains its latest three track messages per server during a run so earlier tracks can still be rated; -1 on an older track does not skip a different current song. messages from before a bot restart are not registered again.

in the dashboard, a song can have only a link and no title. its title and artwork are filled from source metadata when playback starts. supplied titles are preserved. cover thumbnails appear beside song ids after artwork has been resolved.

## dashboard status and radio

“keep radio connected and retry” in bot settings keeps active radio connected when the channel is empty or no eligible songs remain. eligible songs already repeat after each cycle. while waiting, radio rechecks every 15 seconds and resumes when listeners return or ratings allow playback. source failures still keep their five-minute cooldown. explicit stop and leave commands remain available; this does not reconnect a bot that was forcibly disconnected.

the dashboard shows discord, lavalink, and voice connection status, the current track, pause state, source, and listener count. status refreshes about every five seconds; a heartbeat older than 20 seconds is marked outdated.

“similar links” finds entries pointing to the same recording, including youtube short/watch/embed links and common tracking variations. it does not compare audio across different uploads. “new songs” shows additions from the last seven days; date-added sorting is also available. timestamps are recorded for new entries; existing entries with unknown dates are excluded from the new-songs view.

search accepts titles, exact ids such as `#123`, full links, and partial links. full youtube links also match alternate url formats for the same video. “duplicate finder” lists possible duplicates by normalized link or similar title and shows the matching ids. review recordings before deleting: similar titles can belong to different versions, and ratings are not merged automatically.
