# NYC Setlist Analyst

What actually gets played at NYC concerts, and who climbs from small clubs to arenas?

A Text2SQL agent over about 10,000 shows at 20 NYC venues (2022–2025), with every song in order from setlist.fm, matched to every Billboard Hot 100 chart since 1958. Ask it how many hits an artist plays, which of their hits they skip, who went from Bowery Ballroom to headlining the Garden, which guests showed up, or what gets covered. Every answer shows the queries behind it.

- **Live:** TODO deploy URL
- **Path:** `text2sql`
- **Team:** Nameera Faisal Akhtar (nf2613), Sathya Raman (sr4213)

## Who it's for

- **Fans** deciding which night of a run to go to, or whether an artist still plays the old hits.
- **Music writers and bookers** checking a hunch: who's climbing, who repeats the same set every night.
- **Anyone** who has argued about whether a show "played all the hits."

## Data

| Source | What we use | How it's loaded |
|---|---|---|
| [setlist.fm API](https://api.setlist.fm/docs/1.0/index.html) | 10,179 shows by 5,420 artists at 20 NYC venues, 2022–2025. 6,817 have setlists: 96,532 songs in order, with encores, covers, 2,652 guest appearances and fan notes ("snippet", "acoustic"). | `fetch_data.py` looks up each venue's id, then pages through `/search/setlists?venueId=…&year=…`. Needs a free API key in `.env`. |
| [Billboard Hot 100](https://github.com/utdata/rwd-billboard-data) (utdata/rwd-billboard-data) | 356,200 weekly chart rows, 1958 to now: 32,807 songs with peak, weeks on chart and first/last chart week. | `fetch_data.py` downloads `hot-100-current.csv`. |
| `data/venues.csv` | The 20 venues, capacities and nicknames ("MSG", "the Garden"), hand-entered. | Committed. |

The built database `data/concerts.duckdb` (about 10 MB) is committed, so the app and the Cloud Run deploy don't need the raw downloads. To rebuild from scratch:

```bash
cp .env.example .env        # add SETLISTFM_API_KEY
gcloud auth application-default login
uv run fetch_data.py        # ~10 min, cached and resumable
uv run build_mappings.py
```

## The offline build step (`build_mappings.py`)

The agent writes better SQL because the hard parts are done once, offline:

1. **Clean setlists.** Real DATEs instead of setlist.fm's `DD-MM-YYYY`. Tape intros are dropped. Guests (`guest`) and fan notes (`note`) are kept. `is_partial` flags songs that were only a snippet or partial (but not "teased *another* song" or "extended intro").
2. **Label each act's role that night.** Fans log national-anthem singers and halftime acts at Knicks and Yankees games as "concerts," and festivals log every act. Each show is labeled `headliner`, `support`, `festival` (6+ acts that night), `cameo` (the only act logged at an arena that night, 1–6 songs: Mary J. Blige's anthem, Naughty by Nature at halftime) or `unknown` (no setlist).
3. **Match songs and artists across sources.** Titles and names are normalized (case, accents, `(Live)`, `feat.`, `&`), and Billboard credit strings ("Taylor Swift Featuring Kendrick Lamar") are split so each artist links to every song they're credited on. Covers are matched to the *original* artist's hit. 10,929 songs played in NYC match a Hot 100 entry.
4. **Precompute the venue ladder.** `artist_ladder` holds the 31 artists who played a club (≤2,000 capacity) and later *headlined* an arena or stadium at least 180 days apart, plus how many Hot 100 songs they charted in between. Festival sets and cameos don't count, which stops "Surf Curse: Bowery Ballroom → Citi Field in one day" (that was Governors Ball).
5. **Write `data/concerts_mappings.json`.** Every table and column gets a description (hand-written, with the model filling gaps), enum values, usage notes and the data's limits.

At runtime the agent (`app.py`) has five tools:
- `read_mappings`: the context above.
- `get_enum_values`.
- `find_names`: fuzzy lookup for artists, songs and venues, so "renee rapp" finds *Reneé Rapp* and "msg" finds Madison Square Garden.
- `run_sql`: read-only DuckDB, no file or network access.
- `make_chart`: Vega-Lite, drawn by the page.

Two guardrails reject `artist_name = '…'` (use the id from `find_names`) and `DD-MM-YYYY` dates.

## Sample queries for the grader

Expected answers were checked by hand with SQL. The no-tools column is the same model (`gemini-3.5-flash-lite`) with no tools and no system prompt. Full traces are in [`traces/`](traces/).

| # | Question | Expected answer | Source | No tools, no prompt | Our agent |
|---|---|---|---|---|---|
| 1 | How many times did Billy Joel play Piano Man at Madison Square Garden? | **29.** He headlined 29 MSG shows in 2022–2025 and played it in full at all 29. | `shows` ⋈ `songs_played` ⋈ `venues` | "**150 times**", his career total, not this data ([trace](traces/s1_no_tools.json)) | "29 times … logged as played in full during every single one" ([trace](traces/s1_ours.json)) |
| 2 | Which of Olivia Rodrigo's Hot 100 songs has she never played at an NYC show? | **5:** 1 Step Forward, 3 Steps Back (#19, only inside a medley), So American (#58), Stranger (#75), Scared Of My Guitar (#90), Girl I've Always Been (#99) | `artist_chart_credits` ⋈ `chart_songs`, anti-join `songs_played` | "**Get Him Back!**", which she did play, and it lists "So American" among songs she *has* played ([trace](traces/s2_no_tools.json)) | The same 5, with the medley noted ([trace](traces/s2_ours.json)) |
| 3 | Which artists went from playing NYC clubs to headlining an NYC arena, and who had the most hits in between? | **31 artists.** Most hits in between: **Sabrina Carpenter**, 14 (Webster Hall, Oct 2022 → MSG headline, Sep 2024), then Don Toliver 12, Sleep Token 10 | `artist_ladder` | Blondie, Talking Heads and the Strokes at CBGB in the 1970s–2000s ([trace](traces/s3_no_tools.json)) | 31 artists, led by Sabrina Carpenter with 14, then Don Toliver and Sleep Token ([trace](traces/s3_ours.json)) |

## Where the plain `run_sql` agent fails and ours doesn't

"Plain" is the same model and the same database with only a `run_sql` tool and a one-line prompt: no mappings, no name lookup, no guardrails, no notes. Reproduce with `uv run compare.py t1 t2`.

### 1. "Which of SZA's Hot 100 hits has she never played at an NYC show?"

SZA's last NYC show in the data is December 2023, but the Hot 100 keeps going.

- **Plain** ([trace](traces/t1_plain.json), 20 tool calls): lists "Saturn" (Feb 2024), "BMF", "Scorsese Baby Daddy" and "Drive" (2024–2025) as hits she "never played." She couldn't have; they didn't exist yet. It only searched credits where SZA is the main performer, so it also misses her #1 feature "Slime You Out" (with Drake).
- **Ours** ([trace](traces/t1_ours.json), 6 tool calls): only counts songs charted before her last show and includes features. It lists "Slime You Out" (#1), "What Lovers Do" (#9), "No Love" (#13), "Telekinesis" (#26) and others, matching the SQL ground truth.
- **What made the difference:** the mappings note *"The Hot 100 runs past 2025 but setlists stop at 2025: only count songs that first charted before the show date"* and the `artist_chart_credits` table.

### 2. "How many times did Madonna perform Causing a Commotion in NYC?"

- **Plain** ([trace](traces/t2_plain.json)): "**3 times**", with the three dates.
- **Ours** ([trace](traces/t2_ours.json)): the same three nights, but "all 3 were logged as **partial** performances (a cappella snippets rather than full renditions)." She never played the full song in NYC.
- **What made the difference:** `songs_played.is_partial`, built offline from fan notes, and the rule to exclude partials when counting what was played.

## What the data can't answer

Ticket prices, attendance, start times, reviews and streaming numbers. Venues outside the 20 (MetLife and UBS Arena aren't included) and anything before 2022. The agent says so: ask "How much were tickets for Billy Joel at MSG?" It also asks a clarifying question when a request is vague ("What was the best concert?" → best by hits, length, guests or career leap?).

Known limits: setlist.fm is fan-logged (a third of shows have no setlist; order is sometimes approximate). Song matching is by normalized title and artist, so medleys and retitled songs can be missed. The role labels are heuristics.

## Run locally

```bash
uv sync
gcloud auth application-default login
uv run app.py               # http://localhost:8000
```

## Deploy

Cloud Run with continuous deploy from GitHub. `app.py` reads `PORT` and binds `0.0.0.0` on Cloud Run. Set the request timeout to 300 s; the agent is capped at 25 turns. The service account needs the Vertex AI User role.

## Files

```
app.py                  agent, tools, guardrails and the FastAPI server (/, /chat, /clear)
index.html              the page: chat, tables, charts, tool-call traces
fetch_data.py           downloads setlist.fm and the Hot 100 into data/raw/ (not committed)
build_mappings.py       offline build -> data/concerts.duckdb + data/concerts_mappings.json
compare.py              plain run_sql vs ours vs no-tools -> traces/
data/venues.csv         the 20 venues, hand-entered
traces/                 the README's evidence
```

Concert data from [setlist.fm](https://www.setlist.fm). Hot 100 data from Billboard via [utdata/rwd-billboard-data](https://github.com/utdata/rwd-billboard-data).
