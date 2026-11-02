"""Listening history: every song you play, kept in Postgres for the Grafana dashboards (dashboards/).

Spotify's "recently played" lists your last 50 plays (anything played for at least 30 seconds), with the exact time
each ended. Reading it every 15 minutes or so catches every play, even after the app was down for a couple of hours.
import_history.py fills in the past from a Spotify data export."""
import logging
import os
from datetime import datetime, timedelta
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


def store_plays(conn, plays):
    """Inserts plays that aren't stored yet; returns how many were new."""
    if not plays:
        return 0
    query = sql.SQL("INSERT INTO plays ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.SQL(', ').join(map(sql.Identifier, COLUMNS)), sql.SQL(', ').join(sql.Placeholder() * len(COLUMNS)))
    with conn.transaction(), conn.cursor() as cur:
        before = cur.execute("SELECT count(*) FROM plays").fetchone()[0]
        cur.executemany(query, [tuple(play[c] for c in COLUMNS) for play in plays])
        return cur.execute("SELECT count(*) FROM plays").fetchone()[0] - before


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
        self._prepared = False

    def record_recent_plays(self, spotify):
        """Stores plays from "recently played" that aren't stored yet. Returns how many were new."""
        items = spotify.current_user_recently_played(limit=RECENTLY_PLAYED_LIMIT)['items']
        plays = [play_from_recently_played(item) for item in items if item.get('track')]

        with connect(self.database_url) as conn:
            if not self._prepared:
                migrate(conn)
                apply_settings(conn, self.timezone, self.grafana_password)
                self._prepared = True
            previous_latest = latest_api_play(conn)
            # After an import, the first poll overlaps the end of the export.
            added = store_plays(conn, without_plays_from(conn, plays, 'export'))

        # A full page that is all new means there may have been more plays than Spotify still lists.
        if previous_latest and len(plays) == RECENTLY_PLAYED_LIMIT and min(p['played_at'] for p in plays) > previous_latest:
            logging.warning(f"Listening history: all {RECENTLY_PLAYED_LIMIT} recent plays were new - plays between "
                            f"{previous_latest.isoformat()} and {min(p['played_at'] for p in plays).isoformat()} "
                            "may be missing (poll more often if this keeps happening)")
        if added:
            logging.info(f"Listening history: {added} new play(s) recorded")
        return added
