"""Generates spotify-listening.json (Grafana, over the listening history Postgres - see listening_history.py).

    python dashboards/build.py dashboards

Edit this, not the JSON (which it overwrites). SQL is joined onto one line, so no -- comments in it.
"""
import json
import sys
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "spotify-history"}
# The local time zone, as the app set it from its TZ (see migrations/001_listening_history.sql).
TZ = "(SELECT timezone FROM settings)"
TODAY = f"(now() AT TIME ZONE {TZ})::date"
GREEN = "#1db954"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

panels = []
_id = [0]
_y = [0]


def at(x, w, h):
    """A grid position in the current row (rows are laid out top to bottom; see next_row)."""
    return x, _y[0], w, h


def next_row(h):
    _y[0] += h


def section(title):
    """A heading across the dashboard."""
    _id[0] += 1
    panels.append({"id": _id[0], "type": "row", "title": title, "collapsed": False, "panels": [],
                   "gridPos": {"x": 0, "y": _y[0], "w": 24, "h": 1}})
    next_row(1)


def panel(kind, title, grid, sql, fmt="table", description=None, **extra):
    _id[0] += 1
    p = {
        "id": _id[0], "type": kind, "title": title, "datasource": DS,
        "gridPos": dict(zip("xywh", grid)),
        "targets": [{"refId": "A", "datasource": DS, "editorMode": "code", "format": fmt, "rawQuery": True,
                     "rawSql": " ".join(sql.split())}],
    }
    if description:
        p["description"] = description
    p.update(extra)
    panels.append(p)
    return p


def stat(title, grid, sql, unit="none", decimals=0, description=None, text=False):
    text_size = {"text": {"valueSize": 22}} if text else {}
    return panel("stat", title, grid, sql, description=description,
                 fieldConfig={"defaults": {"unit": unit, "decimals": decimals,
                                           "color": {"mode": "fixed", "fixedColor": "text"}}, "overrides": []},
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text else "",
                                            "values": False},
                          "colorMode": "none", "graphMode": "none", "textMode": "value", "justifyMode": "auto",
                          "orientation": "auto", "wideLayout": True, "showPercentChange": False, **text_size})


IN_RANGE = "$__timeFilter(played_at)"
# Songs are grouped by name, not id: the basic data export has no ids, and Spotify gives the same song new ids
# (singles, albums, re-releases).
SONG = "lower(artist), lower(track_name)"
# A picture for a group of plays: any of theirs (looked up by the app a few at a time, most played first). Until it
# has one, a transparent pixel: Grafana shows a broken-image icon for an empty image cell.
BLANK = "'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'"
COVER = f"COALESCE((array_agg(album_image) FILTER (WHERE album_image IS NOT NULL))[1], {BLANK})"
PICTURE = f"COALESCE((array_agg(artist_image) FILTER (WHERE artist_image IS NOT NULL))[1], {BLANK})"
RANK = 'row_number() OVER (ORDER BY count(*) DESC, max(played_at) DESC) AS "#"'
EXPORT_ONLY = ("Only Spotify data exports say this (the app's own recordings can't), so it covers plays up to "
               "your latest import.")


# --- Table helpers ---------------------------------------------------------------------------------------------
def by_name(name, *properties):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k, "value": v} for k, v in properties]}


def plays_bar(name="Plays"):
    return by_name(name, ("custom.cellOptions", {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}),
                   ("color", {"mode": "fixed", "fixedColor": GREEN}), ("custom.width", 100))


HOURS = by_name("Hours", ("unit", "suffix: h"), ("decimals", 1), ("custom.width", 70))
RANK_COLUMN = by_name("#", ("custom.width", 45), ("custom.align", "right"))


def image(name, n=1):
    # No header text; each image column needs its own (invisible) one, or Grafana shows the first one's pictures in all.
    return by_name(name, ("custom.cellOptions", {"type": "image"}), ("custom.width", 52), ("displayName", "​" * n))


def table(title, grid, sql, overrides=(), description=None, images=(), cell_height=None):
    """A table; columns named in `images` hold picture URLs."""
    return panel("table", title, grid, sql, description=description,
                 fieldConfig={"defaults": {"unit": "none"},
                              "overrides": [RANK_COLUMN, plays_bar(), HOURS,
                                            *(image(name, n) for n, name in enumerate(images, 1)), *overrides]},
                 options={"showHeader": True, "cellHeight": cell_height or ("lg" if images else "sm"),
                          "footer": {"show": False}})


def bars(title, grid, sql, unit="suffix: h", description=None):
    """A horizontal bar per row: the first column names it, the second is its value."""
    return panel("bargauge", title, grid, sql, description=description,
                 fieldConfig={"defaults": {"unit": unit, "decimals": 1, "min": 0,
                                           "color": {"mode": "fixed", "fixedColor": GREEN}}, "overrides": []},
                 options={"orientation": "horizontal", "displayMode": "basic", "valueMode": "text",
                          "showUnfilled": True, "namePlacement": "left", "sizing": "auto",
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}})


def bar_chart_by_time(title, grid, sql, unit, description=None):
    return panel("timeseries", title, grid, sql, fmt="time_series", description=description,
                 fieldConfig={"defaults": {"unit": unit, "decimals": 1 if unit.endswith("h") else 0, "min": 0,
                                           "color": {"mode": "fixed", "fixedColor": GREEN},
                                           "custom": {"drawStyle": "bars", "barAlignment": -1, "barWidthFactor": 0.8,
                                                      "fillOpacity": 85, "lineWidth": 1, "showPoints": "never",
                                                      "axisSoftMin": 0, "gradientMode": "none"}}, "overrides": []},
                 options={"legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}})


# Consecutive days with music: each run of days is one streak (days minus their position is constant within one).
STREAKS = f"""
    WITH days AS (SELECT DISTINCT local_time::date AS d FROM v_plays WHERE {{where}}),
         runs AS (SELECT d, d - (row_number() OVER (ORDER BY d))::integer AS run FROM days)
    SELECT count(*) AS days, min(d) AS first_day, max(d) AS last_day FROM runs GROUP BY run"""

# === Headline numbers =============================================================================================
stat("Hours listened", at(0, 4, 4),
     f"SELECT COALESCE(round(sum(seconds) / 3600.0, 1), 0) FROM v_plays WHERE {IN_RANGE}", unit="suffix: h",
     decimals=1, description="Data-export plays count the time actually played; recorded plays count the song's "
                             "length (Spotify only lists plays of 30 s or more).")
stat("Plays", at(4, 3, 4), f"SELECT count(*) FROM v_plays WHERE {IN_RANGE}")
stat("Songs", at(7, 3, 4), f"SELECT count(DISTINCT ({SONG})) FROM v_plays WHERE {IN_RANGE}",
     description="Different songs played.")
stat("Artists", at(10, 3, 4), f"SELECT count(DISTINCT lower(artist)) FROM v_plays WHERE {IN_RANGE}",
     description="Different main artists played.")
stat("Favourite artist", at(13, 5, 4),
     f"""SELECT COALESCE((SELECT max(artist) || ' · ' || count(*) || ' plays' FROM v_plays WHERE {IN_RANGE}
                         GROUP BY lower(artist) ORDER BY count(*) DESC LIMIT 1), '-')""", text=True)
stat("Most played song", at(18, 6, 4),
     f"""SELECT COALESCE((SELECT max(track_name) || ' – ' || max(artist) || ' · ' || count(*) || ' plays'
                         FROM v_plays WHERE {IN_RANGE} GROUP BY {SONG} ORDER BY count(*) DESC LIMIT 1), '-')""",
     text=True)
next_row(4)

stat("Current streak", at(0, 4, 3),
     f"""SELECT COALESCE((SELECT days || CASE WHEN days = 1 THEN ' day' ELSE ' days' END
                         FROM ({STREAKS.format(where="true")}) s WHERE last_day >= {TODAY} - 1), '0 days')""",
     text=True, description="Days in a row with music, up to today (or yesterday, if you haven't listened yet "
                            "today). Not limited by the time range.")
stat("Longest streak", at(4, 9, 3),
     f"""SELECT COALESCE((SELECT days || ' days · ' || to_char(first_day, 'DD Mon YYYY') || ' – '
                                 || to_char(last_day, 'DD Mon YYYY')
                         FROM ({STREAKS.format(where=IN_RANGE)}) s ORDER BY days DESC, last_day DESC LIMIT 1), '-')""",
     text=True, description="The most days in a row with music, in the selected time range.")
stat("Days with music", at(13, 4, 3),
     f"""SELECT round(100.0 * count(DISTINCT local_time::date)
                      / GREATEST(1, (($__timeTo())::date - ($__timeFrom())::date)), 0)
         FROM v_plays WHERE {IN_RANGE}""", unit="percent",
     description="Share of the days in the selected range on which you listened to anything.")
stat("New artists", at(17, 3, 3),
     f"""SELECT count(*) FROM (SELECT min(played_at) AS first_played FROM v_plays GROUP BY lower(artist)) f
         WHERE $__timeFilter(first_played)""",
     description="Artists you first played in the selected range (going by your whole history).")
stat("Shuffle", at(20, 4, 3), f"SELECT round(100.0 * avg(shuffle::integer), 0) FROM v_plays WHERE {IN_RANGE}",
     unit="percent", description="Share of plays with shuffle on. " + EXPORT_ONLY)
next_row(3)

# === Over time ====================================================================================================
section("Over time")
bar_chart_by_time("Hours per $bucket", at(0, 24, 8),
                  f"""SELECT date_trunc('$bucket', local_time) AT TIME ZONE {TZ} AS time,
                             round(sum(seconds) / 3600.0, 2) AS "Hours"
                      FROM v_plays WHERE {IN_RANGE} GROUP BY 1 ORDER BY 1""", "suffix: h")
next_row(8)

YEAR = "extract(year FROM local_time)::integer"
table("Year by year", at(0, 24, 16),
      f"""WITH p AS (SELECT {YEAR} AS year, * FROM v_plays),
               songs AS (SELECT year, max(track_name) || ' – ' || max(artist) AS name, {COVER} AS cover,
                                row_number() OVER (PARTITION BY year ORDER BY count(*) DESC) AS n
                         FROM p GROUP BY year, lower(artist), lower(track_name)),
               artists AS (SELECT year, max(artist) AS name, {PICTURE} AS picture,
                                  row_number() OVER (PARTITION BY year ORDER BY count(*) DESC) AS n
                           FROM p GROUP BY year, lower(artist)),
               albums AS (SELECT year, max(album_name) || ' – ' || max(artist) AS name, {COVER} AS cover,
                                 row_number() OVER (PARTITION BY year ORDER BY count(*) DESC) AS n
                          FROM p WHERE album_name IS NOT NULL GROUP BY year, lower(artist), lower(album_name)),
               firsts AS (SELECT min({YEAR}) AS year FROM v_plays GROUP BY lower(artist)),
               totals AS (SELECT year, round(sum(seconds) / 3600.0, 1) AS hours, count(*) AS plays,
                                 count(DISTINCT lower(artist)) AS artists FROM p GROUP BY year)
          SELECT t.year::text AS "Year", t.hours AS "Hours", t.plays AS "Plays",
                 s.cover AS "Song cover", s.name AS "Top song", a.picture AS "Artist picture", a.name AS "Top artist",
                 al.cover AS "Album cover", al.name AS "Top album", t.artists AS "Artists",
                 (SELECT count(*) FROM firsts f WHERE f.year = t.year) AS "New artists"
          FROM totals t
          LEFT JOIN songs s ON s.year = t.year AND s.n = 1
          LEFT JOIN artists a ON a.year = t.year AND a.n = 1
          LEFT JOIN albums al ON al.year = t.year AND al.n = 1
          ORDER BY t.year DESC""",
      images=("Song cover", "Artist picture", "Album cover"),
      overrides=[by_name("Year", ("custom.width", 60)), by_name("Plays", ("custom.width", 80)),
                 by_name("Plays", ("custom.cellOptions", {"type": "auto"})),
                 by_name("Artists", ("custom.width", 70)), by_name("New artists", ("custom.width", 95))],
      description="Your own Wrapped for every year: top song, artist and album, and how many artists you heard and "
                  "discovered (first played that year). All time, not limited by the time range.")
next_row(16)

# === When (a play counts in the hour it ended) ====================================================================
section("When")
GITHUB_GREEN = {"mode": "opacity", "fill": "#39d353", "scale": "exponential", "exponent": 0.6, "min": 0,
                "steps": 64}
day_columns = ", ".join(
    f"""round(COALESCE(sum(p.seconds) FILTER (WHERE EXTRACT(ISODOW FROM p.local_time) = {i + 1}), 0) / 3600.0, 2)
        AS "{d}\"""" for i, d in reversed(list(enumerate(WEEKDAYS))))
panel("heatmap", "Days listened", at(0, 24, 7),
      f"""SELECT w.week AT TIME ZONE {TZ} AS time, {day_columns}
          FROM generate_series(date_trunc('week', $__timeFrom() AT TIME ZONE {TZ}),
                               $__timeTo() AT TIME ZONE {TZ}, interval '1 week') AS w (week)
          LEFT JOIN v_plays p ON date_trunc('week', p.local_time) = w.week AND $__timeFilter(p.played_at)
          GROUP BY w.week ORDER BY w.week""",
      description="Hours of listening per day over the last year, one column per week (like GitHub's contribution "
                  "graph). Not limited by the time range.",
      timeFrom="1y",
      fieldConfig={"defaults": {"unit": "suffix: h", "decimals": 1}, "overrides": []},
      options={"calculate": False, "cellGap": 2, "cellValues": {"unit": "suffix: h", "decimals": 1},
               "color": GITHUB_GREEN, "yAxis": {"axisPlacement": "left", "reverse": False},
               "rowsFrame": {"layout": "unknown"}, "showValue": "never", "legend": {"show": True},
               "tooltip": {"mode": "single", "yHistogram": False, "showColorScale": False},
               "exemplars": {"color": "transparent"}, "filterValues": {"le": 0.01}})
next_row(7)

hour_columns = ", ".join(
    f"""round(COALESCE(sum(p.seconds) FILTER (WHERE EXTRACT(HOUR FROM p.local_time) = {h}), 0) / 3600.0, 1)
        AS "{h:02d}\"""" for h in range(24))
weekday_values = ", ".join(f"({i + 1}, '{d}')" for i, d in enumerate(WEEKDAYS))
panel("table", "Hours by weekday × hour of day", at(0, 24, 9),
      f"""SELECT d.label AS "Day", {hour_columns}
          FROM (VALUES {weekday_values}) AS d (dow, label)
          LEFT JOIN v_plays p ON EXTRACT(ISODOW FROM p.local_time) = d.dow AND $__timeFilter(p.played_at)
          GROUP BY d.dow, d.label ORDER BY d.dow""",
      description="Each cell: hours of listening in that clock hour (local time) on that weekday, over the whole "
                  "selected range - e.g. Thu / 21 = everything between 21:00 and 22:00 on every Thursday.",
      fieldConfig={"defaults": {"unit": "none", "decimals": 1, "min": 0, "color": {"mode": "continuous-greens"},
                                "custom": {"align": "center", "minWidth": 40,
                                           "cellOptions": {"type": "color-background", "mode": "gradient"}},
                                "mappings": [{"type": "value", "options": {"0": {"text": " "}}}]},
                   "overrides": [by_name("Day", ("custom.width", 70), ("custom.cellOptions", {"type": "auto"}),
                                         ("custom.align", "left"))]},
      options={"showHeader": True, "cellHeight": "md", "footer": {"show": False}})
next_row(9)

# Sessions: a new one starts when a song starts more than 20 minutes after the previous one ended.
table("Longest listening sessions", at(0, 12, 10),
      f"""WITH p AS (SELECT played_at, seconds, artist,
                            played_at - make_interval(secs => seconds) AS started_at,
                            lag(played_at) OVER (ORDER BY played_at) AS previous_end
                     FROM v_plays WHERE {IN_RANGE}),
               s AS (SELECT *, sum(CASE WHEN previous_end IS NULL
                                             OR started_at - previous_end > interval '20 minutes' THEN 1 ELSE 0 END)
                                   OVER (ORDER BY played_at) AS session FROM p)
          SELECT row_number() OVER (ORDER BY sum(seconds) DESC) AS "#",
                 min(started_at) AS "Started", round(sum(seconds) / 3600.0, 1) AS "Hours", count(*) AS "Songs",
                 mode() WITHIN GROUP (ORDER BY artist) AS "Mostly"
          FROM s GROUP BY session ORDER BY sum(seconds) DESC LIMIT 25""",
      overrides=[by_name("Started", ("unit", "time: ddd YYYY-MM-DD HH:mm"), ("custom.width", 170)),
                 by_name("Songs", ("custom.width", 70))],
      description="Stretches of listening with no break longer than 20 minutes. Mostly = the artist you played most "
                  "in it.")
table("Late night favourites (midnight – 4 am)", at(12, 12, 10),
      f"""SELECT {RANK}, {COVER} AS "Cover", max(track_name) AS "Song", max(artist) AS "Artist", count(*) AS "Plays"
          FROM v_plays WHERE {IN_RANGE} AND EXTRACT(HOUR FROM local_time) < 4
          GROUP BY {SONG} ORDER BY count(*) DESC, max(played_at) DESC LIMIT 50""",
      images=("Cover",))
next_row(10)

# === Favourites ===================================================================================================
section("Favourites")
table("Top artists", at(0, 8, 14),
      f"""SELECT {RANK}, {PICTURE} AS "Picture", max(artist) AS "Artist", count(*) AS "Plays",
                 round(sum(seconds) / 3600.0, 1) AS "Hours", count(DISTINCT lower(track_name)) AS "Songs"
          FROM v_plays WHERE {IN_RANGE} GROUP BY lower(artist) ORDER BY count(*) DESC, max(played_at) DESC LIMIT 100""",
      images=("Picture",), overrides=[by_name("Songs", ("custom.width", 65))])
table("Top songs", at(8, 8, 14),
      f"""SELECT {RANK}, {COVER} AS "Cover", max(track_name) AS "Song", max(artist) AS "Artist", count(*) AS "Plays"
          FROM v_plays WHERE {IN_RANGE} GROUP BY {SONG} ORDER BY count(*) DESC, max(played_at) DESC LIMIT 100""",
      images=("Cover",))
table("Top albums", at(16, 8, 14),
      f"""SELECT {RANK}, {COVER} AS "Cover", max(album_name) AS "Album", max(artist) AS "Artist", count(*) AS "Plays"
          FROM v_plays WHERE {IN_RANGE} AND album_name IS NOT NULL
          GROUP BY lower(artist), lower(album_name) ORDER BY count(*) DESC, max(played_at) DESC LIMIT 100""",
      images=("Cover",), description="The basic data export has no albums, so its plays aren't counted here.")
next_row(14)

table("Obsessions: most plays of one song in a day", at(0, 12, 10),
      f"""SELECT {RANK}, local_time::date AS "Day", {COVER} AS "Cover", max(track_name) AS "Song",
                 max(artist) AS "Artist", count(*) AS "Plays"
          FROM v_plays WHERE {IN_RANGE}
          GROUP BY local_time::date, {SONG} ORDER BY count(*) DESC, max(played_at) DESC LIMIT 50""",
      images=("Cover",), overrides=[by_name("Day", ("unit", "time: YYYY-MM-DD"), ("custom.width", 100))])
table("Forgotten favourites: not played in a year", at(12, 12, 10),
      f"""SELECT {RANK}, {COVER} AS "Cover", max(track_name) AS "Song", max(artist) AS "Artist", count(*) AS "Plays",
                 max(played_at) AS "Last played"
          FROM v_plays GROUP BY {SONG} HAVING max(played_at) < now() - interval '1 year'
          ORDER BY count(*) DESC, max(played_at) DESC LIMIT 50""",
      images=("Cover",), overrides=[by_name("Last played", ("unit", "time: YYYY-MM-DD"), ("custom.width", 100))],
      description="Your most played songs ever that you haven't played for over a year. All time, not limited by "
                  "the time range.")
next_row(10)

# === Discovery ====================================================================================================
section("Discovery")
FIRSTS = f"""SELECT lower(artist) AS key, max(artist) AS artist, min(played_at) AS first_played, count(*) AS plays,
                   {PICTURE} AS picture
            FROM v_plays GROUP BY lower(artist)"""
bar_chart_by_time("New artists per $bucket", at(0, 14, 10),
                  f"""SELECT date_trunc('$bucket', first_played AT TIME ZONE {TZ}) AT TIME ZONE {TZ} AS time,
                             count(*) AS "New artists"
                      FROM ({FIRSTS}) f WHERE $__timeFilter(first_played) GROUP BY 1 ORDER BY 1""", "none",
                  description="Artists you played for the first time ever (going by your whole history).")
table("Artists you found in this range", at(14, 10, 10),
      f"""SELECT row_number() OVER (ORDER BY plays DESC, first_played) AS "#", picture AS "Picture",
                 artist AS "Artist", first_played AS "First played", plays AS "Plays"
          FROM ({FIRSTS}) f WHERE $__timeFilter(first_played) ORDER BY plays DESC, first_played LIMIT 50""",
      images=("Picture",), overrides=[by_name("First played", ("unit", "time: YYYY-MM-DD"), ("custom.width", 100))],
      description="Artists first played in the selected range, by how much you've played them since (ever).")
next_row(10)

# === Where and how (data exports only) ===========================================================================
section("Where and how")
bars("Devices", at(0, 8, 8),
     f"""SELECT device AS "Device", round(sum(seconds) / 3600.0, 1) AS "Hours" FROM v_plays
         WHERE {IN_RANGE} AND device IS NOT NULL GROUP BY device ORDER BY 2 DESC""",
     description="Hours per kind of device. " + EXPORT_ONLY)
bars("Countries", at(8, 6, 8),
     f"""SELECT country AS "Country", round(sum(seconds) / 3600.0, 1) AS "Hours" FROM v_plays
         WHERE {IN_RANGE} AND country IS NOT NULL GROUP BY country ORDER BY 2 DESC LIMIT 15""",
     description="Where you listened (two-letter country codes). " + EXPORT_ONLY)
table("Most skipped songs", at(14, 10, 8),
      f"""SELECT row_number() OVER (ORDER BY avg((reason_end IN ('fwdbtn', 'backbtn'))::integer) DESC, count(*) DESC)
                 AS "#", {COVER} AS "Cover", max(track_name) AS "Song", max(artist) AS "Artist",
                 count(*) AS "Plays",
                 round(100.0 * avg((reason_end IN ('fwdbtn', 'backbtn'))::integer)) AS "Skipped"
          FROM v_plays WHERE {IN_RANGE} AND reason_end IS NOT NULL
          GROUP BY {SONG} HAVING count(*) >= 10
          ORDER BY avg((reason_end IN ('fwdbtn', 'backbtn'))::integer) DESC, count(*) DESC LIMIT 50""",
      images=("Cover",), overrides=[by_name("Skipped", ("unit", "percent"), ("custom.width", 75))],
      description="Songs (played at least 10 times) you most often skipped to the next or previous song after the "
                  "first 30 seconds - shorter plays aren't kept. " + EXPORT_ONLY)
next_row(8)

# === Recently played ==============================================================================================
section("Recently played")
table("Recently played", at(0, 24, 12),
      f"""SELECT played_at AS "Played", COALESCE(album_image, {BLANK}) AS "Cover", track_name AS "Song",
                 array_to_string(artist_names, ', ') AS "Artists", album_name AS "Album", device AS "Device",
                 context_type AS "From", source AS "Source"
          FROM v_plays WHERE {IN_RANGE} ORDER BY played_at DESC LIMIT 100""",
      images=("Cover",),
      overrides=[by_name("Played", ("unit", "time: YYYY-MM-DD HH:mm"), ("custom.width", 140)),
                 *[by_name(name, ("custom.width", 90)) for name in ("Device", "From", "Source")]],
      description="Played = when the song ended. From = album, playlist, artist page, ... (recorded plays only). "
                  "Device: data exports only. Source: api = recorded by the app, export = imported from a Spotify "
                  "data export.")
next_row(12)

out = Path(sys.argv[1])
dashboard = {
    "uid": "spotify-listening",
    "title": "Spotify listening",
    "description": "Every song played on Spotify: recorded by the playlist generator, plus imported data exports "
                   "(github.com/QuinnBast/SpotifyPlaylistGenerator).",
    "tags": ["spotify", "music"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "5m",
    "schemaVersion": 39,
    "time": {"from": "now-30d", "to": "now"},
    "templating": {"list": [
        {"name": "bucket", "label": "Group by", "type": "custom", "query": "day,week,month,year",
         "current": {"text": "day", "value": "day"},
         "options": [{"text": v, "value": v, "selected": v == "day"} for v in ("day", "week", "month", "year")]},
    ]},
    "panels": panels,
}
json.dump(dashboard, open(out / "spotify-listening.json", "w", newline="\n"), indent=2)
print(f"spotify-listening: {len(panels)} panels")
