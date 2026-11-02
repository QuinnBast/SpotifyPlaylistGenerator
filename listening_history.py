"""Listening history: every song you play, kept in Postgres for the Grafana dashboards (dashboards/).

Spotify's "recently played" lists your last 50 plays (anything played for at least 30 seconds), with the exact time
each ended. Reading it every 15 minutes or so catches every play, even after the app was down for a couple of hours.
import_history.py fills in the past from a Spotify data export."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg import sql

MIGRATIONS = Path(__file__).parent / 'migrations'
# Postgres advisory lock id, so the app and an import starting together don't both migrate.
_MIGRATION_LOCK = 7_265_101

# The Spotify permission needed for "recently played".
SCOPES = ('user-read-recently-played',)
# "Recently played" never returns more than this.
RECENTLY_PLAYED_LIMIT = 50

# The app and a data export time the same play slightly differently (the export to the second), so where the two
# meet the same play can arrive twice under different times. A play of the same song from the other source ending
# within this window is that play.
SAME_PLAY_WINDOW = timedelta(seconds=10)

COLUMNS = ('played_at', 'track_key', 'track_id', 'track_name', 'artist_names', 'artist_ids', 'album_id', 'album_name',
           'duration_ms', 'ms_played', 'context_type', 'context_uri', 'source')
# How and where a play happened: only data exports have these (missing keys are stored as NULL).
EXPORT_DETAILS = ('platform', 'country', 'shuffle', 'skipped', 'reason_start', 'reason_end', 'offline', 'incognito')
TRACK_COLUMNS = ('track_id', 'found', 'track_name', 'duration_ms', 'artist_ids', 'artist_names', 'album_id',
                 'album_name', 'album_image')
ARTIST_COLUMNS = ('artist_id', 'found', 'name', 'image', 'genres')
# Looking up details (album covers, artist pictures): requests per poll, and the pause between them. Spotify's rate
# limit is per app over a rolling 30 seconds, so a short, spaced-out burst every poll leaves the playlists and the
# history polls untouched.
DEFAULT_LOOKUPS_PER_POLL = 20
LOOKUP_SPACING_SECONDS = 1.0


def connect(database_url):
    return psycopg.connect(database_url, autocommit=True)


def migrate(conn):
    """Applies migrations/NNN_*.sql that haven't been applied yet, in order."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob('*.sql')):
            if path.name in done:
                continue
            logging.info(f"Applying migration {path.name}")
            conn.execute(path.read_text(encoding='utf-8'))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))


def apply_settings(conn, timezone, grafana_password):
    """Stores the time zone for the dashboards and creates/updates the read-only Postgres role Grafana uses."""
    with conn.transaction():
        if not conn.execute("SELECT 1 FROM pg_timezone_names WHERE name = %s", (timezone,)).fetchone():
            raise ValueError(f"TZ: unknown time zone {timezone!r}")
        conn.execute("UPDATE settings SET timezone = %s", (timezone,))
        if grafana_password:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana'").fetchone()
            verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
            conn.execute(sql.SQL("{} ROLE grafana LOGIN PASSWORD {}").format(verb, sql.Literal(grafana_password)))
            conn.execute("GRANT USAGE ON SCHEMA public TO grafana")
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana")
            conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana")


def name_key(artist, track):
    """track_key for a play without a Spotify track id."""
    return f"{artist} / {track}".lower()


def play_from_recently_played(item):
    """One item of Spotify's "recently played" as a row for the plays table."""
    track = item['track']
    artists = track.get('artists') or []
    artist_names = [a['name'] for a in artists] or ['Unknown artist']
    album = track.get('album') or {}
    context = item.get('context') or {}
    return {
        'played_at': datetime.fromisoformat(item['played_at'].replace('Z', '+00:00')),
        # Local files have no id.
        'track_key': track.get('id') or name_key(artist_names[0], track['name']),
        'track_id': track.get('id'),
        'track_name': track['name'],
        'artist_names': artist_names,
        'artist_ids': [a.get('id') for a in artists] or None,
        'album_id': album.get('id'),
        'album_name': album.get('name'),
        'duration_ms': track.get('duration_ms'),
        'ms_played': None,
        'context_type': context.get('type'),
        'context_uri': context.get('uri'),
        'source': 'api',
    }


def _upsert(table, columns, key, update):
    """INSERT ... ON CONFLICT (key) DO UPDATE SET each of `update` to the new value."""
    return sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
        sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() * len(columns)), sql.SQL(', ').join(map(sql.Identifier, key)),
        sql.SQL(', ').join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in update))


def store_plays(conn, plays):
    """Inserts plays that aren't stored yet; returns how many were new. A play imported before these details were
    kept gets them from a new import."""
    if not plays:
        return 0
    columns = COLUMNS + EXPORT_DETAILS
    query = sql.Composed([_upsert('plays', columns, ('played_at', 'track_key'), EXPORT_DETAILS),
                          sql.SQL(" WHERE plays.platform IS NULL AND EXCLUDED.platform IS NOT NULL")])
    with conn.transaction(), conn.cursor() as cur:
        before = cur.execute("SELECT count(*) FROM plays").fetchone()[0]
        cur.executemany(query, (tuple(play.get(c) for c in columns) for play in plays))
        return cur.execute("SELECT count(*) FROM plays").fetchone()[0] - before


SHORT_PLAY_COLUMNS = ('played_at', 'track_key', 'track_id', 'track_name', 'artist_names', 'album_name',
                      'ms_played') + EXPORT_DETAILS


def store_short_plays(conn, plays):
    """Stores plays under 30 s from a data export (for the skip stats only); returns how many were new."""
    if not plays:
        return 0
    query = sql.SQL("INSERT INTO short_plays ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.SQL(', ').join(map(sql.Identifier, SHORT_PLAY_COLUMNS)),
        sql.SQL(', ').join(sql.Placeholder() * len(SHORT_PLAY_COLUMNS)))
    with conn.transaction(), conn.cursor() as cur:
        before = cur.execute("SELECT count(*) FROM short_plays").fetchone()[0]
        cur.executemany(query, (tuple(play.get(c) for c in SHORT_PLAY_COLUMNS) for play in plays))
        return cur.execute("SELECT count(*) FROM short_plays").fetchone()[0] - before


def _store(conn, table, columns, rows):
    """Inserts or replaces rows of the tracks or artists table."""
    if rows:
        query = _upsert(table, columns + ('updated_at',), columns[:1], columns[1:] + ('updated_at',))
        now = datetime.now(timezone.utc)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(query, [tuple(row.get(c) for c in columns) + (now,) for row in rows])


def store_tracks(conn, tracks):
    _store(conn, 'tracks', TRACK_COLUMNS, tracks)


def store_artists(conn, artists):
    _store(conn, 'artists', ARTIST_COLUMNS, artists)


def pick_image(images):
    """The URL of the smallest image at least 160 px wide (Spotify lists 640, 300 and 64 px), else the largest."""
    images = [i for i in images or () if i.get('url')]
    if not images:
        return None
    big_enough = [i for i in images if (i.get('width') or 0) >= 160]
    return (min(big_enough, key=lambda i: i['width']) if big_enough
            else max(images, key=lambda i: i.get('width') or 0))['url']


def track_details(track):
    """A Spotify track object (from "recently played" or GET /tracks/{id}) as a row for the tracks table."""
    album = track.get('album') or {}
    artists = [a for a in track.get('artists') or () if a.get('id')]
    return {'track_id': track['id'], 'found': True, 'track_name': track.get('name'),
            'duration_ms': track.get('duration_ms'), 'artist_ids': [a['id'] for a in artists],
            'artist_names': [a['name'] for a in artists], 'album_id': album.get('id'),
            'album_name': album.get('name'), 'album_image': pick_image(album.get('images'))}


def artist_details(artist):
    return {'artist_id': artist['id'], 'found': True, 'name': artist.get('name'),
            'image': pick_image(artist.get('images')), 'genres': artist.get('genres') or None}


# What to look up next, most played first: songs without details, and main artists without a picture (an imported
# play's artist id comes from its song's details).
PENDING_TRACKS_SQL = """
    SELECT 'track', p.track_id, count(*) FROM plays p
    WHERE p.track_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.track_id = p.track_id)
    GROUP BY p.track_id ORDER BY count(*) DESC, p.track_id LIMIT %(limit)s"""
PENDING_ARTISTS_SQL = """
    SELECT 'artist', x.artist_id, count(*) FROM (
        SELECT COALESCE(p.artist_ids[1], t.artist_ids[1]) AS artist_id FROM plays p
        LEFT JOIN tracks t ON t.track_id = p.track_id) x
    WHERE x.artist_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artists a WHERE a.artist_id = x.artist_id)
    GROUP BY x.artist_id ORDER BY count(*) DESC, x.artist_id LIMIT %(limit)s"""


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited for {retry_after} s")
        self.retry_after = retry_after


def look_up(client, kind, spotify_id):
    """GET /tracks/{id} or /artists/{id} as a row; found=False if Spotify doesn't have it (any more)."""
    try:
        if kind == 'track':
            return track_details(client.track(spotify_id))
        return artist_details(client.artist(spotify_id))
    except Exception as e:
        status = getattr(e, 'http_status', None)
        if status == 429:
            headers = getattr(e, 'headers', None) or {}
            try:
                retry_after = int(headers.get('Retry-After') or 60)
            except ValueError:
                retry_after = 60
            raise RateLimited(retry_after) from e
        if status in (400, 404):
            return {f'{kind}_id': spotify_id, 'found': False}
        raise


def without_plays_from(conn, plays, source):
    """`plays` minus those the database already has from the other `source` ('api' or 'export'): the same song (by
    track id or name) ending within SAME_PLAY_WINDOW."""
    if not plays:
        return plays
    times = [p['played_at'] for p in plays]
    stored = conn.execute(
        "SELECT played_at, track_key, lower(track_name) FROM plays WHERE source = %s AND played_at BETWEEN %s AND %s",
        (source, min(times) - SAME_PLAY_WINDOW, max(times) + SAME_PLAY_WINDOW)).fetchall()
    if not stored:
        return plays

    def is_stored(play):
        return any(abs(at - play['played_at']) <= SAME_PLAY_WINDOW
                   and (key == play['track_key'] or name == play['track_name'].lower())
                   for at, key, name in stored)
    return [p for p in plays if not is_stored(p)]


def first_api_play(conn):
    """When the app's own recording starts (None before its first poll). Imports stop there."""
    return conn.execute("SELECT min(played_at) FROM plays WHERE source = 'api'").fetchone()[0]


def latest_api_play(conn):
    return conn.execute("SELECT max(played_at) FROM plays WHERE source = 'api'").fetchone()[0]


# Songs as the dashboards count them: by main artist and name, since Spotify gives the same song several ids (single,
# album, re-release) and the basic data export has none. Each song's id for playlists is the one played most recently.
_SONGS = """
    SELECT lower(artist_names[1]) AS artist, lower(album_name) AS album, lower(track_name) AS song,
           count(*) AS plays, max(played_at) AS last_played,
           (array_agg(track_id ORDER BY played_at DESC) FILTER (WHERE track_id IS NOT NULL))[1] AS track_id
    FROM plays"""
TOP_SONGS_SQL = f"""
    SELECT (array_agg(track_id ORDER BY plays DESC))[1]   -- one song on several albums: the most played version
    FROM ({_SONGS} GROUP BY 1, 2, 3) s
    WHERE track_id IS NOT NULL
    GROUP BY artist, song ORDER BY sum(plays) DESC, max(last_played) DESC LIMIT %s"""
TOP_ALBUMS_SQL = f"""
    SELECT array_agg(track_id ORDER BY song) FILTER (WHERE track_id IS NOT NULL)
    FROM ({_SONGS} WHERE album_name IS NOT NULL GROUP BY 1, 2, 3) s
    GROUP BY artist, album HAVING count(track_id) > 0
    ORDER BY sum(plays) DESC, max(last_played) DESC LIMIT %s"""


class ListeningHistory:
    """The `listeningHistory` section of config.json: polls "recently played" into Postgres.

    The database is set up on the first poll rather than at startup, so the playlists keep working while it's down;
    a failed poll is logged and the next one tries again."""

    def __init__(self, history_config):
        self.poll_minutes = float(os.environ.get('LISTENING_HISTORY_POLL_MINUTES', history_config.get('pollMinutes', 15)))
        # DATABASE_URL overrides the config (e.g. to keep the password in a Kubernetes secret).
        self.database_url = os.environ.get('DATABASE_URL', history_config.get('databaseUrl'))
        if not self.database_url:
            raise ValueError("listeningHistory is enabled but no database is set: set listeningHistory.databaseUrl "
                             "in config.json or DATABASE_URL (e.g. postgresql://user:password@host:5432/dbname)")
        # Optional: a read-only Postgres role "grafana" with this password, for the dashboards.
        self.grafana_password = os.environ.get('GRAFANA_DB_PASSWORD', history_config.get('grafanaPassword')) or None
        # Days and hours on the dashboards are in this time zone.
        self.timezone = os.environ.get('TZ') or history_config.get('timeZone', 'UTC')
        # Album covers and artist pictures for the dashboards: this many lookups after each poll (0: none).
        self.lookups_per_poll = int(os.environ.get('LISTENING_HISTORY_LOOKUPS_PER_POLL',
                                                   history_config.get('lookupsPerPoll', DEFAULT_LOOKUPS_PER_POLL)))
        self._lookups_paused_until = 0.0
        self._prepared = False

    def _connect(self):
        """A connection to the database, set up (migrations, settings) on first use."""
        conn = connect(self.database_url)
        if not self._prepared:
            try:
                migrate(conn)
                apply_settings(conn, self.timezone, self.grafana_password)
            except Exception:
                conn.close()
                raise
            self._prepared = True
        return conn

    def top_songs(self, count):
        """Spotify track ids of your `count` most played songs ever, most played first."""
        with self._connect() as conn:
            return [row[0] for row in conn.execute(TOP_SONGS_SQL, (count,))]

    def top_albums(self, count):
        """Your `count` most played albums ever, most played first: for each, the track ids of the songs you've
        played from it."""
        with self._connect() as conn:
            return [row[0] for row in conn.execute(TOP_ALBUMS_SQL, (count,))]

    def look_up_details(self, client, sleep=time.sleep):
        """Looks up the details (album cover, artist picture, ...) of up to lookups_per_poll songs and artists, most
        played first, one request at a time with a pause in between. `client` must not retry or wait on its own
        (spotipy with a plain requests.Session): when Spotify says to slow down (429), the lookups stop and wait for
        as long as it asks, so they never hold up the playlists or the history polls. Returns how many were looked up.
        """
        if self.lookups_per_poll <= 0 or time.monotonic() < self._lookups_paused_until:
            return 0
        done = 0
        with self._connect() as conn:
            limit = {'limit': self.lookups_per_poll}
            pending = (conn.execute(PENDING_TRACKS_SQL, limit).fetchall()
                       + conn.execute(PENDING_ARTISTS_SQL, limit).fetchall())
            pending = sorted(pending, key=lambda row: -row[2])[:self.lookups_per_poll]
            for i, (kind, spotify_id, _) in enumerate(pending):
                if i:
                    sleep(LOOKUP_SPACING_SECONDS)
                try:
                    row = look_up(client, kind, spotify_id)
                except RateLimited as e:
                    self._lookups_paused_until = time.monotonic() + e.retry_after
                    logging.warning(f"Listening history: Spotify asked to slow down - pausing the album cover and "
                                    f"artist picture lookups for {e.retry_after} s")
                    break
                (store_tracks if kind == 'track' else store_artists)(conn, [row])
                done += 1
        if done:
            logging.info(f"Listening history: looked up the details of {done} song(s)/artist(s)")
        return done

    def record_recent_plays(self, spotify):
        """Stores plays from "recently played" that aren't stored yet. Returns how many were new."""
        items = spotify.current_user_recently_played(limit=RECENTLY_PLAYED_LIMIT)['items']
        plays = [play_from_recently_played(item) for item in items if item.get('track')]

        with self._connect() as conn:
            previous_latest = latest_api_play(conn)
            # After an import, the first poll overlaps the end of the export.
            added = store_plays(conn, without_plays_from(conn, plays, 'export'))
            # The songs' details come with them: nothing to look up for what the app records.
            tracks = {item['track']['id']: track_details(item['track'])
                      for item in items if item.get('track') and item['track'].get('id')}
            store_tracks(conn, list(tracks.values()))

        # A full page that is all new means there may have been more plays than Spotify still lists.
        if previous_latest and len(plays) == RECENTLY_PLAYED_LIMIT and min(p['played_at'] for p in plays) > previous_latest:
            logging.warning(f"Listening history: all {RECENTLY_PLAYED_LIMIT} recent plays were new - plays between "
                            f"{previous_latest.isoformat()} and {min(p['played_at'] for p in plays).isoformat()} "
                            "may be missing (poll more often if this keeps happening)")
        if added:
            logging.info(f"Listening history: {added} new play(s) recorded")
        return added
