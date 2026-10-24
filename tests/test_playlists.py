"""Daily Shuffle and Forgotten Favourites against a fake Spotify library (no login, no network).
Run from the repo root: python -m unittest discover -s tests"""
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import PlaylistGenerator as pg

NOW = datetime.now(timezone.utc)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def liked(i, days_ago):
    return {'added_at': (NOW - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'track': {'id': f't{i}', 'name': f'T{i}', 'artists': [{'name': 'A'}]}}


# 120 liked songs: t0-t19 liked recently, t20-t119 two years ago; plus a local file, which has no id.
LIKED = ([liked(i, 10) for i in range(20)] + [liked(i, 730) for i in range(20, 120)]
         + [{'added_at': '2020-01-01T00:00:00Z', 'track': {'id': None}}])
TOP_TRACKS = ['t20', 't21', 't22']   # old likes that are still among the top tracks
PLAYLIST_TRACKS = {
    'pl_mine': ['t30', 't31', None],   # a playlist you created (None: a local file)
    'pl_forgotten': ['t40'],           # the generated playlists themselves
    'pl_shuffle': ['t41'],
    'pl_followed': ['t50'],            # someone else's playlist you follow
}


class FakeSpotify:
    def __init__(self):
        self.replaced = {}
        self.playlists = [
            {'id': 'pl_mine', 'name': 'My Mix', 'owner': {'id': 'me'}},
            {'id': 'pl_forgotten', 'name': 'Forgotten Favourites', 'owner': {'id': 'me'}},
            {'id': 'pl_shuffle', 'name': 'Daily Shuffle', 'owner': {'id': 'me'}},
            {'id': 'pl_followed', 'name': 'Followed', 'owner': {'id': 'someone else'}},
        ]

    def current_user(self):
        return {'id': 'me'}

    def current_user_saved_tracks(self, limit, offset):
        return {'items': LIKED[offset:offset + limit], 'next': 'more' if offset + limit < len(LIKED) else None}

    def current_user_playlists(self, limit, offset):
        return {'items': self.playlists[offset:offset + limit], 'next': None}

    def playlist_items(self, playlist_id, fields, limit, offset, additional_types):
        ids = PLAYLIST_TRACKS[playlist_id][offset:offset + limit]
        return {'items': [{'track': {'id': i} if i else None} for i in ids], 'next': None}

    def current_user_top_tracks(self, limit, offset, time_range):
        tracks = [{'id': t, 'name': t, 'artists': [{'name': 'A'}]} for t in TOP_TRACKS]
        return {'items': tracks[offset:offset + limit]}   # fewer than requested

    def user_playlist_create(self, user, name, public):
        return {'id': name}

    def playlist_replace_items(self, playlist_id, track_ids):
        self.replaced[playlist_id] = list(track_ids)


CONFIG = {'generator': {
    'recurring': False, 'cadenceHours': 24,
    'recentlyAdded': {'enabled': False},
    'mostPlayedLongTerm': {'enabled': False},
    'mostPlayedMediumTerm': {'enabled': False},
    'mostPlayedShortTerm': {'enabled': False},
    'dailyShuffle': {'enabled': True, 'playlistName': 'Daily Shuffle', 'numberOfSongs': 25},
    'forgottenFavorites': {'enabled': True, 'playlistName': 'Forgotten Favourites', 'numberOfSongs': 30,
                           'minDaysSinceLiked': 365},
}}


class PlaylistsTest(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(pg.SpotifyAuth, '__init__', lambda self, *args: None),
                      mock.patch.object(pg.SpotifyAuth, 'authenticate', lambda self: FakeSpotify())):
            patch.start()
            self.addCleanup(patch.stop)
        self.generator = pg.PlaylistGenerator(CONFIG)
        self.spotify = self.generator.spotify
        self.generator.do_generation()

    def test_daily_shuffle_has_distinct_liked_songs(self):
        shuffle = self.spotify.replaced['pl_shuffle']
        self.assertEqual(len(shuffle), 25)
        self.assertEqual(len(set(shuffle)), 25)
        self.assertNotIn(None, shuffle, 'local files are skipped')

    def test_daily_shuffle_changes_every_run(self):
        first = self.spotify.replaced['pl_shuffle']
        self.generator.do_generation()
        self.assertNotEqual(self.spotify.replaced['pl_shuffle'], first)

    def test_forgotten_favorites_rules(self):
        forgotten = self.spotify.replaced['pl_forgotten']
        self.assertEqual(len(forgotten), 30)
        self.assertEqual(len(set(forgotten)), 30)
        allowed = {f't{i}' for i in range(20, 120)} - set(TOP_TRACKS) - {'t30', 't31'}
        self.assertLessEqual(set(forgotten), allowed,
                             'only likes older than a year, not top tracks, not in playlists you created')

    def test_only_playlists_you_created_count(self):
        excluded = self.generator._get_owned_playlist_track_ids({'Forgotten Favourites', 'Daily Shuffle'})
        self.assertEqual(excluded, {'t30', 't31'}, 'not followed playlists, not the shuffling playlists')

    def test_forgotten_favorites_same_all_week_new_next_week(self):
        first = self.spotify.replaced['pl_forgotten']

        self.generator.do_generation()
        self.assertEqual(self.spotify.replaced['pl_forgotten'], first, 'same week -> same selection')

        with mock.patch.object(pg, 'datetime', wraps=datetime) as fake_datetime:
            fake_datetime.now.return_value = NOW + timedelta(days=7)
            self.generator.do_generation()
        self.assertNotEqual(self.spotify.replaced['pl_forgotten'], first, 'next week -> new selection')

    def test_fewer_top_tracks_than_requested(self):
        self.assertEqual(len(self.generator._get_user_top_tracks('short_term', 50)), 3)


if __name__ == '__main__':
    unittest.main()
