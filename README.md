# SpotifyPlaylistGenerator
Various Spotify playlist generators to automatically generate common playlists that are not options Spotify by default
This app generates the following playlists and keeps them up to date on a recurring schedule:

- Most recently Starred
- Your top songs (1 year)
- Your top songs (6 months)
- Your top songs (4 weeks)
- Daily Shuffle: 25 random liked songs, new every run
- Forgotten Favourites: 30 liked songs, liked more than a year ago, that aren't in any playlist you created and aren't
  among your top tracks - songs you liked once and never came back to. A new random selection each week.

Optionally, it also records every song you play into Postgres for a Grafana dashboard (see
[Listening history](#listening-history-optional)), which adds two more (off by default, `generator.allTimeTopSongs`
and `generator.allTimeTopAlbums`):

- All-Time Top 100: your most played songs ever, most played first (counting imported data exports).
- All-Time Top Albums: one song from each of your 50 most played albums ever, a different random song (of the ones
  you've played from that album) every run.

Note: Requires a spotify developer account & spotify app with a clientID and clientSecret.

# Configuration

All values in `config.json` can be overridden with environment variables, which is useful for keeping
secrets out of the config file/docker image:

| Config value             | Environment variable   |
|---------------------------|-------------------------|
| `api.clientId`             | `SPOTIFY_CLIENT_ID`      |
| `api.clientSecret`         | `SPOTIFY_CLIENT_SECRET`  |
| `credentials.username`     | `SPOTIFY_USERNAME`       |
| `api.redirectUri`          | `SPOTIFY_REDIRECT_URI`   |
| `api.cachePath`            | `SPOTIFY_CACHE_PATH`     |
| `api.callbackPort`         | `SPOTIFY_CALLBACK_PORT`  |
| `login.refreshTokenLifetimeDays` | `SPOTIFY_REFRESH_TOKEN_LIFETIME_DAYS` |
| `login.warningDaysBeforeExpiry` | `SPOTIFY_REAUTH_WARNING_DAYS` |
| `login.reminderIntervalHours` | `SPOTIFY_LOGIN_REMINDER_HOURS` |
| `notifications.discord.webhookUrl` | `DISCORD_WEBHOOK_URL` |
| `notifications.discord.mention` | `DISCORD_MENTION` |
| `notifications.ntfy.url`   | `NTFY_URL`               |
| `listeningHistory.databaseUrl` | `DATABASE_URL`       |
| `listeningHistory.pollMinutes` | `LISTENING_HISTORY_POLL_MINUTES` |
| `listeningHistory.timeZone` | `TZ`                    |
| `listeningHistory.lookupsPerPoll` | `LISTENING_HISTORY_LOOKUPS_PER_POLL` |
| `listeningHistory.grafanaPassword` | `GRAFANA_DB_PASSWORD` |

The config file is read from `config.json`, or from the path in `CONFIG_PATH`.

## Re-authentication

Since July 2026, Spotify refresh tokens expire 180 days after you log in, and refreshing does not extend that - so a
login is needed about twice a year. This script handles it like this:

- **Before it expires:** Configure `login.warningDaysBeforeExpiry` (default 14). It logs a warning and, 
  if any notification channel is enabled, the script will send a notification which includes a Spotify login link and will listening for the login,
  while it keeps working with the current token. Logging in restarts the 180 days.
- **When a login is required** (no cached token, Spotify answers `invalid_grant`, or the warning period ran out), it
  sends a "login required" notification and waits for the login. A scheduled run that hits an expired login logs in
  again and then retries.

Until you log in, the notification repeats every `login.reminderIntervalHours` (default 24), so you get one a day.
Every login notification is urgent: it mentions `DISCORD_MENTION` (if set) and uses ntfy's urgent priority. A
"Spotify login complete" message (no mention) confirms the login.

Notifications go to a Discord webhook (`DISCORD_WEBHOOK_URL`) and/or [ntfy](https://ntfy.sh) (`NTFY_URL`). The login
time is stored next to the token cache, in `<cachePath>.authorized_at`, so keep that folder on persistent storage.

**The login link:** after you log in, Spotify redirects your browser to `redirectUri`, which your app must be listening at.
You must also configure this setting in the Spotify developer dashboard. Spotify only accepts `https` URIs, or `http` on a loopback IP such as
`http://127.0.0.1:9123`. The app listens for that redirect on `callbackPort` (default: the URI's port, else 9123):

- Running the login on the same machine as your browser: `http://127.0.0.1:9123` works as-is.
- But if running on a server: put an https address in front of the callback port (e.g. a reverse proxy at
  `https://spotify.example.com/` -> port 9123) and use that as `redirectUri`, so the link works from your phone or other devices.

## Listening history (optional)

With `listeningHistory.enabled: true`, the app also records every song you play in a Postgres database, for the
Grafana dashboard in `dashboards/`: when you listen (days, hours, streaks, longest sessions), a year-by-year
"Wrapped", ranked top artists, songs and albums with covers and pictures, obsessions, forgotten favourites,
new artists you found, and devices, countries and skips (from data exports). It's off by default.

- **How:** every `pollMinutes` (default 15) it reads Spotify's "recently played": your last 50 plays of 30 seconds or
  more, with the exact time each ended. That catches every play even if the app was down for a couple of hours (it
  logs a warning if plays may have been missed). Songs only: Spotify doesn't list podcasts there.
- **Database:** `listeningHistory.databaseUrl` (or `DATABASE_URL`), e.g. `postgresql://spotify:<password>@postgres:5432/spotify`. The app creates its
  tables on the first poll. If the database is down, the playlists keep working and the next poll tries again.
- **Grafana:** set `listeningHistory.grafanaPassword` (or `GRAFANA_DB_PASSWORD`) and the app creates a read-only Postgres role `grafana` with it. Point a
  PostgreSQL datasource with uid `spotify-history` at the database as that user, and import
  `dashboards/spotify-listening.json` (`python dashboards/build.py dashboards` regenerates it after changing `build.py`).
- **Album covers and artist pictures:** after each poll the app looks up `lookupsPerPoll` (default 20) songs or
  artists it has no details for yet, most played first, one request a second. Songs the app records come with theirs;
  imported ones are looked up one at a time (Spotify removed the batch lookups in February 2026), so a big import
  fills in over days, starting with what the dashboard shows most. It's gentle on Spotify's rate limit (which the app's
  other requests share): 20 requests every 15 minutes, never retried, and if Spotify ever answers "too many
  requests" the lookups pause for as long as it asks. `0` turns them off.
- **Days and hours** on the dashboard use `listeningHistory.timeZone` (or `TZ`), e.g. `America/Regina` (default UTC).
- **New permission:** it needs `user-read-recently-played`. Turning it on makes the app ask for one new login (the
  usual notification with a login link); the playlists wait until you've logged in. Turning it off asks for nothing.

### Importing your past listening

Spotify can send you your whole history: spotify.com -> Account -> **Privacy settings** -> **Download your data**, tick
**Extended streaming history** (takes up to 30 days; "Account data" alone only has the last year, without track ids).
You get an email with a zip. `import_history.py` loads it into the app's database (from `config.json`, or `DATABASE_URL`):

```bash
python import_history.py my_spotify_data.zip   # or the unzipped folder, or JSON files
```

- Add `--dry-run` to only read the export and show what it would import.
- Only songs played for at least 30 seconds are imported (`--min-seconds` to change), to match what the app records.
  Podcasts and audiobooks are skipped.
- Plays from when the app started recording onwards are skipped (the app has those already), and plays that are
  already imported are skipped, so it's safe to run again, e.g. with a newer export.

In a container, copy the zip in and run it there, so it uses the container's config and database:

```bash
docker cp my_spotify_data.zip <container>:/tmp/ && docker exec <container> python import_history.py /tmp/my_spotify_data.zip
# Kubernetes:
kubectl cp my_spotify_data.zip <pod>:/tmp/ && kubectl exec <pod> -- python import_history.py /tmp/my_spotify_data.zip
```

# Setup

Clone the repository with `git clone https://github.com/QuinnBast/SpotifyPlaylistGenerator.git`

Setup a config file by renaming the file `config.json.example` to `config.json` and changing the values to reflect your
personal information. Once you have created your config file you can run this app one of two ways:

## As Standalone Python script

To run this as a python script one-time, simply run `python PlaylistGenerator.py`.
This will prompt you to login and automatically generate the configured playlists in your spotify account.

You can then either set this up to run on a schedule using cron-jobs or use some other scheduling service on your own.
Or, using the config, you can make the application just run forever. However I wouldn't recommend doing that as a standalone app.

## As a Docker container

I would recommend this method. After creating your configuration file, use the `Dockerfile` in this repository to build a local docker image.
You can then save the docker image, move it where you need, or upload it to some registry.
You can then use this docker image and move and run it wherever you like and it will run forever on a recurring basis.

I used the following docker compose:

```yaml
services:
  spotify-generator:
    image: spotify-playlist-generator:1.0.0
    ports:
      - "9123:9123" # Maps the Oauth callback server listener when needed.
    # Optional:
    volumes:
      - /some/local/path/config.json:/app/config.json
```

If you want, you can build the docker image without the configuration file so that your credentials are not baked into the image.
To do this, simply build the dockerfile in the repository without your `config.json` file.
Then, when you run the docker image, just mount the correct configuration file into the docker image.

This will make the container re-usable for others, but I am too lazy to build this image and publish it for people to use.

# Running the tests

The tests fake Spotify and the notification webhook (nothing touches your account) and run the real login
callback listener on a free local port. From the repo root, with the requirements installed:

```bash
python -m unittest discover -s tests
```

The listening history's database tests are skipped unless `TEST_DATABASE_URL` points at a throwaway Postgres (they
wipe its `public` schema):

```bash
docker run --rm -d -p 127.0.0.1:5439:5432 -e POSTGRES_PASSWORD=test postgres:17
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5439/postgres python -m unittest discover -s tests
```
