-- Plays shorter than 30 seconds from data exports. They aren't plays as Spotify counts them (and not in `plays`, so
-- they don't count anywhere else), but they're where nearly all skips are: kept only for the skip stats.
CREATE TABLE short_plays (
    played_at timestamptz NOT NULL,
    track_key text NOT NULL,
    track_id text,
    track_name text NOT NULL,
    artist_names text[] NOT NULL,
    album_name text,
    ms_played integer,
    platform text,
    country text,
    shuffle boolean,
    skipped boolean,
    reason_start text,
    reason_end text,
    offline boolean,
    incognito boolean,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (played_at, track_key)
);

-- Every time a song started, as far as the data exports tell (both tables, only rows with reason_end), and whether
-- you skipped it: moved to the next or previous song, or - in its first 30 seconds - picked something else or
-- stopped (endplay; later on, that's usually just stopping listening).
CREATE VIEW v_song_starts AS
SELECT x.played_at,
       x.played_at AT TIME ZONE s.timezone AS local_time,
       x.track_id,
       x.track_name,
       x.artist_names[1] AS artist,
       x.album_name,
       t.album_image,
       x.ms_played,
       x.reason_end,
       x.reason_end IN ('fwdbtn', 'backbtn') OR (x.reason_end = 'endplay' AND x.ms_played < 30000) AS skip
FROM (SELECT played_at, track_id, track_name, artist_names, album_name, ms_played, reason_end FROM plays
      UNION ALL
      SELECT played_at, track_id, track_name, artist_names, album_name, ms_played, reason_end FROM short_plays) x
CROSS JOIN settings s
LEFT JOIN tracks t ON t.track_id = x.track_id
WHERE x.reason_end IS NOT NULL;
