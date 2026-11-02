"""Listening history and the data-export import. Parsing tests always run; the database tests need a throwaway
Postgres in TEST_DATABASE_URL (they wipe its public schema), e.g.
    docker run --rm -d -p 127.0.0.1:5439:5432 -e POSTGRES_PASSWORD=test postgres:17
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5439/postgres python -m unittest discover -s tests"""
import io
import json
import logging
import os
import tempfile
import unittest
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest import mock

import import_history
import listening_history as history

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def recently_played_item(i, minutes_after_t0, track_id=None):
    return {
        'played_at': (T0 + timedelta(minutes=minutes_after_t0)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        'track': {'id': track_id or f't{i}', 'name': f'Song {i}', 'duration_ms': 200_000,
                  'artists': [{'id': 'a1', 'name': 'Main Artist'}, {'id': 'a2', 'name': 'Featured'}],
                  'album': {'id': 'al1', 'name': 'Album'}},
        'context': {'type': 'playlist', 'uri': 'spotify:playlist:p1'},
    }


def extended_row(ts, ms_played, uri='spotify:track:abc', name='Song', artist='Artist'):
    return {'ts': ts, 'ms_played': ms_played, 'spotify_track_uri': uri, 'master_metadata_track_name': name,
            'master_metadata_album_artist_name': artist, 'master_metadata_album_album_name': 'Album',
            'episode_name': None, 'spotify_episode_uri': None}


EPISODE = {'ts': '2025-01-01T10:00:00Z', 'ms_played': 900_000, 'spotify_track_uri': None,
           'master_metadata_track_name': None, 'master_metadata_album_artist_name': None,
           'episode_name': 'A podcast', 'spotify_episode_uri': 'spotify:episode:e1'}


class SpotifyError(Exception):
    def __init__(self, http_status, headers=None):
        super().__init__(f"HTTP {http_status}")
        self.http_status, self.headers = http_status, headers


class FakeLookupClient:
    """GET /tracks/{id} and /artists/{id}: track 'gone' is no longer on Spotify; after `rate_limit_after` requests,
    every request gets 429."""
    def __init__(self, rate_limit_after=None):
        self.requests, self.rate_limit_after = [], rate_limit_after

    def _request(self, what):
        if self.rate_limit_after is not None and len(self.requests) >= self.rate_limit_after:
            raise SpotifyError(429, {'Retry-After': '120'})
        self.requests.append(what)

    def track(self, track_id):
        self._request(('track', track_id))
        if track_id == 'gone':
            raise SpotifyError(404)
        return {'id': track_id, 'name': f'Song {track_id}', 'duration_ms': 180_000,
                'artists': [{'id': f'artist_of_{track_id}', 'name': 'Artist'}],
                'album': {'id': 'al', 'name': 'Album', 'images': [
                    {'url': 'big', 'width': 640}, {'url': 'medium', 'width': 300}, {'url': 'small', 'width': 64}]}}

    def artist(self, artist_id):
        self._request(('artist', artist_id))
        return {'id': artist_id, 'name': 'Artist', 'genres': ['indie pop'],
                'images': [{'url': f'picture_of_{artist_id}', 'width': 320}]}


class FakeSpotify:
    def __init__(self, items):
        self.items = items

    def current_user_recently_played(self, limit):
        return {'items': self.items[:limit]}


class ParsingTest(unittest.TestCase):
    def test_recently_played_item(self):
        play = history.play_from_recently_played(recently_played_item(1, 0))
        self.assertEqual(play['played_at'], T0)
        self.assertEqual((play['track_key'], play['artist_names'], play['album_name']),
                         ('t1', ['Main Artist', 'Featured'], 'Album'))
        self.assertEqual((play['context_type'], play['source'], play['ms_played']), ('playlist', 'api', None))

    def test_local_file_has_no_id(self):
        item = recently_played_item(1, 0)
        item['track']['id'] = None
        self.assertEqual(history.play_from_recently_played(item)['track_key'], 'main artist / song 1')

    def _zip(self, files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, rows in files.items():
                archive.writestr(name, json.dumps(rows))
        path = os.path.join(tempfile.mkdtemp(), 'my_spotify_data.zip')
        with open(path, 'wb') as f:
            f.write(buffer.getvalue())
        return path

    def test_extended_history_in_a_zip(self):
        path = self._zip({
            'Spotify Extended Streaming History/Streaming_History_Audio_2024-2025_0.json': [
                extended_row('2025-01-01T10:00:00Z', 180_000),
                extended_row('2025-01-01T10:03:00Z', 5_000),   # skipped after 5 s
                EPISODE],
            'Spotify Extended Streaming History/Streaming_History_Video_2024-2025.json': [EPISODE],
            'Spotify Account Data/StreamingHistory_music_0.json': [
                {'endTime': '2025-01-01 10:00', 'artistName': 'Artist', 'trackName': 'Song', 'msPlayed': 180_000}],
        })
        counts = Counter()
        plays = import_history.parse_export([path], 30, counts)

        self.assertEqual(len(plays), 1, 'extended history wins over the basic one; short plays and podcasts skipped')
        self.assertEqual((plays[0]['track_key'], plays[0]['artist_names'], plays[0]['ms_played']),
                         ('abc', ['Artist'], 180_000))
        self.assertEqual(plays[0]['played_at'], datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(counts['played less than 30 s'], 1)
        self.assertEqual(counts['not a song (podcast, audiobook, removed track)'], 1)

    def test_basic_history_when_there_is_no_extended_one(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'StreamingHistory_music_0.json'), 'w', encoding='utf-8') as f:
            json.dump([{'endTime': '2025-03-04 21:15', 'artistName': 'Artist', 'trackName': 'Song',
                        'msPlayed': 200_000}], f)
        plays = import_history.parse_export([folder], 30, Counter())
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]['track_key'], 'artist / song')
        self.assertEqual(plays[0]['played_at'], datetime(2025, 3, 4, 21, 15, tzinfo=timezone.utc))

    def test_history_needs_a_database(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                history.ListeningHistory({'enabled': True})


@unittest.skipUnless(TEST_DATABASE_URL, 'set TEST_DATABASE_URL to run the database tests')
class DatabaseTest(unittest.TestCase):
    def setUp(self):
        with history.connect(TEST_DATABASE_URL) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana'").fetchone():
                conn.execute("DROP OWNED BY grafana")
                conn.execute("DROP ROLE grafana")
        env = {'DATABASE_URL': TEST_DATABASE_URL, 'TZ': 'America/Regina', 'GRAFANA_DB_PASSWORD': 'grafana-pw'}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.history = history.ListeningHistory({'enabled': True})

    def query(self, sql, params=(), url=TEST_DATABASE_URL):
        with history.connect(url) as conn:
            return conn.execute(sql, params).fetchall()

    def test_polls_store_each_play_once(self):
        items = [recently_played_item(i, -i * 4) for i in range(10)]
        self.assertEqual(self.history.record_recent_plays(FakeSpotify(items)), 10)
        self.assertEqual(self.history.record_recent_plays(FakeSpotify(items)), 0, 'the next poll adds nothing')
        newer = [recently_played_item(99, 10)] + items
        self.assertEqual(self.history.record_recent_plays(FakeSpotify(newer)), 1)

    def test_dashboard_view_and_grafana_role(self):
        self.history.record_recent_plays(FakeSpotify([recently_played_item(1, 0)]))
        artist, local_time, seconds = self.query("SELECT artist, local_time, seconds FROM v_plays")[0]
        self.assertEqual(artist, 'Main Artist')
        self.assertEqual(local_time, datetime(2026, 10, 1, 6, 0), 'Regina is UTC-6 all year')
        self.assertEqual(seconds, 200)

        grafana_url = TEST_DATABASE_URL.replace('postgres:test@', 'grafana:grafana-pw@')
        self.assertEqual(self.query("SELECT count(*) FROM v_plays", url=grafana_url)[0][0], 1)
        with self.assertRaises(Exception, msg='grafana is read-only'):
            self.query("DELETE FROM plays", url=grafana_url)

    def test_warns_when_plays_may_have_been_missed(self):
        self.history.record_recent_plays(FakeSpotify([recently_played_item(0, 0)]))
        later = [recently_played_item(i, 600 + i * 4) for i in range(1, 51)]
        with self.assertLogs(level='WARNING') as logs:
            logging.disable(logging.NOTSET)
            try:
                self.history.record_recent_plays(FakeSpotify(later))
            finally:
                logging.disable(logging.CRITICAL)
        self.assertIn('may be missing', logs.output[0])

    def test_import_stops_where_the_app_started_and_can_run_again(self):
        self.history.record_recent_plays(FakeSpotify([recently_played_item(1, 0)]))   # recording starts at T0
        folder = tempfile.mkdtemp()
        rows = [extended_row((T0 - timedelta(days=d)).strftime('%Y-%m-%dT%H:%M:%SZ'), 120_000, f'spotify:track:x{d}')
                for d in (300, 200, 100)]
        rows.append(extended_row((T0 + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'), 120_000))   # app has it
        with open(os.path.join(folder, 'Streaming_History_Audio_2025-2026_0.json'), 'w', encoding='utf-8') as f:
            json.dump(rows, f)

        self.assertEqual(import_history.main([folder]), 0)
        self.assertEqual(self.query("SELECT source, count(*) FROM plays GROUP BY source ORDER BY source"),
                         [('api', 1), ('export', 3)])
        self.assertEqual(import_history.main([folder]), 0)
        self.assertEqual(self.query("SELECT count(*) FROM plays")[0][0], 4, 'running it again adds nothing')

    def test_all_time_top_songs_and_albums(self):
        def play(minutes, track_id, name, album, artist='Artist'):
            return {'played_at': T0 + timedelta(minutes=minutes), 'track_key': track_id or f'{artist} / {name}',
                    'track_id': track_id, 'track_name': name, 'artist_names': [artist], 'artist_ids': None,
                    'album_id': None, 'album_name': album, 'duration_ms': None, 'ms_played': 60_000,
                    'context_type': None, 'context_uri': None, 'source': 'export'}
        plays = ([play(m, 'hit_single', 'Hit', 'Hit - Single') for m in range(3)]       # one song, two ids/albums:
                 + [play(10 + m, 'hit_album', 'Hit', 'Big Album') for m in range(4)]   # 7 plays, the album's id
                 + [play(20 + m, 'deep_cut', 'Deep Cut', 'Big Album') for m in range(2)]
                 + [play(30 + m, 'other', 'Other', 'Other Album', 'Someone') for m in range(5)]
                 + [play(40 + m, None, 'No Id', 'Other Album', 'Someone') for m in range(9)])   # basic export
        with history.connect(TEST_DATABASE_URL) as conn:
            history.migrate(conn)
            history.store_plays(conn, plays)

        self.assertEqual(self.history.top_songs(10), ['hit_album', 'other', 'deep_cut'],
                         'songs counted across albums, without an id left out')
        self.assertEqual(self.history.top_songs(1), ['hit_album'])
        self.assertEqual(self.history.top_albums(10), [['other'], ['deep_cut', 'hit_album'], ['hit_single']],
                         'albums by plays (counting songs without an id), with the songs played from each')

    def test_export_details_kept_and_filled_in_by_a_new_import(self):
        row = extended_row('2025-06-01T10:00:00Z', 120_000)
        row.update({'platform': 'iOS 17.1 (iPhone15,2)', 'conn_country': 'CA', 'shuffle': True, 'skipped': False,
                    'reason_start': 'clickrow', 'reason_end': 'trackdone', 'offline': False, 'incognito_mode': False})
        plain = {k: v for k, v in row.items() if k not in ('platform', 'conn_country')}
        self.assertEqual(import_history.main([self._write_export([plain])]), 0)   # an older import, without them
        self.assertEqual(self.query("SELECT device, country FROM v_plays"), [(None, None)])
        self.assertEqual(import_history.main([self._write_export([row])]), 0)
        self.assertEqual(self.query("SELECT count(*), max(device), max(country), bool_and(shuffle) FROM v_plays"),
                         [(1, 'iPhone / iPad', 'CA', True)])

    def test_details_looked_up_most_played_first_without_retrying(self):
        def play(minutes, track_id):
            return {'played_at': T0 + timedelta(minutes=minutes), 'track_key': track_id, 'track_id': track_id,
                    'track_name': track_id, 'artist_names': ['Artist'], 'artist_ids': None, 'album_id': None,
                    'album_name': 'Album', 'duration_ms': None, 'ms_played': 60_000, 'context_type': None,
                    'context_uri': None, 'source': 'export'}
        with history.connect(TEST_DATABASE_URL) as conn:
            history.migrate(conn)
            history.store_plays(conn, [play(m, 'top') for m in range(5)] + [play(10 + m, 'gone') for m in range(3)]
                                + [play(20, 'rare')])
        self.history.lookups_per_poll = 3
        pauses = []
        client = FakeLookupClient()
        self.assertEqual(self.history.look_up_details(client, sleep=pauses.append), 3)
        self.assertEqual(client.requests, [('track', 'top'), ('track', 'gone'), ('track', 'rare')], 'most played first')
        self.assertEqual(pauses, [history.LOOKUP_SPACING_SECONDS] * 2, 'a pause between requests')
        self.assertEqual(self.query("SELECT track_id, found, album_image FROM tracks ORDER BY track_id"),
                         [('gone', False, None), ('rare', True, 'medium'), ('top', True, 'medium')])

        # Then the artists, whose ids came with their songs' details.
        limited = FakeLookupClient(rate_limit_after=1)
        self.assertEqual(self.history.look_up_details(limited, sleep=pauses.append), 1, 'stops at the 429')
        self.assertEqual(limited.requests, [('artist', 'artist_of_top')])
        self.assertEqual(self.query("SELECT album_image, artist_image, seconds FROM v_plays WHERE track_id = 'top' "
                                    "LIMIT 1"), [('medium', 'picture_of_artist_of_top', 60)])
        self.assertEqual(self.history.look_up_details(FakeLookupClient(), sleep=pauses.append), 0,
                         'paused for as long as Spotify asked')

    def test_recorded_plays_bring_their_details(self):
        item = recently_played_item(1, 0)
        item['track']['album']['images'] = [{'url': 'cover', 'width': 300}]
        self.history.record_recent_plays(FakeSpotify([item]))
        self.assertEqual(self.query("SELECT track_id, album_image, artist_ids FROM tracks"),
                         [('t1', 'cover', ['a1', 'a2'])])

    def _write_export(self, rows):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'Streaming_History_Audio_2026_0.json'), 'w', encoding='utf-8') as f:
            json.dump(rows, f)
        return folder

    def test_first_poll_after_an_import_skips_the_overlap(self):
        # The export's last plays are the app's first ones, timed a fraction of a second differently.
        rows = [extended_row((T0 - timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%SZ'), 120_000,
                             f'spotify:track:t{m // 4}', f'Song {m // 4}') for m in (8, 4, 0)]
        self.assertEqual(import_history.main([self._write_export(rows)]), 0)
        items = [recently_played_item(i, -i * 4 + 0.0123) for i in range(3)] + [recently_played_item(9, 5)]
        self.assertEqual(self.history.record_recent_plays(FakeSpotify(items)), 1, 'only the play after the export')
        self.assertEqual(self.query("SELECT count(*) FROM plays")[0][0], 4)

    def test_import_after_the_app_skips_the_overlap(self):
        self.history.record_recent_plays(FakeSpotify([recently_played_item(0, 0.0123), recently_played_item(1, 4)]))
        # The app's first play also ends the export, a moment *before* the app's time for it.
        rows = [extended_row((T0 - timedelta(minutes=4)).strftime('%Y-%m-%dT%H:%M:%SZ'), 120_000, 'spotify:track:old'),
                extended_row(T0.strftime('%Y-%m-%dT%H:%M:%SZ'), 120_000, 'spotify:track:t0', 'Song 0')]
        self.assertEqual(import_history.main([self._write_export(rows)]), 0)
        self.assertEqual(self.query("SELECT source, count(*) FROM plays GROUP BY source ORDER BY source"),
                         [('api', 2), ('export', 1)])


if __name__ == '__main__':
    unittest.main()
