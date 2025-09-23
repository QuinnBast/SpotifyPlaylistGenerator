import spotipy
from spotipy.oauth2 import SpotifyOAuth
import json
import schedule
import time
import logging

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

class PlaylistGenerator:
    def __init__(self, config):
        self.config = config
        scopes = 'playlist-modify-public playlist-read-private playlist-modify-private user-library-read user-top-read'

        self.spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
            scope=scopes,
            client_id = self.config['api']['clientId'],
            client_secret = self.config['api']['clientSecret'],
            username=self.config['credentials']['username'],
            # If you choose an http-scheme URL, and it’s for 127.0.0.1, AND it specifies a port,
            # then spotipy will instantiate a server there to receive the access token from the response
            # Note: This URL must be allowed in the spotify OAuth client in the dev portal
            redirect_uri = "http://127.0.0.1:9123",
            open_browser=False
        ))

        logging.info("Login successful")

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
        response = self.spotify.user_playlist_create(self.spotify.current_user()['id'], playlist_name, False)
        return response['id']


    def most_recently_starred(self):
        if not config['generator']['recentlyAdded']['enabled']:
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

        if not config['generator']['mostPlayed' + configTitle]['enabled']:
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

    def do_generation(self):
        self.most_recently_starred()
        self.most_played("long_term")
        self.most_played("medium_term")
        self.most_played("short_term")

    def schedule_generation(self):
        cadenceHours = config['generator']['cadenceHours']
        schedule.every(cadenceHours).hours.do(self.do_generation)


if __name__ == "__main__":
    with open('config.json') as jsonConfigFile:
        config = json.loads(jsonConfigFile.read())

    logging.info(config)

    generator = PlaylistGenerator(config)

    if config['generator']['recurring']:
        generator.do_generation()
        logging.info("Running on a recurring schedule")
        generator.schedule_generation()
        # Keep the program running.
        while True:
            schedule.run_pending()
            time.sleep(600)  # Sleep for 10 minutes at a time to avoid busy-waiting
    else:
        generator.do_generation()
