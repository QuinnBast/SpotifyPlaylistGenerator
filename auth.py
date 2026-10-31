import os
import threading
import time
import logging
import http.server
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import spotipy
from spotipy.oauth2 import SpotifyOAuth


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the `code`/`error` query params Spotify redirects back with after login."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        code = query.get('code', [None])[0]
        error = query.get('error', [None])[0]

        if not code and not error:
            # Not the Spotify redirect (e.g. a browser asking for /favicon.ico) - keep waiting.
            self.send_response(404)
            self.end_headers()
            return

        self.server.oauth_code = code
        self.server.oauth_error = error

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if code:
            self.wfile.write(b"Spotify login received. You can close this window.")
        else:
            self.wfile.write(b"Spotify login failed. Check the application logs.")

    def log_message(self, format, *args):
        pass


def is_login_expired(error):
    """True for the error Spotify returns once the refresh token is no longer valid."""
    return "invalid_grant" in str(error)


class SpotifyAuth:
    """Logs in to Spotify and keeps the login alive.

    Spotify refresh tokens expire a fixed time (180 days) after the user logs in, and refreshing doesn't extend that.
    So this records when the login happened, asks for a new one before it expires, and - whenever a login is needed -
    sends a login link through the notifier and waits for Spotify's redirect on its own callback listener."""

    SCOPES = 'playlist-modify-public playlist-read-private playlist-modify-private user-library-read user-top-read'

    def __init__(self, api_config, credentials_config, login_config, notifier, extra_scopes=()):
        self.notifier = notifier
        # Permissions for optional features (e.g. listening history); a login without them asks for a new one.
        self.scope = ' '.join([self.SCOPES, *extra_scopes])

        # Credentials can be provided via config.json, but environment variables take
        # precedence so secrets don't need to be baked into the config file/image.
        self.client_id = os.environ.get('SPOTIFY_CLIENT_ID', api_config.get('clientId'))
        self.client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET', api_config.get('clientSecret'))
        self.username = os.environ.get('SPOTIFY_USERNAME', credentials_config.get('username'))
        # Must be one of the redirect URIs allowed for the app in the Spotify developer dashboard. Spotify only accepts
        # https URIs, or http on a loopback IP (e.g. http://127.0.0.1:9123). The callback listener below receives the
        # redirect on callbackPort, whatever host the URI names (e.g. a reverse proxy in front of this app).
        self.redirect_uri = os.environ.get('SPOTIFY_REDIRECT_URI', api_config.get('redirectUri', 'http://127.0.0.1:9123'))
        self.callback_port = int(os.environ.get('SPOTIFY_CALLBACK_PORT',
                                                api_config.get('callbackPort', urlparse(self.redirect_uri).port or 9123)))
        self.cache_path = os.environ.get('SPOTIFY_CACHE_PATH', api_config.get('cachePath', './cache'))
        # When the current login was made; Spotify refresh tokens expire a fixed time after login (not after last use).
        self.authorized_at_path = self.cache_path + '.authorized_at'

        # Spotify refresh tokens expire 180 days after the user logs in (since July 2026); refreshing doesn't extend it.
        self.refresh_token_lifetime_days = float(os.environ.get('SPOTIFY_REFRESH_TOKEN_LIFETIME_DAYS',
                                                                login_config.get('refreshTokenLifetimeDays', 180)))
        # Start asking for a new login this many days before the refresh token expires.
        self.warning_days_before_expiry = float(os.environ.get('SPOTIFY_REAUTH_WARNING_DAYS',
                                                               login_config.get('warningDaysBeforeExpiry', 14)))
        # While waiting for a login, repeat the notification this often.
        self.reminder_interval_hours = float(os.environ.get('SPOTIFY_LOGIN_REMINDER_HOURS',
                                                            login_config.get('reminderIntervalHours', 24)))

        self.auth_manager = SpotifyOAuth(
            scope=self.scope,
            client_id=self.client_id,
            client_secret=self.client_secret,
            username=self.username,
            redirect_uri=self.redirect_uri,
            open_browser=False,
            cache_path=self.cache_path,
        )
        self._login_thread = None
        self._login_lock = threading.Lock()

    def authenticate(self):
        """Returns a logged-in Spotify client, waiting for a login first if there is no usable token."""
        try:
            token = self.auth_manager.cache_handler.get_cached_token()
            if token is None:
                logging.info("No cached Spotify token found; a login is required.")
                return self.login_and_wait("required")
            # spotipy treats a token without every requested permission as no token at all and falls back to asking
            # on the terminal - which a container can't answer. Ask for a login the usual way instead.
            if not set(self.scope.split()) <= set(token.get('scope', '').split()):
                logging.info("The cached Spotify login lacks newly needed permissions; a login is required.")
                return self.login_and_wait("permissions")

            spotify = spotipy.Spotify(auth_manager=self.auth_manager)
            # Force the token to be fetched/refreshed now instead of on first API call.
            spotify.current_user()
            logging.info("Login successful")
            return spotify
        except Exception as e:
            if not is_login_expired(e):
                raise
            logging.error("Spotify auth failed with invalid_grant - re-authentication required.")
            spotify = self.login_and_wait("required")
            logging.info("Login successful after re-authentication.")
            return spotify

    def login_and_wait(self, reason):
        """Asks for a login (or joins one already waiting) and returns a Spotify client once it's done."""
        self._start_login(reason).join()
        spotify = spotipy.Spotify(auth_manager=self.auth_manager)
        spotify.current_user()
        return spotify

    def check_login_expiry(self):
        """Starts a background login (with a notification) once the refresh token is close to expiring. The current
        token keeps working meanwhile."""
        if self.days_until_token_expiry() <= self.warning_days_before_expiry:
            self._start_login("expiring")

    def days_until_token_expiry(self):
        try:
            with open(self.authorized_at_path) as f:
                authorized_at = datetime.fromisoformat(f.read().strip())
        except (OSError, ValueError):
            # A cache from before login times were recorded: count from now, so the reminder comes in time
            # at the latest (an earlier expiry still ends up in the "login required" path).
            self._record_login_time()
            authorized_at = datetime.now(timezone.utc)
        age_days = (datetime.now(timezone.utc) - authorized_at).total_seconds() / 86400
        return self.refresh_token_lifetime_days - age_days

    def _record_login_time(self):
        with open(self.authorized_at_path, 'w') as f:
            f.write(datetime.now(timezone.utc).isoformat())

    def _start_login(self, reason):
        """Starts a login in the background unless one is already waiting. Returns the login thread."""
        with self._login_lock:
            if self._login_thread is None or not self._login_thread.is_alive():
                self._login_thread = threading.Thread(target=self._login_worker, args=(reason,), daemon=True)
                self._login_thread.start()
            return self._login_thread

    def _login_message(self, reason):
        """Title and text of a login request. An early warning that runs out turns into a "required" one."""
        if reason == "expiring":
            days_left = self.days_until_token_expiry()
            if days_left > 0:
                when = "tomorrow" if days_left < 1.5 else f"in about {days_left:.0f} days"
                return ("Spotify login needed soon",
                        f"The playlist generator's Spotify login expires {when}. "
                        "Log in again to keep the playlists updating.")
        if reason == "permissions":
            return ("Spotify login required",
                    "A newly enabled feature (listening history) needs more Spotify permissions. Log in again to "
                    "grant them; the playlists wait until then.")
        return ("Spotify login required",
                "The playlist generator can't reach Spotify until you log in again.")

    def _login_worker(self, reason):
        """Runs the OAuth authorization-code flow with our own callback listener (bound to 0.0.0.0, since spotipy's
        built-in local server only binds to 127.0.0.1 and won't receive callbacks forwarded from outside the
        container). Keeps waiting - and repeats the notification every reminder_interval_hours - until a login succeeds."""
        server = http.server.HTTPServer(("0.0.0.0", self.callback_port), _OAuthCallbackHandler)
        server.timeout = 60
        try:
            while True:
                auth_url = self.auth_manager.get_authorize_url()
                title, message = self._login_message(reason)
                logging.info(f"{title}. Open this URL to authenticate: {auth_url}")
                # Every login request needs you to act, so it's urgent (Discord mention / ntfy priority).
                self.notifier.notify(title, message, url=auth_url, urgent=True)

                logging.info(f"Waiting for the Spotify login callback on port {self.callback_port}...")
                deadline = time.monotonic() + self.reminder_interval_hours * 3600
                while time.monotonic() < deadline:
                    server.oauth_code = None
                    server.oauth_error = None
                    server.handle_request()  # returns after one request, or after server.timeout

                    if server.oauth_error:
                        logging.error(f"Spotify login failed: {server.oauth_error}. Still waiting for a login.")
                        continue
                    if not server.oauth_code:
                        continue

                    try:
                        self.auth_manager.get_access_token(code=server.oauth_code, as_dict=False, check_cache=False)
                    except Exception as e:
                        logging.error(f"Exchanging the Spotify login code failed: {e}. Still waiting for a login.")
                        continue

                    self._record_login_time()
                    logging.info("Spotify login completed successfully.")
                    self.notifier.notify("Spotify login complete",
                                         f"Logged in. The next login is needed in about {self.refresh_token_lifetime_days:.0f} days.")
                    return
        finally:
            server.server_close()
