# NYC Setlist Analyst

A Text2SQL agent for questions about what gets played at NYC concerts. It covers about 10,000 shows at 20 NYC venues from 2022 to 2025, with full setlists from setlist.fm, matched against every Billboard Hot 100 chart since 1958.

You can ask which hits an artist plays or skips, who went from small clubs to headlining arenas, who brought out guests, and what gets covered. Every answer shows the SQL behind it.

Live: https://agentic-project-02-git-948884183586.europe-west1.run.app

Path: `text2sql` · Team: Nameera Faisal Akhtar (nf2613), Sathya Raman (sr4213)

## Who it's for

Fans deciding which night of a run to go to, or whether an artist still plays the old songs. Also music writers and bookers who want to check a hunch about who's on the rise.

## Data

- **setlist.fm API.** 10,179 shows by 5,420 artists. 6,817 of them have setlists, which adds up to 96,532 songs in order, with encores, covers, guests and fan notes like "snippet". Pulled per venue and year from `/search/setlists`.
- **Billboard Hot 100** from [utdata/rwd-billboard-data](https://github.com/utdata/rwd-billboard-data). 356,200 weekly chart rows: every song's peak, weeks on the chart, and first and last week.
- **`data/venues.csv`.** The 20 venues with capacities and nicknames ("MSG", "the Garden"), entered by hand.

The built database (`data/concerts.duckdb`, about 10 MB) is committed, so the app doesn't need the raw files. To rebuild it, you need a free setlist.fm API key in `.env`:

```bash
uv run data/fetch_data.py   # about 10 minutes; cached, so it can resume
uv run build_mappings.py
```

## Offline build (`build_mappings.py`)

- **Cleans the setlists.** Converts dates, drops recorded intros, and flags songs that were only a snippet or partial (`is_partial`).
- **Labels each act's role that night:** headliner, support, festival, cameo, or unknown. Fans log national anthems and halftime sets at Knicks and Yankees games as "concerts", so without this a one-song anthem counts as a show.
- **Matches songs and artists to the Hot 100.** Titles and names are normalized, and Billboard credits like "Drake Featuring SZA" are split so each artist gets credit for their features. Covers are matched to the original artist's hit.
- **Precomputes `artist_ladder`.** These are the 31 artists who played a club (2,000 capacity or less) and later headlined an arena at least 180 days later, plus how many Hot 100 songs they charted in between. Festival sets don't count, which removes fake "climbs" like Bowery Ballroom to Citi Field in one day (that was Governors Ball).
- **Writes `data/concerts_mappings.json`.** It has column descriptions, enum values, usage notes and the data's limits. The agent reads this first.

The agent's tools are `read_mappings`, `get_enum_values`, `find_names`, `run_sql` (read-only) and `make_chart`. `find_names` is a fuzzy lookup, so "renee rapp" finds Reneé Rapp. Guardrails reject matching artists by exact name and dates in setlist.fm's DD-MM-YYYY format.

## Sample queries

The answers were checked by hand in SQL. "No tools" means the same model (Gemini 3.5 Flash-Lite) with no tools and no system prompt. All traces are in [`traces/`](traces/).

**1. How many times did Billy Joel play Piano Man at Madison Square Garden?**
- Expected: 29. He headlined 29 MSG shows in 2022–2025 and played it in full at every one. (`shows`, `songs_played`, `venues`)
- No tools: "150 times", his career total ([trace](traces/s1_no_tools.json)).
- Agent: 29, played in full at all 29 shows ([trace](traces/s1_ours.json)).

**2. Which of Olivia Rodrigo's Hot 100 songs has she never played at an NYC show?**
- Expected: five songs. "1 Step Forward, 3 Steps Back" (played only inside a medley), "So American", "Stranger", "Scared Of My Guitar", "Girl I've Always Been". (`artist_chart_credits`, `chart_songs`, `songs_played`)
- No tools: "Get Him Back!", which she did play ([trace](traces/s2_no_tools.json)).
- Agent: the same five, and it notes the medley ([trace](traces/s2_ours.json)).

**3. Which artists went from NYC clubs to headlining an NYC arena, and who had the most hits in between?**
- Expected: 31 artists. Sabrina Carpenter had the most, 14 (Webster Hall in Oct 2022, MSG in Sep 2024), then Don Toliver (12) and Sleep Token (10). (`artist_ladder`)
- No tools: Blondie, Talking Heads and the Strokes at CBGB decades ago ([trace](traces/s3_no_tools.json)).
- Agent: 31 artists, led by Sabrina Carpenter, Don Toliver and Sleep Token ([trace](traces/s3_ours.json)).

## Where a plain run_sql agent gets it wrong

The baseline uses the same model and database, but has only a `run_sql` tool and a one-line prompt. To reproduce: `uv run traces/compare.py t1 t2`.

**"Which of SZA's Hot 100 hits has she never played at an NYC show?"**
Her last NYC show in the data is December 2023.
- Plain ([trace](traces/t1_plain.json)) lists "Saturn", "BMF" and "Drive", which came out in 2024–25, so she couldn't have played them yet. It also misses her #1 feature on Drake's "Slime You Out".
- Ours ([trace](traces/t1_ours.json)) only counts songs that charted before her last show, includes features, and matches the SQL ground truth.

**"How many times did Madonna perform Causing a Commotion in NYC?"**
- Plain ([trace](traces/t2_plain.json)) says 3 times.
- Ours ([trace](traces/t2_ours.json)) finds the same 3 nights but flags that all of them were a cappella snippets, so she never played the full song. It knows this from the `is_partial` flag built offline.

## What it can't answer

The data has no ticket prices, attendance, start times, reviews or streaming numbers. It only covers the 20 venues (not MetLife or UBS Arena) and only 2022–2025. The agent says this when asked, and asks a follow-up question when a request is vague, like "what was the best concert?"

setlist.fm is logged by fans. About a third of shows have no setlist, and song order is sometimes approximate. Medleys and renamed songs can miss their Hot 100 match.

## Running it

```bash
uv sync
gcloud auth application-default login
uv run app.py   # http://localhost:8000
```

It's deployed on Cloud Run with continuous deploy from GitHub. The request timeout is 300 s, the agent is capped at 25 turns, and the service account has the Vertex AI User role.

## Files

```
app.py                  agent, tools and server
index.html              the page
build_mappings.py       offline build
data/fetch_data.py      downloads raw data into data/raw/ (not committed)
data/venues.csv         the 20 venues
data/concerts.duckdb    the database
traces/compare.py       generates the traces above
```
