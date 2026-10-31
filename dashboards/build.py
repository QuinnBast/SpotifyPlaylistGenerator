"""Generates spotify-listening.json (Grafana, over the listening history Postgres - see listening_history.py).

    python dashboards/build.py dashboards

Edit this, not the JSON (which it overwrites).
"""
import json
import sys
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "spotify-history"}
# The local time zone, as the app set it from its TZ (see migrations/001_listening_history.sql).
TZ = "(SELECT timezone FROM settings)"
GREEN = "#1db954"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

panels = []
_id = [0]


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

# --- Row: headline numbers --------------------------------------------------------------------------------------
stat("Hours listened", (0, 0, 4, 4),
     f"SELECT COALESCE(round(sum(seconds) / 3600.0, 1), 0) FROM v_plays WHERE {IN_RANGE}", unit="suffix: h",
     decimals=1, description="Data-export plays count the time actually played; recorded plays count the song's "
                             "length (Spotify only lists plays of 30 s or more).")
stat("Plays", (4, 0, 3, 4), f"SELECT count(*) FROM v_plays WHERE {IN_RANGE}")
stat("Songs", (7, 0, 3, 4), f"SELECT count(DISTINCT ({SONG})) FROM v_plays WHERE {IN_RANGE}",
     description="Different songs played.")
stat("Artists", (10, 0, 3, 4), f"SELECT count(DISTINCT lower(artist)) FROM v_plays WHERE {IN_RANGE}",
     description="Different main artists played.")
stat("Favourite artist", (13, 0, 5, 4),
     f"""SELECT COALESCE((SELECT max(artist) || ' · ' || count(*) || ' plays' FROM v_plays WHERE {IN_RANGE}
                         GROUP BY lower(artist) ORDER BY count(*) DESC LIMIT 1), '-')""", text=True)
stat("Most played song", (18, 0, 6, 4),
     f"""SELECT COALESCE((SELECT max(track_name) || ' – ' || max(artist) || ' · ' || count(*) || ' plays'
                         FROM v_plays WHERE {IN_RANGE} GROUP BY {SONG} ORDER BY count(*) DESC LIMIT 1), '-')""",
     text=True)

# --- Row: over time ---------------------------------------------------------------------------------------------
panel("timeseries", "Hours per $bucket", (0, 4, 24, 8),
      f"""SELECT date_trunc('$bucket', local_time) AT TIME ZONE {TZ} AS time, round(sum(seconds) / 3600.0, 2) AS "Hours"
          FROM v_plays WHERE {IN_RANGE} GROUP BY 1 ORDER BY 1""",
      fmt="time_series",
      fieldConfig={"defaults": {"unit": "suffix: h", "decimals": 1, "min": 0,
                                "color": {"mode": "fixed", "fixedColor": GREEN},
                                "custom": {"drawStyle": "bars", "barAlignment": -1, "barWidthFactor": 0.8,
                                           "fillOpacity": 85, "lineWidth": 1, "showPoints": "never",
                                           "axisSoftMin": 0, "gradientMode": "none"}}, "overrides": []},
      options={"legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}})

# --- Row: when (a play counts in the hour it ended) ---------------------------------------------------------------
GITHUB_GREEN = {"mode": "opacity", "fill": "#39d353", "scale": "exponential", "exponent": 0.6, "min": 0,
                "steps": 64}
day_columns = ", ".join(
    f"""round(COALESCE(sum(p.seconds) FILTER (WHERE EXTRACT(ISODOW FROM p.local_time) = {i + 1}), 0) / 3600.0, 2)
        AS "{d}\"""" for i, d in reversed(list(enumerate(WEEKDAYS))))
panel("heatmap", "Days listened", (0, 12, 24, 7),
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

hour_columns = ", ".join(
    f"""round(COALESCE(sum(p.seconds) FILTER (WHERE EXTRACT(HOUR FROM p.local_time) = {h}), 0) / 3600.0, 1)
        AS "{h:02d}\"""" for h in range(24))
weekday_values = ", ".join(f"({i + 1}, '{d}')" for i, d in enumerate(WEEKDAYS))
panel("table", "Hours by weekday × hour of day", (0, 19, 24, 9),
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
                   "overrides": [{"matcher": {"id": "byName", "options": "Day"},
                                  "properties": [{"id": "custom.width", "value": 70},
                                                 {"id": "custom.cellOptions", "value": {"type": "auto"}},
                                                 {"id": "custom.align", "value": "left"}]}]},
      options={"showHeader": True, "cellHeight": "md", "footer": {"show": False}})

# --- Row: favourites ----------------------------------------------------------------------------------------------
def plays_bar(name="Plays"):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic",
                                                                   "valueDisplayMode": "text"}},
                           {"id": "color", "value": {"mode": "fixed", "fixedColor": GREEN}}]}


HOURS = {"matcher": {"id": "byName", "options": "Hours"},
         "properties": [{"id": "unit", "value": "suffix: h"}, {"id": "decimals", "value": 1},
                        {"id": "custom.width", "value": 70}]}
top_fields = {"defaults": {"unit": "none"}, "overrides": [plays_bar(), HOURS]}
panel("table", "Top artists", (0, 28, 8, 13),
      f"""SELECT max(artist) AS "Artist", count(*) AS "Plays", round(sum(seconds) / 3600.0, 1) AS "Hours",
                 count(DISTINCT lower(track_name)) AS "Songs"
          FROM v_plays WHERE {IN_RANGE} GROUP BY lower(artist) ORDER BY count(*) DESC LIMIT 50""",
      fieldConfig=top_fields, options={"showHeader": True, "cellHeight": "sm"})
panel("table", "Top songs", (8, 28, 8, 13),
      f"""SELECT max(track_name) AS "Song", max(artist) AS "Artist", count(*) AS "Plays",
                 round(sum(seconds) / 3600.0, 1) AS "Hours"
          FROM v_plays WHERE {IN_RANGE} GROUP BY {SONG} ORDER BY count(*) DESC LIMIT 50""",
      fieldConfig=top_fields, options={"showHeader": True, "cellHeight": "sm"})
panel("table", "Top albums", (16, 28, 8, 13),
      f"""SELECT max(album_name) AS "Album", max(artist) AS "Artist", count(*) AS "Plays",
                 round(sum(seconds) / 3600.0, 1) AS "Hours"
          FROM v_plays WHERE {IN_RANGE} AND album_name IS NOT NULL
          GROUP BY lower(artist), lower(album_name) ORDER BY count(*) DESC LIMIT 50""",
      description="The basic data export has no albums, so its plays aren't counted here.",
      fieldConfig=top_fields, options={"showHeader": True, "cellHeight": "sm"})

panel("table", "Recently played", (0, 41, 24, 10),
      f"""SELECT played_at AS "Played", track_name AS "Song", array_to_string(artist_names, ', ') AS "Artists",
                 album_name AS "Album", context_type AS "From", source AS "Source"
          FROM v_plays WHERE {IN_RANGE} ORDER BY played_at DESC LIMIT 100""",
      description="Played = when the song ended. From = album, playlist, artist page, ... (recorded plays only). "
                  "Source: api = recorded by the app, export = imported from a Spotify data export.",
      fieldConfig={"defaults": {"unit": "none"}, "overrides": [
          {"matcher": {"id": "byName", "options": "Played"},
           "properties": [{"id": "unit", "value": "time: YYYY-MM-DD HH:mm"}, {"id": "custom.width", "value": 140}]},
          *[{"matcher": {"id": "byName", "options": name}, "properties": [{"id": "custom.width", "value": 80}]}
            for name in ("From", "Source")]]},
      options={"showHeader": True, "cellHeight": "sm"})

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
        {"name": "bucket", "label": "Group by", "type": "custom", "query": "day,week,month",
         "current": {"text": "day", "value": "day"},
         "options": [{"text": v, "value": v, "selected": v == "day"} for v in ("day", "week", "month")]},
    ]},
    "panels": panels,
}
json.dump(dashboard, open(out / "spotify-listening.json", "w", newline="\n"), indent=2)
print(f"spotify-listening: {len(panels)} panels")
