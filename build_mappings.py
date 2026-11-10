"""Build the database and its schema mappings. Run once, offline, after data/fetch_data.py and before app.py.

1. Load setlist.fm setlists and the Billboard Hot 100 into data/concerts.duckdb, cleaned:
   real DATEs, tape/intro tracks dropped, guests and "snippet"/"acoustic" notes kept.
2. Label each act's role that night (headliner, support, festival, cameo), so a halftime anthem
   or a festival slot isn't read as a concert.
3. Match every song played to the Hot 100 (title + artist, normalized), and every artist to the
   Billboard credits they appear in ("Taylor Swift Featuring Kendrick Lamar").
4. Precompute the venue ladder: who went from clubs to headlining arenas, and how fast.
5. Describe every column (hand-written, the model fills any gaps) with enum values, and write
   data/concerts_mappings.json, the context app.py retrieves.

    uv run build_mappings.py
"""

import json
from datetime import datetime
from pathlib import Path

import duckdb
import litellm

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "concerts.duckdb"
MAPPINGS_PATH = DATA / "concerts_mappings.json"
MAX_ENUM_VALUES = 30
MIN_CLIMB_DAYS = 180  # a "climb" needs months between the club show and the arena headline

# --- 0. Flatten setlist.fm JSON into rows DuckDB can read ---


def flatten_setlists() -> tuple[list[dict], list[dict]]:
    shows, songs, seen = [], [], set()
    for page in sorted((RAW / "setlists").glob("*.json")):
        for s in json.loads(page.read_text()).get("setlist", []):
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            position = 0
            for set_ in s.get("sets", {}).get("set", []) or []:
                for song in set_.get("song", []) or []:
                    if song.get("tape") or not song.get("name"):
                        continue  # recorded intros/outros aren't performances
                    position += 1
                    songs.append({
                        "show_id": s["id"],
                        "position": position,
                        "song_name": song["name"],
                        "is_encore": bool(set_.get("encore")),
                        "is_cover": "cover" in song,
                        "original_artist": song.get("cover", {}).get("name"),
                        "guest": song.get("with", {}).get("name"),
                        "note": song.get("info"),
                    })
            shows.append({
                "show_id": s["id"],
                "show_date": datetime.strptime(s["eventDate"], "%d-%m-%Y").date().isoformat(),
                "venue_id": s["venue"]["id"],
                "artist_mbid": s["artist"]["mbid"],
                "artist_name": s["artist"]["name"],
                "tour_name": s.get("tour", {}).get("name"),
                "n_songs": position,
                "show_note": s.get("info"),
                "setlistfm_url": s.get("url"),
            })
    return shows, songs


shows, songs = flatten_setlists()
(RAW / "shows.jsonl").write_text("\n".join(json.dumps(r) for r in shows))
(RAW / "songs.jsonl").write_text("\n".join(json.dumps(r) for r in songs))

# --- 1. The database ---

DB_PATH.unlink(missing_ok=True)
db = duckdb.connect(str(DB_PATH))
db.execute(f"""
    -- One normalization for every title and name we match: lowercase, no accents, no "(Live)" or
    -- "(feat. X)", & -> and, no punctuation. 'drivers license' = 'Drivers License (Live)'.
    CREATE MACRO norm(s) AS trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
        lower(strip_accents(s)), '\\s*[\\(\\[].*?[\\)\\]]', '', 'g'), '&', ' and ', 'g'),
        '[^a-z0-9 ]', '', 'g'), '\\s+', ' ', 'g'));

    CREATE TABLE venues AS
    SELECT setlistfm_id AS venue_id, venue_name, coalesce(aliases, '') AS aliases, borough, capacity::INTEGER AS capacity,
           CASE WHEN capacity::INTEGER <= 2000 THEN 'club'
                WHEN capacity::INTEGER < 13000 THEN 'mid-size'
                ELSE 'arena/stadium' END AS size_tier
    FROM read_csv('{DATA / "venues.csv"}', header = true, all_varchar = true)
    WHERE setlistfm_id IS NOT NULL;

    CREATE TABLE _shows AS
    SELECT * REPLACE (show_date::DATE AS show_date)
    FROM read_json('{RAW / "shows.jsonl"}', format = 'newline_delimited');

    -- Billboard Hot 100, every week since 1958
    CREATE TABLE hot100_weekly AS
    SELECT chart_week, current_week AS chart_rank, title, performer
    FROM read_csv('{RAW / "hot-100.csv"}', nullstr = 'NA');

    CREATE TABLE chart_songs AS
    SELECT title, performer, min(chart_rank) AS peak_position, count(*) AS weeks_on_chart,
           min(chart_week) AS first_chart_week, max(chart_week) AS last_chart_week,
           count(*) FILTER (WHERE chart_rank = 1) AS weeks_at_no1
    FROM hot100_weekly GROUP BY title, performer;
""")

# --- 2. Each act's role that night ---

db.execute("""
    CREATE TABLE shows AS
    WITH night AS (
        SELECT s.*, v.size_tier,
               count(*) OVER w AS acts_that_night,
               row_number() OVER (PARTITION BY s.venue_id, s.show_date ORDER BY s.n_songs DESC) AS song_rank
        FROM _shows s JOIN venues v USING (venue_id)
        WINDOW w AS (PARTITION BY s.venue_id, s.show_date)
    )
    SELECT show_id, show_date, venue_id, artist_mbid, artist_name, tour_name, n_songs,
           CASE WHEN acts_that_night >= 6 THEN 'festival'
                WHEN n_songs BETWEEN 1 AND 6 AND size_tier = 'arena/stadium' AND acts_that_night = 1 THEN 'cameo'
                WHEN n_songs = 0 THEN 'unknown'
                WHEN song_rank = 1 THEN 'headliner'
                ELSE 'support' END AS role,
           acts_that_night, show_note, setlistfm_url
    FROM night;
    DROP TABLE _shows;
""")

# --- 3. Match artists and songs to the Hot 100 ---

db.execute(f"""
    -- Every name credited on a Billboard entry, plus the whole credit string (so bands with '&'
    -- like 'Simon & Garfunkel' still match).
    CREATE TABLE _credits AS
    SELECT DISTINCT performer, norm(c) AS name_key
    FROM (SELECT DISTINCT performer FROM chart_songs),
         unnest(list_append(regexp_split_to_array(performer,
             '\\s+(Featuring|Feat\\.|With|X|x|&|And|Duet With|Vs\\.)\\s+|,\\s+'), performer)) t(c);

    CREATE TABLE artist_chart_credits AS
    SELECT DISTINCT a.artist_mbid, a.artist_name, c.performer
    FROM (SELECT DISTINCT artist_mbid, artist_name FROM shows) a
    JOIN _credits c ON c.name_key = norm(a.artist_name);

    CREATE TABLE songs_played AS
    WITH p AS (
        SELECT sp.*, s.artist_name, s.show_date,
               CASE WHEN sp.is_cover THEN sp.original_artist ELSE s.artist_name END AS credited_artist
        FROM read_json('{RAW / "songs.jsonl"}', format = 'newline_delimited') sp JOIN shows s USING (show_id)
    ),
    candidates AS (  -- Billboard songs by the credited artist, keyed by normalized title
        SELECT DISTINCT norm(c.name_key) AS artist_key, cs.title, cs.performer, norm(cs.title) AS title_key,
               cs.peak_position, cs.first_chart_week, cs.last_chart_week
        FROM _credits c JOIN chart_songs cs USING (performer)
    ),
    matched AS (
        SELECT p.show_id, p.position, any_value(c.title ORDER BY c.peak_position) AS chart_title,
               any_value(c.performer ORDER BY c.peak_position) AS chart_performer,
               min(c.peak_position) AS chart_peak,
               any_value(c.first_chart_week ORDER BY c.peak_position) AS chart_first_week,
               any_value(c.last_chart_week ORDER BY c.peak_position) AS chart_last_week
        FROM p JOIN candidates c ON c.artist_key = norm(p.credited_artist) AND c.title_key = norm(p.song_name)
        GROUP BY ALL
    )
    SELECT p.show_id, p.position, p.song_name, p.is_encore, p.is_cover, p.original_artist, p.guest, p.note,
           -- The song itself was cut short. Not "teased <other song>", "extended intro" or "shortened".
           coalesce(regexp_matches(lower(p.note),
               '^\\W*(snippet|excerpt|tease)\\b|\\bpartial\\b|\\b(only|just)\\b[^,;]*\\b(intro|verse|chorus|snippet|first|beginning|part)\\b|\\b(verse|chorus|intro|half) only\\b|\\bsnippet\\W*$'),
               false) AS is_partial,
           m.chart_title, m.chart_performer, m.chart_peak, m.chart_first_week, m.chart_last_week,
           m.chart_title IS NOT NULL AND NOT p.is_cover AS is_own_hit
    FROM p LEFT JOIN matched m USING (show_id, position)
    ORDER BY p.show_id, p.position;
    DROP TABLE _credits;
""")

# --- 4. The venue ladder ---

db.execute(f"""
    CREATE TABLE artist_ladder AS
    WITH a AS (
        SELECT s.artist_mbid, any_value(s.artist_name) AS artist_name,
               arg_min(v.venue_name, s.show_date) FILTER (WHERE v.size_tier = 'club' AND s.role IN ('headliner', 'support')) AS first_club_venue,
               min(s.show_date) FILTER (WHERE v.size_tier = 'club' AND s.role IN ('headliner', 'support')) AS first_club_date,
               arg_min(s.role, s.show_date) FILTER (WHERE v.size_tier = 'club' AND s.role IN ('headliner', 'support')) AS first_club_role,
               arg_min(v.venue_name, s.show_date) FILTER (WHERE v.size_tier = 'arena/stadium' AND s.role = 'headliner') AS first_arena_venue,
               min(s.show_date) FILTER (WHERE v.size_tier = 'arena/stadium' AND s.role = 'headliner') AS first_arena_headline_date
        FROM shows s JOIN venues v USING (venue_id)
        GROUP BY s.artist_mbid
    )
    SELECT a.*, date_diff('day', first_club_date, first_arena_headline_date) AS days_to_arena,
           (SELECT count(DISTINCT cs.title) FROM artist_chart_credits acc
             JOIN chart_songs cs USING (performer)
            WHERE acc.artist_mbid = a.artist_mbid
              AND cs.first_chart_week BETWEEN a.first_club_date AND a.first_arena_headline_date) AS hits_in_between
    FROM a
    WHERE first_club_date IS NOT NULL AND first_arena_headline_date IS NOT NULL
      AND date_diff('day', first_club_date, first_arena_headline_date) >= {MIN_CLIMB_DAYS};
""")

# --- 4b. Run nights: every headline night, with how surprising it was ---

db.execute("""
    CREATE TABLE run_nights AS
    WITH base AS (
        SELECT s.show_id, s.artist_mbid, s.artist_name, s.venue_id, v.venue_name, s.show_date, s.tour_name
        FROM shows s JOIN venues v USING (venue_id)
        WHERE s.role = 'headliner' AND s.n_songs > 0
    ),
    flags AS (  -- per show: songs with a guest, and songs a fan marked as a debut or surprise
        SELECT b.*, coalesce(g.guest_songs, 0) AS guest_songs, coalesce(g.debut_songs, 0) AS debut_songs
        FROM base b LEFT JOIN (
            SELECT show_id,
                   count(*) FILTER (WHERE guest IS NOT NULL) AS guest_songs,
                   count(*) FILTER (WHERE note ILIKE '%debut%' OR note ILIKE '%surprise%'
                                       OR note ILIKE '%first performance%' OR note ILIKE '%first time%'
                                       OR note ILIKE '%mashup%' OR note ILIKE '%mash-up%') AS debut_songs
            FROM songs_played GROUP BY show_id
        ) g USING (show_id)
    ),
    brk AS (  -- a new run starts when the previous show by this act at this venue was not the day before
        SELECT *, CASE WHEN lag(show_date) OVER w IS NULL
                         OR date_diff('day', lag(show_date) OVER w, show_date) > 1 THEN 1 ELSE 0 END AS is_new_run
        FROM flags WINDOW w AS (PARTITION BY artist_mbid, venue_id ORDER BY show_date)
    ),
    runs AS (
        SELECT *, sum(is_new_run) OVER (PARTITION BY artist_mbid, venue_id ORDER BY show_date
                                        ROWS UNBOUNDED PRECEDING) AS run_id
        FROM brk
    ),
    numbered AS (
        SELECT *, count(*) OVER r AS run_size, row_number() OVER (r ORDER BY show_date) AS night_no,
               min(show_date) OVER r AS run_start
        FROM runs WINDOW r AS (PARTITION BY artist_mbid, venue_id, run_id)
    ),
    song_nights AS (  -- on how many nights of the run was each song played?
        SELECT n.artist_mbid, n.venue_id, n.run_id, sp.song_name, count(DISTINCT n.show_id) AS nights_played
        FROM numbered n JOIN songs_played sp USING (show_id) GROUP BY ALL
    ),
    one_night AS (
        SELECT n.show_id, count(*) AS one_night_songs
        FROM numbered n JOIN songs_played sp USING (show_id)
        JOIN song_nights sn ON sn.artist_mbid = n.artist_mbid AND sn.venue_id = n.venue_id
                           AND sn.run_id = n.run_id AND sn.song_name = sp.song_name
        WHERE sn.nights_played = 1 GROUP BY n.show_id
    )
    SELECT n.show_id, n.artist_mbid, n.artist_name, n.venue_name, n.show_date, n.tour_name,
           n.run_start, n.run_size, n.night_no, n.guest_songs, n.debut_songs,
           CASE WHEN n.run_size >= 2 THEN coalesce(o.one_night_songs, 0) END AS one_night_songs
    FROM numbered n LEFT JOIN one_night o USING (show_id)
    ORDER BY n.artist_name, n.show_date;
""")

# --- 5. Describe each column ---

DOCS = {
    "venues": ("One NYC-area venue we track (21 total: 20 in NYC plus MetLife Stadium in East Rutherford, NJ).", {
        "venue_id": "setlist.fm venue id. Join key to shows.venue_id.",
        "aliases": "Nicknames people use, semicolon-separated ('MSG; the Garden').",
        "capacity": "Approximate capacity, hand-entered. Not attendance.",
        "size_tier": "club (<=2,000), mid-size, or arena/stadium (>=13,000). Used for the venue ladder.",
    }),
    "shows": ("One act's performance at one venue on one date, from setlist.fm (2022-2025). Openers and festival acts get their own rows.", {
        "show_id": "setlist.fm setlist id. Join key to songs_played.show_id.",
        "show_date": "Date of the show. No start time is available.",
        "artist_mbid": "MusicBrainz artist id. Filter on this, not artist_name.",
        "artist_name": "Artist name as on setlist.fm. Use find_names to get the exact spelling.",
        "n_songs": "Songs performed live (tape intros excluded). 0 means fans never logged the setlist.",
        "role": "headliner, support, festival (6+ acts logged that night: festivals, benefit and tribute shows), cameo (the only act logged at an arena/stadium that night, 1-6 songs: anthems, halftime sets), or unknown (no setlist).",
        "acts_that_night": "How many acts setlist.fm logged at this venue on this date.",
        "show_note": "Free-text note from the fan who logged it, e.g. 'Approximate order'.",
    }),
    "songs_played": ("One song performed at one show, in order. Tape intros are excluded.", {
        "position": "1-based order in the show, across all sets.",
        "is_cover": "True if the artist played someone else's song. original_artist says whose.",
        "guest": "A guest who joined for this song (surprise guests), else NULL.",
        "note": "Fan note on the song, e.g. 'Acoustic', 'snippet', 'with string section'.",
        "is_partial": "True when the note says snippet/tease/intro/partial. Exclude these when counting 'played the hit'.",
        "chart_title": "The Hot 100 title this song matched, or NULL if it never charted for the credited artist.",
        "chart_performer": "The Billboard credit string of the matched song.",
        "chart_peak": "Best Hot 100 position of the matched song (1 = #1).",
        "chart_first_week": "First week the matched song was on the Hot 100.",
        "chart_last_week": "Last week the matched song was on the Hot 100.",
        "is_own_hit": "True if the artist played their OWN Hot 100 song (not a cover).",
    }),
    "chart_songs": ("One Billboard Hot 100 song (title + credit string), 1958 to now.", {
        "performer": "Billboard credit string, e.g. 'Taylor Swift Featuring Kendrick Lamar'. Use artist_chart_credits to find an artist's songs.",
        "peak_position": "Best position reached (1 = #1).",
        "weeks_on_chart": "Total weeks on the Hot 100.",
        "weeks_at_no1": "Weeks at #1.",
    }),
    "hot100_weekly": ("Every Hot 100 chart, one row per song per week, 1958 to now.", {
        "chart_week": "Chart date.",
        "chart_rank": "Position that week (1-100).",
    }),
    "artist_chart_credits": ("Links each setlist.fm artist to every Billboard credit string they appear in. Join to chart_songs on performer.", {}),
    "artist_ladder": (f"Precomputed: artists who played a club (headliner or support) and later HEADLINED an arena/stadium in NYC, at least {MIN_CLIMB_DAYS} days apart. Festival sets and cameos don't count.", {
        "days_to_arena": "Days from the first club show to the first arena headline.",
        "hits_in_between": "Hot 100 songs by the artist that first charted between those two dates.",
    }),
    "run_nights": ("Precomputed, for picking a night: one row per HEADLINE show that has a setlist. A run is consecutive nights by one act at one venue (a one-night show is a run of 1).", {
        "run_start": "Date of the first night of this run.",
        "run_size": "How many nights are in the run (1 = a single show).",
        "night_no": "Which night of the run this is (1 = first).",
        "guest_songs": "Songs on this night where setlist.fm lists a guest.",
        "debut_songs": "Songs whose fan note says debut, surprise, first performance, first time or mashup.",
        "one_night_songs": "Songs played on this night and no other night of the run. NULL when run_size = 1.",
    }),
}
NOTES = [
    "Filter artists by artist_mbid from find_names, never by artist_name = '...'.",
    "Count a 'concert' only where shows.role = 'headliner' unless the user asks about openers or festivals.",
    "Exclude is_partial songs when counting whether a song was played.",
    "A 'hit' means a Hot 100 song (songs_played.chart_title IS NOT NULL). Billboard is US-only and singles-only.",
    "To list an artist's hits they did NOT play: artist_chart_credits -> chart_songs, anti-join songs_played.",
    "The Hot 100 runs past 2025 but setlists stop at 2025: only count songs that first charted before the show date.",
    "PICK A NIGHT: for 'which night should I go to' or 'best night' questions, query run_nights and show guest_songs, debut_songs and one_night_songs side by side for each night, with no single score. Always also say: (1) venue context, the share of run_nights rows at that venue with guest_songs > 0, e.g. 'MetLife shows had a guest 47% of the time'; (2) how many nights you compared; (3) that last nights are not reliably more surprising than other nights; (4) that the data cannot see future lineups, so any odds are only how often it happened before.",
]
LIMITS = [
    "Only 21 NYC-area venues, 2022-2025 (20 in NYC plus MetLife Stadium in New Jersey). No other cities, years, or venues (no UBS Arena, Prudential Center).",
    "No ticket prices, attendance, start times, reviews, or streaming numbers.",
    "setlist.fm is fan-logged: about a third of shows have no setlist and order is sometimes approximate.",
    "Song matching is by normalized title + artist; medleys and retitled songs can be missed.",
    "Guests and debuts come from fan notes, so an unlogged surprise counts as zero. The data only has past shows: it can say how often surprises happened before, never what a future night will have.",
]

tables = []
for (table,) in db.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall():
    description, col_docs = DOCS.get(table, ("", {}))
    columns = []
    for name, dtype, *_ in db.execute(f"DESCRIBE {table}").fetchall():
        column = {"name": name, "type": dtype, "description": col_docs.get(name, "")}
        if dtype == "VARCHAR" and not name.endswith(("_id", "_mbid", "_url", "_name", "_note", "note", "title", "performer", "guest", "_venue", "artist")):
            values = f"SELECT DISTINCT {name} AS v FROM {table} WHERE {name} IS NOT NULL"
            if db.execute(f"SELECT count(*) FROM ({values})").fetchone()[0] <= MAX_ENUM_VALUES:
                column["enum_values"] = [r[0] for r in db.execute(f"{values} ORDER BY v").fetchall()]
        columns.append(column)
    rows = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    tables.append({"table": table, "description": description, "row_count": rows, "columns": columns})
db.close()

# The model writes a description for any column we didn't document by hand.
missing = [{"table": t["table"], "column": c["name"], "type": c["type"]} for t in tables for c in t["columns"] if not c["description"]]
if missing:
    response = litellm.completion(
        model="vertex_ai/gemini-3.5-flash-lite",
        vertex_location="global",
        messages=[{"role": "user", "content": (
            "These columns are from a database of NYC concert setlists (setlist.fm) joined to the Billboard "
            "Hot 100. Write a concise one-sentence description of each. Return a JSON object mapping "
            "'table.column' to its description.\n\n" + json.dumps(missing))}],
        response_format={"type": "json_object"},
    )
    found = json.loads(response.choices[0].message.content)
    for t in tables:
        for c in t["columns"]:
            c["description"] = c["description"] or found.get(f"{t['table']}.{c['name']}", "")

MAPPINGS_PATH.write_text(json.dumps({"tables": tables, "notes": NOTES, "limits": LIMITS}, indent=2, default=str))
for t in tables:
    print(f"  {t['table']:22} {t['row_count']:>9,} rows")
print(f"Built {DB_PATH.name} ({DB_PATH.stat().st_size / 1e6:.1f} MB) and {MAPPINGS_PATH.name}")
