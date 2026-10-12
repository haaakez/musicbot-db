# musicbot

a discord music bot with a shared library, personal ratings, radio, playlist queues, playback across up to three voice channels, and a dashboard for managing songs and ratings.

## run

needs python 3.11+, node 22+, deno, git, lavalink v4 saved as `Lavalink.jar`, its required java version, and a discord bot with message content and server members intents enabled.

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
- custom filters saved in the browser; cached usernames with manual refresh.

**—** clears a rating. apply the view again to refresh filtering after edits. the preview does not change the bot's settings. anyone logged in can edit all ratings.

for remote access, use https and a production server:

```bash
gunicorn --workers 1 --bind 127.0.0.1:5000 webapp:app
```

run everything from the same directory. keep lavalink private. back up `music_bot.db` with sqlite's backup api, or stop both apps before copying it.

## youtube audio

youtube audio is downloaded on demand with yt-dlp and a local playback-token helper, then played through lavalink's local source. the original titles, links, ratings, and channel sync are retained. no browser cookies or account are needed for this setup.

the first bot start installs the pinned helper under `.audio-cache/provider`. later starts reuse it. audio is cached under `.audio-cache/tracks`, with a 1 gb limit by default. old files are removed as needed, while currently playing files are kept.

```bash
# after updating the code
nix-shell
pip install -r requirements.txt
# restart lavalink so sources.local is enabled
java -jar Lavalink.jar
```

set `YOUTUBE_CACHE_MB` to change the cache limit, or `YOUTUBE_CACHE_ENABLED=false` to use lavalink's youtube streaming again. source availability can still change; `b!radioinfo` and the terminals show playback failures separately from rating filters.
