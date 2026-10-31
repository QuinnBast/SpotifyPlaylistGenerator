-- Listening history: one row per play of a song. Times are timestamptz (UTC); days and hours on the dashboards use
-- the time zone in settings, which the app sets from its TZ at every start.

CREATE TABLE settings (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    timezone text NOT NULL DEFAULT 'UTC'
);
INSERT INTO settings DEFAULT VALUES;

CREATE TABLE plays (
    -- When the play ended: Spotify's played_at (recently played) and ts/endTime (data exports) both mark the end.
    played_at timestamptz NOT NULL,
    -- The Spotify track id, or "artist / track" (lowercased) for the basic data export, which has no ids.
    track_key text NOT NULL,
    track_id text,
    track_name text NOT NULL,
    -- Every credited artist from the API; the data exports only name the main one.
    artist_names text[] NOT NULL,
    artist_ids text[],
    album_id text,
    album_name text,
    duration_ms integer,             -- the track's length (API only)
    ms_played integer,               -- how long it actually played (data exports only)
    context_type text,               -- what it was played from: album, playlist, artist, ... (API only)
    context_uri text,
    source text NOT NULL CHECK (source IN ('api', 'export')),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (played_at, track_key)   -- re-reading the same plays (API polls, imports) adds nothing
);
CREATE INDEX plays_track_key ON plays (track_key);

-- What the dashboards query. artist = the main artist, so API plays and exports count the same way.
CREATE VIEW v_plays AS
SELECT p.played_at,
       p.played_at AT TIME ZONE s.timezone AS local_time,   -- wall-clock time where you listened
       p.track_key,
       p.track_name,
       p.artist_names[1] AS artist,
       p.artist_names,
       p.album_name,
       -- Time listened: the real figure from exports, the track length for API plays (a play is >= 30 s).
       round(COALESCE(p.ms_played, p.duration_ms, 0) / 1000.0)::integer AS seconds,
       p.context_type,
       p.source
FROM plays p
CROSS JOIN settings s;
