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
