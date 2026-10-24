"""Login flow tests: Spotify (spotipy) and the notification webhook are faked; the callback listener runs for real on
a free local port. Run from the repo root: python -m unittest discover -s tests"""
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

import auth
import notifications
import PlaylistGenerator as pg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class FakeOAuth:
    """Stands in for spotipy's SpotifyOAuth: no cached token, and any login code except 'bad' is accepted."""
    exchanged = []

    def __init__(self, **kwargs):
        self.cache_handler = mock.Mock()
        self.cache_handler.get_cached_token.return_value = None

    def get_authorize_url(self):
        return 'https://accounts.spotify.com/authorize?fake'

    def get_access_token(self, code, as_dict, check_cache):
        if code == 'bad':
            raise Exception('invalid code')
        FakeOAuth.exchanged.append(code)


class FakeSpotify:
    def __init__(self, auth_manager):
        pass

    def current_user(self):
        return {'id': 'me'}


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.posts = []
        FakeOAuth.exchanged = []
        env = {
            'SPOTIFY_CLIENT_ID': 'id', 'SPOTIFY_CLIENT_SECRET': 'secret', 'SPOTIFY_USERNAME': 'me',
            'SPOTIFY_REDIRECT_URI': 'https://spotify.home.example/', 'SPOTIFY_CALLBACK_PORT': str(self.port),
            'SPOTIFY_CACHE_PATH': os.path.join(tempfile.mkdtemp(), 'token-cache.json'),
            'DISCORD_WEBHOOK_URL': 'https://discord.example/webhook', 'DISCORD_MENTION': '<@1>',
        }
        for patch in (mock.patch.dict(os.environ, env),
                      mock.patch.object(auth, 'SpotifyOAuth', FakeOAuth),
                      mock.patch.object(auth.spotipy, 'Spotify', FakeSpotify),
                      mock.patch.object(notifications.requests, 'post', self._fake_post)):
            patch.start()
            self.addCleanup(patch.stop)
        with open(os.path.join(ROOT, 'config.example.json')) as f:
            self.config = json.load(f)

    def _fake_post(self, url, json=None, data=None, headers=None, timeout=None):
        self.posts.append(json or data)
        response = mock.Mock()
        response.raise_for_status = lambda: None
        return response

    def _hit(self, path):
        """GETs the callback listener, retrying until it's up. Returns the HTTP status."""
        for _ in range(100):
            try:
                return urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
            except OSError:
                time.sleep(0.05)
        self.fail('callback listener never came up')

    def _start_generator(self):
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault('generator', pg.PlaylistGenerator(self.config)))
        thread.start()
        return thread, result

    def _logged_in_generator(self):
        thread, result = self._start_generator()
        self.assertEqual(self._hit('/?code=good'), 200)
        thread.join(10)
        return result['generator']

    def test_first_login_waits_and_ignores_stray_and_bad_requests(self):
        thread, result = self._start_generator()

        self.assertEqual(self._hit('/favicon.ico'), 404)
        self.assertEqual(self._hit('/?code=bad'), 200)
        time.sleep(0.3)
        self.assertTrue(thread.is_alive(), 'a bad code must not finish the login')

        self.assertEqual(self._hit('/?code=good'), 200)
        thread.join(10)
        self.assertFalse(thread.is_alive(), 'login should complete')
        self.assertEqual(FakeOAuth.exchanged, ['good'])
        self.assertTrue(os.path.exists(result['generator'].auth.authorized_at_path), 'login time recorded')

        required, complete = self.posts[0], self.posts[-1]
        self.assertEqual(required['content'], '<@1>')
        self.assertIn('Spotify login required', required['embeds'][0]['title'])
        self.assertIn('accounts.spotify.com', required['embeds'][0]['description'])
        self.assertEqual(complete['embeds'][0]['title'], 'Spotify login complete')
        self.assertEqual(complete['content'], '', 'only urgent messages mention you')

    def test_no_reminder_right_after_login(self):
        generator = self._logged_in_generator()
        posts_before = len(self.posts)

        generator.auth.check_login_expiry()
        time.sleep(0.3)

        self.assertEqual(len(self.posts), posts_before)
        self.assertFalse(generator.auth._login_thread.is_alive())

    def test_reminder_before_expiry_and_background_relogin(self):
        generator = self._logged_in_generator()
        posts_before = len(self.posts)
        with open(generator.auth.authorized_at_path, 'w') as f:
            f.write((datetime.now(timezone.utc) - timedelta(days=170)).isoformat())

        generator.auth.check_login_expiry()
        generator.auth.check_login_expiry()  # a second check must not start a second listener
        time.sleep(0.5)

        reminders = [p for p in self.posts[posts_before:] if 'soon' in p['embeds'][0]['title']]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]['content'], '<@1>', 'every login request mentions you')
        self.assertIn('10 days', reminders[0]['embeds'][0]['description'])

        self.assertEqual(self._hit('/?code=renew'), 200)
        generator.auth._login_thread.join(10)
        self.assertEqual(FakeOAuth.exchanged[-1], 'renew')
        self.assertGreater(generator.auth.days_until_token_expiry(), 179, 'a new login restarts the 180 days')

    def test_warning_that_runs_out_becomes_login_required(self):
        generator = self._logged_in_generator()
        with open(generator.auth.authorized_at_path, 'w') as f:
            f.write((datetime.now(timezone.utc) - timedelta(days=170)).isoformat())
        self.assertEqual(generator.auth._login_message('expiring')[0], 'Spotify login needed soon')
        with open(generator.auth.authorized_at_path, 'w') as f:
            f.write((datetime.now(timezone.utc) - timedelta(days=179, hours=12)).isoformat())
        self.assertIn('expires tomorrow', generator.auth._login_message('expiring')[1])
        with open(generator.auth.authorized_at_path, 'w') as f:
            f.write((datetime.now(timezone.utc) - timedelta(days=181)).isoformat())
        self.assertEqual(generator.auth._login_message('expiring')[0], 'Spotify login required')

    def test_expired_login_during_scheduled_run_logs_in_and_retries(self):
        generator = self._logged_in_generator()
        calls = []

        def generation():
            calls.append(1)
            if len(calls) == 1:
                raise Exception('http status: 400, code: -1 - invalid_grant')

        generator.do_generation = generation
        thread = threading.Thread(target=generator.run_generation)
        thread.start()
        self.assertEqual(self._hit('/?code=again'), 200)
        thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 2, 'retried after logging in')
        self.assertEqual(FakeOAuth.exchanged[-1], 'again')

    def test_other_generation_errors_are_logged_not_raised(self):
        generator = self._logged_in_generator()

        def generation():
            raise RuntimeError('network down')

        generator.do_generation = generation
        generator.run_generation()  # must not raise


if __name__ == '__main__':
    unittest.main()
