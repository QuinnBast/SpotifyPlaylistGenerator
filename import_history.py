"""Imports a Spotify data export into the listening history, so the dashboards start with your past listening.

    python import_history.py [--dry-run] [--min-seconds 30] PATH [PATH ...]

PATH: the zip Spotify sent (e.g. my_spotify_data.zip), its unzipped folder, or single JSON files. Request the export
at spotify.com -> Account -> Privacy settings -> Download your data:
  - "Extended streaming history" (preferred): your whole history, Streaming_History_Audio_*.json. Takes up to 30 days.
  - "Account data": only the last year, StreamingHistory_music_*.json (no track ids). Used only when the zip/folder has
    no extended history.

Writes to the app's database: listeningHistory.databaseUrl in config.json (or CONFIG_PATH), or DATABASE_URL. Safe to run again: plays already stored are
skipped. Plays from the moment the app started recording are skipped too, since the app has those already (and so is
the same play recorded by both, a few seconds apart, where the two meet). Only songs
are imported (no podcasts or audiobooks, like the app), and only plays of at least --min-seconds, which is what
Spotify's "recently played" counts as a play. Shorter ones from the extended history are kept separately, only for
the dashboard's skip stats.
"""
import argparse
import json
import logging
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import listening_history as history

EXTENDED = re.compile(r'Streaming_History_Audio_.*\.json$')
# StreamingHistory_music_0.json; older exports used StreamingHistory0.json (songs and podcasts together).
BASIC = re.compile(r'StreamingHistory(_music_)?\d+\.json$')


def read_json_files(paths):
    """(file name, parsed JSON) for every JSON file in the given zips, folders and files."""
    for path in map(Path, paths):
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path) as archive:
                for name in sorted(archive.namelist()):
                    if name.lower().endswith('.json'):
                        yield name, json.loads(archive.read(name).decode('utf-8'))
        elif path.is_dir():
            for file in sorted(path.rglob('*.json')):
                yield str(file), json.loads(file.read_text(encoding='utf-8'))
        else:
            yield str(path), json.loads(path.read_text(encoding='utf-8'))


def play_from_extended(row):
    """A row of the extended streaming history, or None for podcasts, audiobooks and removed tracks."""
    uri = row.get('spotify_track_uri')
    name = row.get('master_metadata_track_name')
    if not uri or not name:
        return None
    track_id = uri.rsplit(':', 1)[-1]
    return {
        'played_at': datetime.fromisoformat(row['ts'].replace('Z', '+00:00')),
        'track_key': track_id, 'track_id': track_id, 'track_name': name,
        'artist_names': [row.get('master_metadata_album_artist_name') or 'Unknown artist'], 'artist_ids': None,
        'album_id': None, 'album_name': row.get('master_metadata_album_album_name'),
        'duration_ms': None, 'ms_played': row.get('ms_played'),
        'context_type': None, 'context_uri': None, 'source': 'export',
        'platform': row.get('platform'), 'country': row.get('conn_country'), 'shuffle': row.get('shuffle'),
        'skipped': row.get('skipped'), 'reason_start': row.get('reason_start'), 'reason_end': row.get('reason_end'),
        'offline': row.get('offline'), 'incognito': row.get('incognito_mode'),
    }


def play_from_basic(row):
    """A row of the account-data streaming history (times are UTC, to the minute)."""
    artist, name = row.get('artistName'), row.get('trackName')
    if not artist or not name:
        return None
    return {
        'played_at': datetime.strptime(row['endTime'], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc),
        'track_key': history.name_key(artist, name), 'track_id': None, 'track_name': name,
        'artist_names': [artist], 'artist_ids': None, 'album_id': None, 'album_name': None,
        'duration_ms': None, 'ms_played': row.get('msPlayed'),
        'context_type': None, 'context_uri': None, 'source': 'export',
    }


def _compact(play):
    """The play without its empty fields, its strings shared with every other play of the same song, artist, device,
    ...: an export has ~100k plays of far fewer songs, and the import has to fit in the app's memory."""
    return {k: sys.intern(v) if isinstance(v, str) else v for k, v in play.items() if v is not None}


def parse_export(paths, min_seconds, counts, short_plays=None):
    """All song plays in the export (extended history if there is any, else the basic one). Tallies what was left
    out in `counts`. Shorter plays from the extended history (which says how each ended) go in `short_plays`, if given,
    for the skip stats. Each file is converted as it's read, so the raw export is never all in memory at once."""
    found = {kind: {'plays': [], 'short': [], 'counts': Counter()} for kind in ('extended', 'basic')}
    for name, data in read_json_files(paths):
        base = os.path.basename(name)
        if EXTENDED.search(base):
            kind, parse = 'extended', play_from_extended
        elif BASIC.search(base):
            kind, parse = 'basic', play_from_basic
        else:
            continue
        counts[f'{kind} files'] += 1
        out = found[kind]
        for row in data:
            play = parse(row)
            if play is None:
                out['counts']['not a song (podcast, audiobook, removed track)'] += 1
            elif (play['ms_played'] or 0) < min_seconds * 1000:
                out['counts'][f'played less than {min_seconds:g} s'] += 1
                if play.get('reason_end'):
                    out['short'].append(_compact(play))
            else:
                out['plays'].append(_compact(play))
        del data

    if counts['extended files'] and counts['basic files']:
        logging.info(f"Using the extended streaming history; ignoring {counts['basic files']} basic history file(s) "
                     "(the same plays, with less detail)")
    chosen = found['extended'] if counts['extended files'] else found['basic']
    counts.update(chosen['counts'])
    if short_plays is not None:
        short_plays.extend(chosen['short'])
    return chosen['plays']


def database_url_from_config():
    """The app's database, the way the app finds it: DATABASE_URL, else listeningHistory.databaseUrl in the config."""
    if os.environ.get('DATABASE_URL'):
        return os.environ['DATABASE_URL']
    config_path = Path(os.environ.get('CONFIG_PATH', 'config.json'))
    if config_path.exists():
        return json.loads(config_path.read_text(encoding='utf-8')).get('listeningHistory', {}).get('databaseUrl')
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('paths', nargs='+', metavar='PATH', help='export zip, folder or JSON file(s)')
    parser.add_argument('--min-seconds', type=float, default=30,
                        help='skip shorter plays (default 30, like Spotify\'s "recently played")')
    parser.add_argument('--dry-run', action='store_true', help='only read the export and show what would be imported')
    args = parser.parse_args(argv)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        logging.error(f"Not found: {', '.join(missing)}")
        return 1

    counts = Counter()
    short_plays = []
    plays = parse_export(args.paths, args.min_seconds, counts, short_plays)
    if not counts['extended files'] and not counts['basic files']:
        logging.error("No streaming history found: expected Streaming_History_Audio_*.json or "
                      "StreamingHistory_music_*.json in the given paths")
        return 1
    if plays:
        logging.info(f"Found {len(plays)} plays, {min(p['played_at'] for p in plays):%Y-%m-%d} to "
                     f"{max(p['played_at'] for p in plays):%Y-%m-%d}")

    if not args.dry_run:
        database_url = database_url_from_config()
        if not database_url:
            logging.error("No database: set listeningHistory.databaseUrl in config.json (or CONFIG_PATH), "
                          "or DATABASE_URL")
            return 1
        with history.connect(database_url) as conn:
            history.migrate(conn)
            recording_since = history.first_api_play(conn)
            if recording_since:
                kept = history.without_plays_from(conn, [p for p in plays if p['played_at'] < recording_since], 'api')
                counts['already recorded by the app (after its first poll)'] += len(plays) - len(kept)
                plays = kept
            added = history.store_plays(conn, plays)
            short_added = history.store_short_plays(conn, short_plays)
        counts['already imported'] += len(plays) - added
        logging.info(f"Imported {added} plays")
        if short_added:
            logging.info(f"Kept {short_added} plays under {args.min_seconds:g} s for the skip stats only")
    else:
        logging.info(f"Dry run: {len(plays)} plays would be imported (minus any already in the database)")

    for reason, n in counts.items():
        if n and not reason.endswith('files'):
            logging.info(f"Skipped {n}: {reason}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
