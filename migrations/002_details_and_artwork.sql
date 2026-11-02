-- How and where each play happened (data exports only: "recently played" doesn't say), and songs' and artists'
-- details from Spotify (album covers, artist pictures), looked up a few at a time by the app.

ALTER TABLE plays
    ADD COLUMN platform text,        -- the device, as Spotify names it, e.g. "iOS 8.0.2 (iPhone5,3)"
    ADD COLUMN country text,         -- where you were (ISO code; ZZ = unknown)
    ADD COLUMN shuffle boolean,
    ADD COLUMN skipped boolean,
    ADD COLUMN reason_start text,    -- why it started: trackdone (the previous one ended), clickrow, fwdbtn, ...
    ADD COLUMN reason_end text,      -- why it ended: trackdone, fwdbtn (skipped), endplay, ...
    ADD COLUMN offline boolean,
    ADD COLUMN incognito boolean;

-- One row per Spotify track id: from "recently played" as plays are recorded, else looked up (GET /tracks/{id}).
CREATE TABLE tracks (
    track_id text PRIMARY KEY,
    found boolean NOT NULL,          -- false: Spotify no longer has it
    track_name text,
    duration_ms integer,
    artist_ids text[],
    artist_names text[],
    album_id text,
    album_name text,
    album_image text,                -- cover URL (~300 px)
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per Spotify artist id, looked up (GET /artists/{id}).
CREATE TABLE artists (
    artist_id text PRIMARY KEY,
    found boolean NOT NULL,
    name text,
    image text,                      -- picture URL (~300 px)
    genres text[],
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- New columns go at the end (CREATE OR REPLACE VIEW can only add columns).
CREATE OR REPLACE VIEW v_plays AS
SELECT p.played_at,
       p.played_at AT TIME ZONE s.timezone AS local_time,
       p.track_key,
       p.track_name,
       p.artist_names[1] AS artist,
       p.artist_names,
       p.album_name,
       round(COALESCE(p.ms_played, p.duration_ms, t.duration_ms, 0) / 1000.0)::integer AS seconds,
       p.context_type,
       p.source,
       p.track_id,
       t.album_image,
       a.image AS artist_image,
       COALESCE(p.artist_ids[1], t.artist_ids[1]) AS artist_id,
       CASE WHEN p.platform IS NULL THEN NULL
            WHEN p.platform ILIKE 'ios%' THEN 'iPhone / iPad'
            WHEN p.platform ILIKE 'android%' THEN 'Android'
            WHEN p.platform ILIKE 'windows%' THEN 'Windows'
            WHEN p.platform ILIKE 'os x%' OR p.platform ILIKE 'macos%' THEN 'Mac'
            WHEN p.platform ILIKE 'linux%' THEN 'Linux'
            WHEN p.platform ILIKE 'web%' THEN 'Web player'
            WHEN p.platform ILIKE 'partner%' THEN 'TV / speaker / car'
            ELSE 'Other' END AS device,
       p.platform,
       NULLIF(p.country, 'ZZ') AS country,
       p.shuffle,
       p.skipped,
       p.reason_start,
       p.reason_end,
       p.offline
FROM plays p
CROSS JOIN settings s
LEFT JOIN tracks t ON t.track_id = p.track_id
LEFT JOIN artists a ON a.artist_id = COALESCE(p.artist_ids[1], t.artist_ids[1]);
