# Admit One, Informed

## NYC Setlist Analyst: does the show play the hits?

### Sathya Raman (sr4213) and Nameera Faisal Akhtar (nf2613)

We're two seasoned concert goers, and we know being a fan is a full-time job: the setlist homework, the hunt for which night has the surprise guest, the gamble of spending real money on a single night.

Every time we buy concert tickets, the same question comes up: will they actually play the songs we came for? This agent answers it with data. It knows every logged setlist at 21 NYC-area venues (20 in NYC plus MetLife Stadium in New Jersey) from 2022 to 2025 (about 10,400 shows, from setlist.fm), matched against every Billboard Hot 100 chart since 1958. It also tracks which artists went from playing tiny clubs to headlining arenas.

It's for fans deciding which night of a run to go to, and for anyone who wants to settle an argument about who's on the rise.

How to use: ask about an artist, a venue or a song, e.g. which hits someone skips, who brought out guests, which night of a run to go to, or who went from Bowery Ballroom to the Garden. Every answer shows the SQL behind it.

Live: https://agentic-project-02-git-948884183586.europe-west1.run.app · Path: `text2sql`

## Data

- **setlist.fm API.** 10,359 shows by 5,463 artists. 6,967 of them have setlists, which adds up to 99,101 songs in order, with encores, covers, guests and fan notes like "snippet". Pulled per venue and year from `/search/setlists`.
- **Billboard Hot 100** from [utdata/rwd-billboard-data](https://github.com/utdata/rwd-billboard-data). 356,200 weekly chart rows: every song's peak, weeks on the chart, and first and last week.
- **`data/venues.csv`.** The 21 venues (20 in NYC plus MetLife Stadium in East Rutherford, NJ) with capacities and nicknames ("MSG", "the Garden"), entered by hand.

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
- **Precomputes `run_nights`** for picking a night. It has one row per headline show with a setlist, grouped into runs of consecutive nights at one venue, with three side-by-side counts: guest songs, debut or surprise songs (from fan notes like "tour debut"), and songs played on only one night of the run. There is no combined score, because any weighting would be arbitrary. Venue context ("MetLife shows had a guest 47% of the time, MSG 40%") comes from the same table.
- **Writes `data/concerts_mappings.json`.** It has column descriptions, enum values, usage notes and the data's limits. The agent reads this first.

## Tools

1. **`read_mappings()`**: returns every table and column with its description, plus usage notes and what the data can't answer. The agent calls it first.
2. **`get_enum_values(table, column)`**: the exact allowed values for a column like `role` or `size_tier`.
3. **`find_names(kind, name)`**: fuzzy lookup for an artist, song or venue, so "renee rapp" finds Reneé Rapp and "msg" finds Madison Square Garden.
4. **`run_sql(sql)`**: runs read-only DuckDB SQL. Guardrails reject matching artists by exact name and dates in setlist.fm's DD-MM-YYYY format.
5. **`make_chart(sql, chart_type, x, y, title)`**: runs a query and draws the result as a chart on the page.

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

## Pick a night

Fans who can only go to one night of a run can compare the nights. The offline `run_nights` table groups each act's consecutive headline nights at a venue into a run, and counts for every night the songs with a guest, the songs a fan marked as a debut or surprise ("tour debut", "live debut"), and the songs played on no other night of the run. The agent shows those three counts side by side with no invented score, adds venue context ("MetLife shows had a guest 47% of the time, MSG 40%"), and says the data only covers past shows: it reports how often surprises happened before, never what a future night will have. Last nights of a run were not reliably better in the data, and it says so.

**I can go to one of Taylor Swift's three MetLife nights. Which one should I pick?**
- Expected, from `run_nights`: May 26 has 3 guest songs (Phoebe Bridgers, Jack Antonoff on "Getaway Car", Ice Spice), 2 debuts and 2 one-night songs ("Getaway Car", "Maroon"). May 27 has 2 guest songs, 2 debuts and 2 one-night songs ("Holy Ground", "False God"). May 28 has 2 guest songs, 1 debut and 2 one-night songs ("Welcome to New York", "Clean"). MetLife headline shows have a guest 47% of the time.
- No tools: picks nights by weather and vibe, puts "Holy Ground" and "False God" on May 28 when the setlists have them on May 27, and names a guest (Aaron Dessner) who isn't logged ([trace](traces/p1_no_tools.json)).
- Agent: the same counts for all three nights, the venue rate, a note that last nights aren't reliably better, and a note that it can't see future lineups ([trace](traces/p1_ours.json)).

## Where a plain run_sql agent gets it wrong

The baseline uses the same model and database, but has only a `run_sql` tool and a one-line prompt. To reproduce: `uv run traces/compare.py t1 t2` (and `p1` for the pick-a-night example above).

**"Which of SZA's Hot 100 hits has she never played at an NYC show?"**
Her last NYC show in the data is December 2023.
- Plain ([trace](traces/t1_plain.json)) lists "Saturn", "BMF" and "Drive", which came out in 2024–25, so she couldn't have played them yet. It also misses her #1 feature on Drake's "Slime You Out".
- Ours ([trace](traces/t1_ours.json)) only counts songs that charted before her last show, includes features, and matches the SQL ground truth.

**"How many times did Madonna perform Causing a Commotion in NYC?"**
- Plain ([trace](traces/t2_plain.json)) says 3 times.
- Ours ([trace](traces/t2_ours.json)) finds the same 3 nights but flags that all of them were a cappella snippets, so she never played the full song. It knows this from the `is_partial` flag built offline.

## What it can't answer

The data has no ticket prices, attendance, start times, reviews or streaming numbers. It only covers the 21 venues (not UBS Arena or the Prudential Center) and only 2022–2025. The agent says this when asked, and asks a follow-up question when a request is vague, like "what was the best concert?"

setlist.fm is logged by fans. About a third of shows have no setlist, and song order is sometimes approximate. Medleys and renamed songs can miss their Hot 100 match.

## Run locally

Needs [uv](https://docs.astral.sh/uv/), the [gcloud CLI](https://cloud.google.com/sdk/docs/install), and a GCP project with billing and the Vertex AI API enabled.

```bash
git clone https://github.com/nameeraf/agentic-project-02.git
cd agentic-project-02
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
uv run app.py
```

Then open http://localhost:8000. `uv run` installs the dependencies on first run. No API key is needed to run the app; the setlist.fm key is only for rebuilding the data.

It's deployed on Cloud Run with continuous deploy from GitHub. The request timeout is 300 s, the agent is capped at 25 turns, and the service account has the Vertex AI User role.

## Files

```
app.py                  agent, tools and server
index.html              the page
build_mappings.py       offline build
data/fetch_data.py      downloads the raw data into data/raw/ (the raw files are committed too)
data/venues.csv         the 21 venues
data/concerts.duckdb    the database
traces/compare.py       generates the traces above
```
