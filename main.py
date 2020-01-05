import spotipy
import spotipy.util as util
import json


class PlaylistGenerator:
    def __init__(self):
        with open('config.json') as jsonConfigFile:
            self.config = json.load(jsonConfigFile)

        scopes = 'playlist-modify-public playlist-read-private playlist-modify-private user-library-read'

        print(self.config)

        token = util.prompt_for_user_token(self.config['credentials']['username'], scopes,
                                   client_id=self.config['api']['clientId'],
                                   client_secret=self.config['api']['clientSecret'],
                                   redirect_uri='http://localhost/')


        self.spotify = spotipy.Spotify(auth=token)

        print('login success')

    def most_recently_starred(self, tracksToAdd):
        # Limit the maximum to 50 tracks
        if tracksToAdd > 50:
            tracksToAdd = 50

        # The response automatically returns the most recently starred songs by default
        # Get the user's most recently starred 50 tracks.
        response = self.spotify.current_user_saved_tracks(tracksToAdd, 0)

        most_recently_starred_tracks = []
        # Get a list of the track ids
        for track in response['items']:
            most_recently_starred_tracks.append(track['track']['id'])

        print("Obtained most recent tracks")

        # Check if a playlist with the name 'Most Recently Starred' exists.
        playlistid = None


        response: {
            next: '',
        }

        offset = 0
        while response['next'] is not None and playlistid is None:
            response = self.spotify.current_user_playlists(50, offset)
            offset = offset + 50
            for playlist in response['items']:
                if playlist['name'] == "Most Recently Starred":
                    playlistid = playlist['id']
                    print("Using existing playlist")
                    break

        # If the playlist was not found, create a new
        if playlistid is None:
            print("Playlist doesn't exist. Creating playlist")
            response = self.spotify.user_playlist_create(self.spotify.current_user()['id'], "Most Recently Starred", False)
            playlistid = response['id']

        print("Setting playlist tracks")
        # Replace the tracks in the playlist with the most recently starred tracks
        self.spotify.user_playlist_replace_tracks(self.spotify.current_user()['id'], playlistid, most_recently_starred_tracks)

if __name__ == "__main__":
    generator = PlaylistGenerator()
    generator.most_recently_starred(50)
