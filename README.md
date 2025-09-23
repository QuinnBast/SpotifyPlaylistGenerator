# SpotifyPlaylistGenerator
Various Spotify playlist generators to automatically generate common playlists that are not options Spotify by default
This app generates the following playlists and keeps them up to date on a recurring schedule:

- Most recently Starred
- Your top songs (1 year)
- Your top songs (6 months)
- Your top songs (4 weeks)

Note: Requires a spotify developer account & spotify app with a clientID and clientSecret.

# Setup

Clone the repository with `git clone https://github.com/QuinnBast/SpotifyPlaylistGenerator.git`

Setup a config file by renaming the file `config.json.example` to `config.json` and changing the values to reflect your
personal information. Once you have created your config file you can run this app one of two ways:

## As Standalone Python script

To run this as a python script one-time, simply run `python PlaylistGenerator.py`.
This will prompt you to login and automatically generate the configured playlists in your spotify account.

You can then either set this up to run on a schedule using cron-jobs or use some other scheduling service on your own.
Or, using the config, you can make the application just run forever. However I wouldn't recommend doing that as a standalone app.

## As a Docker container

I would recommend this method. After creating your configuration file, use the `Dockerfile` in this repository to build a local docker image.
You can then save the docker image, move it where you need, or upload it to some registry.
You can then use this docker image and move and run it wherever you like and it will run forever on a recurring basis.

I used the following docker compose:

```yaml
services:
  spotify-generator:
    image: spotify-playlist-generator:1.0.0
    ports:
      - "9123:9123" # Maps the Oauth callback server listener when needed.
    # Optional:
    volumes:
      - /some/local/path/config.json:/app/config.json
```

If you want, you can build the docker image without the configuration file so that your credentials are not baked into the image.
To do this, simply build the dockerfile in the repository without your `config.json` file.
Then, when you run the docker image, just mount the correct configuration file into the docker image.

This will make the container re-usable for others, but I am too lazy to build this image and publish it for people to use.