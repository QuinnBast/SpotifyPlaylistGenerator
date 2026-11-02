import os
import json
import random
import schedule
import time
import logging
from datetime import datetime, timedelta, timezone

from auth import SpotifyAuth, is_login_expired
from notifications import Notifier

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)


class PlaylistGenerator:
    def __init__(self, config):
        self.config = config
        notifier = Notifier(config.get('notifications', {}))

        # Optional: record every play for the listening history dashboards (needs Postgres and one more permission).
        self.history = None
        extra_scopes = ()
        history_config = config.get('listeningHistory', {})
        if history_config.get('enabled', False):
            import listening_history  # only needs psycopg when enabled
            self.history = listening_history.ListeningHistory(history_config)
            extra_scopes = listening_history.SCOPES

        self.auth = SpotifyAuth(config.get('api', {}), config.get('credentials', {}), config.get('login', {}), notifier,
                                extra_scopes)
        self.spotify = self.auth.authenticate()
        self._liked_tracks = None  # all liked songs, fetched once per generation run

    def create_or_get_playlist_by_name(self, playlist_name):
        # Check if a playlist with the name exists.
        response = {
            "next": '',
        }

        offset = 0
        while response['next'] is not None:
            response = self.spotify.current_user_playlists(50, offset)
            offset = offset + 50
            for playlist in response['items']:
                # "Most Recently Starred"
                if playlist['name'] == playlist_name:
                    logging.info(f"Using existing playlist: {playlist['id']}")
                    return playlist['id']

        # If the playlist was not found, create a new
        logging.info("Playlist doesn't exist. Creating playlist")
        response = self.spotify.current_user_playlist_create(playlist_name, False)
        return response['id']


    def most_recently_starred(self):
        if not self.config['generator']['recentlyAdded']['enabled']:
            return

        tracksToAdd = self.config['generator']['recentlyAdded']['numberOfSongs']
        playlist_name = self.config['generator']['recentlyAdded']['playlistName']

        playlist_id = self.create_or_get_playlist_by_name(playlist_name)

        # The response automatically returns the most recently starred songs by default
        # Get the user's most recently starred 50 tracks.
        response = self.spotify.current_user_saved_tracks(tracksToAdd, 0)

        most_recently_starred_tracks = []
        # Get a list of the track ids
        logging.info("Obtained most recent tracks:")
        current_index = 1
        for track in response['items']:
            logging.info(f"    {current_index}. {track['track']['name']} by {track['track']['artists'][0]['name']} added {track['added_at']}")
            current_index = current_index + 1
            most_recently_starred_tracks.append(track['track']['id'])

        logging.info("Setting playlist tracks")
        # Replace the tracks in the playlist with the most recently starred tracks
        self.spotify.playlist_replace_items(playlist_id, most_recently_starred_tracks)

    def _get_user_top_tracks(self, term, tracks_to_fetch):
        current_offset = 0
        fetched_tracks = []

        while len(fetched_tracks) < tracks_to_fetch:
            required_limit = tracks_to_fetch - len(fetched_tracks)
            if required_limit > 50:
                required_limit = 50

            response = self.spotify.current_user_top_tracks(
                limit=required_limit,
                offset=current_offset,
                time_range=term
            )

            if not response['items']:
                break  # fewer top tracks than requested

            for track in response['items']:
                fetched_tracks.append(track)

            current_offset = current_offset + 50

        return fetched_tracks

    def most_played(self, term):
        configTitle = "ShortTerm"

        if term == "long_term":
            configTitle = "LongTerm"
        elif term == "medium_term":
            configTitle = "MediumTerm"

        if not self.config['generator']['mostPlayed' + configTitle]['enabled']:
            return

        tracksToAdd = self.config['generator']['mostPlayed' + configTitle]['numberOfSongs']
        playlist_name = self.config['generator']['mostPlayed' + configTitle]['playlistName']

        playlist_id = self.create_or_get_playlist_by_name(playlist_name)

        response = self._get_user_top_tracks(term, tracksToAdd)

        user_top_songs = []
        current_index = 1
        # Get a list of the track ids
        logging.info("Obtained user top songs for the past year:")
        for track in response:
            logging.info(f"    {current_index}. {track['name']} by {track['artists'][0]['name']}")
            current_index = current_index + 1
            user_top_songs.append(track['id'])

        logging.info("Setting playlist tracks")
        # Replace the tracks in the playlist with the most recently starred tracks
        self.spotify.playlist_replace_items(playlist_id, user_top_songs)

    def _get_all_liked_tracks(self):
        """Every liked song (saved track) as Spotify returns it: {'added_at': ..., 'track': {...}}. Fetched once per
        generation run, since several playlists use it."""
        if self._liked_tracks is None:
            self._liked_tracks = []
            offset = 0
            while True:
                response = self.spotify.current_user_saved_tracks(50, offset)
                # Local files and unavailable tracks have no id and can't be added to playlists.
                self._liked_tracks.extend(item for item in response['items'] if item['track'] and item['track']['id'])
                if response['next'] is None:
                    break
                offset = offset + 50
            logging.info(f"Fetched {len(self._liked_tracks)} liked songs")
        return self._liked_tracks

    def _get_owned_playlist_track_ids(self, skip_playlist_names):
        """Track ids in every playlist the user created (not ones they follow), except the named playlists."""
        user_id = self.spotify.current_user()['id']
        track_ids = set()
        offset = 0
        while True:
            response = self.spotify.current_user_playlists(50, offset)
            for playlist in response['items']:
                if playlist['owner']['id'] != user_id or playlist['name'] in skip_playlist_names:
                    continue
                items_offset = 0
                while True:
                    # Spotify returns at most 50 per page, each under 'item' ('track' is the deprecated name).
                    items = self.spotify.playlist_items(playlist['id'], fields='items(item(id)),next',
                                                        limit=50, offset=items_offset, additional_types=('track',))
                    track_ids.update(entry['item']['id'] for entry in items['items']
                                     if entry.get('item') and entry['item']['id'])
                    if items['next'] is None:
                        break
                    items_offset = items_offset + 50
            if response['next'] is None:
                break
            offset = offset + 50
        return track_ids

    def _replace_playlist_tracks(self, playlist_name, track_ids):
        playlist_id = self.create_or_get_playlist_by_name(playlist_name)
        logging.info(f"Setting {len(track_ids)} tracks in '{playlist_name}'")
        self.spotify.playlist_replace_items(playlist_id, track_ids)

    def daily_shuffle(self):
        """A random handful of liked songs, new every run (daily with the default cadence)."""
        shuffle_config = self.config['generator'].get('dailyShuffle', {})
        if not shuffle_config.get('enabled', False):
            return

        liked = self._get_all_liked_tracks()
        picked = random.sample(liked, min(shuffle_config['numberOfSongs'], len(liked)))
        self._replace_playlist_tracks(shuffle_config['playlistName'], [item['track']['id'] for item in picked])

    def forgotten_favorites(self):
        """Liked songs added a while ago that aren't in any playlist the user created and aren't among their top
        tracks - songs liked once and never come back to. A random selection that stays the same for a week."""
        forgotten_config = self.config['generator'].get('forgottenFavorites', {})
        if not forgotten_config.get('enabled', False):
            return

        # The shuffling playlists change every run/week; excluding their songs would make this one keep changing too.
        volatile_playlists = {forgotten_config['playlistName'],
                              self.config['generator'].get('dailyShuffle', {}).get('playlistName')}
        in_playlists = self._get_owned_playlist_track_ids(volatile_playlists)
        top_tracks = {track['id'] for term in ("short_term", "medium_term", "long_term")
                      for track in self._get_user_top_tracks(term, 50)}
        liked_before = datetime.now(timezone.utc) - timedelta(days=forgotten_config.get('minDaysSinceLiked', 365))

        candidates = sorted(
            item['track']['id'] for item in self._get_all_liked_tracks()
            if datetime.fromisoformat(item['added_at'].replace('Z', '+00:00')) < liked_before
            and item['track']['id'] not in in_playlists
            and item['track']['id'] not in top_tracks)
        logging.info(f"{len(candidates)} forgotten favourites to pick from")

        # Seeded by the ISO week, so the daily runs keep the same selection until the next week starts.
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        picked = random.Random(f"{year}-W{week}").sample(candidates, min(forgotten_config['numberOfSongs'], len(candidates)))
        self._replace_playlist_tracks(forgotten_config['playlistName'], picked)

    def do_generation(self):
        self._liked_tracks = None  # pick up songs liked since the last run
        self.most_recently_starred()
        self.most_played("long_term")
        self.most_played("medium_term")
        self.most_played("short_term")
        self.daily_shuffle()
        self.forgotten_favorites()

    def _run_scheduled(self, job, what):
        """Runs a scheduled job: logs failures instead of stopping the app, and logs in again (then retries) if the
        login expired since the last run."""
        try:
            job()
        except Exception as e:
            if not is_login_expired(e):
                logging.exception(f"{what} failed; trying again at the next scheduled run.")
                return
            logging.error("Spotify login expired (invalid_grant) - re-authentication required.")
            self.spotify = self.auth.login_and_wait("required")
            job()

    def run_generation(self):
        self._run_scheduled(self.do_generation, "Playlist generation")

    def record_history(self):
        self._run_scheduled(lambda: self.history.record_recent_plays(self.spotify), "Recording listening history")

    def schedule_generation(self):
        cadenceHours = self.config['generator']['cadenceHours']
        schedule.every(cadenceHours).hours.do(self.run_generation)
        if self.history:
            schedule.every(self.history.poll_minutes).minutes.do(self.record_history)


if __name__ == "__main__":
    config_path = os.environ.get('CONFIG_PATH', 'config.json')
    with open(config_path) as jsonConfigFile:
        config = json.loads(jsonConfigFile.read())

    # Only the playlist settings: the config can hold the client secret.
    logging.info(f"Loaded {config_path}; generator settings: {json.dumps(config.get('generator', {}))}")

    generator = PlaylistGenerator(config)

    if config['generator']['recurring']:
        generator.run_generation()
        if generator.history:
            generator.record_history()
            logging.info(f"Listening history: recording every {generator.history.poll_minutes:g} minutes")
        logging.info("Running on a recurring schedule")
        generator.schedule_generation()
        # Keep the program running.
        while True:
            schedule.run_pending()
            generator.auth.check_login_expiry()
            time.sleep(60)  # often enough for the listening history's schedule, without busy-waiting
    else:
        generator.do_generation()
        if generator.history:
            generator.history.record_recent_plays(generator.spotify)
