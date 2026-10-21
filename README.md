# musicbot

music playback, personal ratings, radio, playlist queues, sync across three voice channels, and a dashboard

![dashboard](docs/dashboard.png)

## setup

python 3.11+, java 17+, Lavalink.jar, and a discord bot with message content and server members intents enabled

```bash
# nix: enter this first
nix-shell
# without nix:
python -m venv venv
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# fill in .env
```

load `.env` in each terminal. run from the project directory:

```bash
source .env
java -jar Lavalink.jar
```

```bash
source venv/bin/activate
source .env
python main.py
```

```bash
source venv/bin/activate
source .env
python webapp.py
```

local dashboard: http://127.0.0.1:5000. `token.txt` works instead of `DISCORD_TOKEN`. keep lavalink private

## commands

```text
b!help
b!add <title or link>
b!play <title, link or search>             # b!p
b!radio                                   # b!r
b!queue [page]                            # b!q
b!remove <number>
b!move <from> <to>
b!shuffle
b!clear
b!skip                                    # b!s
b!stop                                    # b!st
b!leave                                   # b!l
b!pause                                   # b!pa
b!resume                                  # b!re
b!replay
b!recover
b!seek <seconds>
b!loop [track|queue|off]                   # b!lp
b!np
b!history
b!minrating <-1|0|1|2|3>                   # b!mr
b!unrated on|off
b!embedratings on|off
b!voicestatus on|off
b!radioinfo
b!rate <-1|0|1|2|3> <title or #song_id>
b!library [page]
b!sync <channel_id> [second_channel_id]
b!unsync
```

b!play offers search results; links and playlists queue directly
 b!add saves to the library
playback buttons require a linked voice channel. the last three track messages remain available for ratings until restart

optional slash commands: set `ENABLE_SLASH_COMMANDS=true` before starting the bot. supported: `/play`, `/radio`, `/queue`, `/np`, `/history`, `/replay`, `/pause`, `/resume`, `/skip`

## radio

ratings and personal minimums: `-1, 0, 1, 2, 3`. each listener's rating must meet their own minimum
minimum `-1` allows their -1 ratings; `—` is unrated. `b!unrated` controls missing ratings. manual requests bypass rating filters

radio repeats eligible songs. “keep radio connected and retry” keeps it connected and waiting when no tracks qualify
source failures have a five-minute cooldown; `b!radio` retries immediately

for sync, invite additional bots, set `DISCORD_SYNC_TOKEN_2` and optionally `DISCORD_SYNC_TOKEN_3`, restart, then use `b!sync`
(timing is approximate)

## dashboard

- discord login; edit your own ratings and minimum.
- playback controls, connection status, history, and bot settings
- search by title, `#id`, full link, or partial link
- duplicate finder, listening preview, new-song filters, and sortable columns
- bulk import with preview: text, csv, or tsv; headers `title,url`; maximum 5000 entries / 2 mb.
- link-only entries fetch titles and covers when playback starts
- russian by default; language choice is remembered

changing a recording link clears stale metadata
video links lose attached playlists and tracking parameters but retain timecodes such as `t=1m30s`
these also work in radio
standalone playlist links remain supported

## discord login

in the [discord developer portal](https://discord.com/developers/applications), open oauth2 and configure these in `.env`:

```bash
export DISCORD_OAUTH_CLIENT_ID='application_id'
export DISCORD_OAUTH_CLIENT_SECRET='oauth_secret_not_bot_token'
export DISCORD_OAUTH_REDIRECT_URI='http://127.0.0.1:5000/auth/discord/callback'
export DISCORD_ALLOWED_USER_IDS='your_id,friend_id'
export DASHBOARD_LOGIN_DAYS='30'
export FLASK_SECRET_KEY='permanent_random_secret'
```

register the exact callback under oauth2 → redirects. for public hosting, use https and `FLASK_COOKIE_SECURE=true`

**add people:** append their user ids to `DISCORD_ALLOWED_USER_IDS`, reload `.env`, and restart the dashboard. discord developer mode enables “copy user id”

generate the session secret:

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

login uses only `identify`. remembered sessions last 30 days by default. keep the secret unchanged across restarts
without oauth configured, local password login uses `ADMIN_USERNAME` / `ADMIN_PASSWORD` (defaults: `admin` / `password`)
(it cannot edit ratings)

## temporary hosting

configure discord login and the allowlist first:

```bash
nix-shell
./share-dashboard.sh
```

add the printed callback to discord's oauth2 redirects, save, then share the https address
keep the terminal open;
ctrl+c stops sharing
every run gets a new URL and callback
the script loads `.env` and starts its own gunicorn process

for permanent hosting, use https in front of:

```bash
source .env
gunicorn --workers 1 --threads 4 --bind 127.0.0.1:5000 webapp:app
```

## sources

youtube, soundcloud, bandcamp, twitch, vimeo, and http audio
spotify/apple music require their credentials in `.env`; lavasrc resolves audio through soundcloud, then youtube
(see [lavasrc](https://github.com/topi314/LavaSrc))

```text
b!play scsearch:<query>
b!play ytsearch:<query>
b!play ytmsearch:<query>
b!play spsearch:<query>
b!play amsearch:<query>
```

youtube uses a pinned development plugin and [yt-cipher](https://github.com/kikkia/yt-cipher). override `YOUTUBE_CIPHER_URL` / `YOUTUBE_CIPHER_PASSWORD` for your own helper
no audio caching
normalization reduces loud peaks


## backups

automatic at bot startup and every six hours by default; latest 28 copies in `backups/`
change the interval in the dashboard
`.env` controls `DB_BACKUP_DIR` and `DB_BACKUP_KEEP`
backups require the bot running; keep an off-device copy

```bash
python backup_db.py
```

restore with both bot and dashboard stopped, back up the current database first:

```bash
python backup_db.py
python - <<'PY'
import sqlite3
with sqlite3.connect('file:backups/CHOSEN_SNAPSHOT.sqlite3?mode=ro', uri=True) as source:
    with sqlite3.connect('music_bot.db') as destination:
        source.backup(destination)
PY
```

## recovery and play counts

playback metadata is checkpointed every five seconds: track, position, queue, repeat mode, radio history, and linked channels. after a restart or connection loss, recent checkpoints (up to 24 hours old) resume when listeners are present. stop and leave discard recovery
automatic recovery retries up to five times; `b!recover` retries a saved checkpoint, expired or unavailable sources can still fail, live streams restart without seeking

plays count once after ten seconds of reported playback progress
paused time, failed starts, and seek jumps do not count, restored attempts keep their count identity, existing play counts are retained.

### import with ratings

upload a json file in bulk import, then review and confirm:

```json
[
  {
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "title": "optional title",
    "ratings": {"123456789012345678": 3, "234567890123456789": -1}
  }
]
```

ratings accept `-1`, `0`, `1`, `2`, `3`. discord ids must be quoted, omit title to fetch it during playback
existing recording links are skipped

normal logins can import only their own ratings
add your discord id to `DISCORD_ADMIN_USER_IDS` in `.env` to import everyone's ratings; restart the dashboard after changing it
this does not grant permission to edit other ratings in the dashboard
