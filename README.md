# SpotifyPlaylistGenerator
Various Spotify playlist generators to automatically generate common playlists that are not options Spotify by default

Note: Requires a spotify developer account & spotify app with a clientID and clientSecret.

### Setup

Clone the repository with `git clone https://github.com/QuinnBast/SpotifyPlaylistGenerator.git`

Setup a config file by renaming the file `config.json.example` to `config.json` and changing the values to reflect your
personal information. Once configured, run the generator with `python PlaylistGenerator.py`

It will prompt you to login to Spotify in the browser (Ensure that your spotify app allows redirects to `http://localhost`).
Once you are taken to a second page, paste in the URL of the page that you are taken to in order to login.

Once logged in, the playlists will be generated. Currently this only generates a "Most recently Starred" playlist that
adds the 50 most recently starred tracks to a playlist for you.