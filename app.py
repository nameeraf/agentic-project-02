import json
import os
import re
import uuid
from pathlib import Path

import duckdb
import uvicorn
from agents import (
    Agent,
    ModelSettings,
    Runner,
    RunHooks,
    SQLiteSession,
    ToolGuardrailFunctionOutput,
    function_tool,
    set_tracing_disabled,
    tool_input_guardrail,
)
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

MODEL = "vertex_ai/gemini-3.5-flash-lite"
MAX_TURNS = 25  # graders wait up to 5 minutes per answer

# --- The Database ---

# build_mappings.py builds both files once, offline. The app only reads them.
DATA = Path(__file__).parent / "data"
MAX_ROWS = 100

try:
    MAPPINGS = json.loads((DATA / "concerts_mappings.json").read_text())
    # The security boundary is the connection, not the prompt: read-only, no file or network
    # access, and the agent can't SET its way out of either.
    db = duckdb.connect(
        str(DATA / "concerts.duckdb"),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
except (FileNotFoundError, duckdb.Error):
    raise SystemExit("No database found. Run `uv run fetch_data.py` and `uv run build_mappings.py` first.")


def query(sql: str, max_rows: int = MAX_ROWS) -> dict:
    # The SDK runs tools in threads. Each call gets its own cursor: DuckDB connections aren't thread-safe.
    with db.cursor() as cursor:
        try:
            cursor.execute(sql)
            rows = cursor.fetchmany(max_rows + 1)
        except duckdb.Error as e:
            # The model cannot see an exception. Return something it can reason about.
            return {"error": str(e)}
        columns = [column[0] for column in cursor.description or []]
    return {"columns": columns, "rows": rows[:max_rows], "truncated": len(rows) > max_rows}


# --- Tools ---


@function_tool
def read_mappings() -> str:
    """Read every table: what one row is, its row count, and its columns (name, type, description,
    whether it's an enum). Also returns usage notes and what the data can't answer. Call this first.
    """
    return json.dumps({
        "tables": [
            {
                "table": t["table"],
                "description": t["description"],
                "row_count": t["row_count"],
                "columns": [
                    {"name": c["name"], "type": c["type"], "description": c["description"], "is_enum": "enum_values" in c}
                    for c in t["columns"]
                ],
            }
            for t in MAPPINGS["tables"]
        ],
        "notes": MAPPINGS["notes"],
        "limits": MAPPINGS["limits"],
    }, default=str)


@function_tool
def get_enum_values(table: str, column: str) -> str:
    """Get the exact valid values of an enum column. Match them exactly: equality is case-sensitive.

    Args:
        table: The table name, e.g. 'shows'.
        column: The column name, e.g. 'role'.
    """
    t = next((t for t in MAPPINGS["tables"] if t["table"] == table), None)
    match = next((c for c in (t or {}).get("columns", []) if c["name"] == column), None)
    if match is None:
        return json.dumps({"error": f"No column {table}.{column}. Call read_mappings for the columns."})
    if "enum_values" not in match:
        return json.dumps({"error": f"{table}.{column} is not an enum column."})
    return json.dumps({"table": table, "column": column, "enum_values": match["enum_values"]}, default=str)


# kind -> SQL returning (id, name, how often it appears), most likely matches first
_FIND = {
    "artist": """SELECT artist_mbid AS id, artist_name AS name, count(*) AS n_nyc_shows,
                        max(jaro_winkler_similarity(norm(artist_name), norm($1))) AS score
                 FROM shows WHERE norm(artist_name) LIKE '%' || norm($1) || '%'
                    OR jaro_winkler_similarity(norm(artist_name), norm($1)) > 0.88
                 GROUP BY artist_mbid, artist_name ORDER BY score DESC, n_nyc_shows DESC LIMIT 8""",
    "song": """SELECT song_name AS name, count(*) AS times_played,
                      max(jaro_winkler_similarity(norm(song_name), norm($1))) AS score
               FROM songs_played WHERE norm(song_name) LIKE '%' || norm($1) || '%'
               GROUP BY song_name ORDER BY score DESC, times_played DESC LIMIT 8""",
    "venue": """SELECT venue_id AS id, venue_name AS name, size_tier,
                       greatest(jaro_winkler_similarity(norm(venue_name), norm($1)),
                                list_max([jaro_winkler_similarity(norm(a), norm($1)) FOR a IN string_split(aliases, ';')])) AS score
                FROM venues ORDER BY score DESC LIMIT 3""",
}


@function_tool
def find_names(kind: str, name: str) -> str:
    """Look up the exact spelling and id of an artist, song or venue. Names in the data rarely match
    what people type ('olivia rodrigo', 'msg', 'drivers license'). Use the returned id to filter.

    Args:
        kind: 'artist', 'song' or 'venue'.
        name: What the user called it.
    """
    if kind not in _FIND:
        return json.dumps({"error": "kind must be 'artist', 'song' or 'venue'."})
    with db.cursor() as cursor:
        cursor.execute(_FIND[kind], [name])
        columns = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
    if not rows:
        return json.dumps({"matches": [], "hint": "No match. It may not be in the data (only 20 NYC venues, 2022-2025)."})
    return json.dumps({"matches": [dict(zip(columns, r)) for r in rows]}, default=str)


@tool_input_guardrail
def no_artist_name_equality(data) -> ToolGuardrailFunctionOutput:
    """Reject `artist_name = '...'`. Spellings vary ('Tyler, The Creator'), so filter on artist_mbid."""
    sql = json.loads(data.context.tool_arguments or "{}").get("sql", "")
    if re.search(r"artist_name\s*(=|IN\s*\()\s*'", sql, re.IGNORECASE):
        # The model reads this instead of a result, and tries again.
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: don't match artist_name with '='. Call find_names(kind='artist', ...) and filter on artist_mbid."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_input_guardrail
def no_setlistfm_dates(data) -> ToolGuardrailFunctionOutput:
    """setlist.fm writes dates as DD-MM-YYYY. In the database they're real DATEs."""
    sql = json.loads(data.context.tool_arguments or "{}").get("sql", "")
    if re.search(r"'\d{2}-\d{2}-\d{4}'", sql):
        return ToolGuardrailFunctionOutput.reject_content("Rejected: dates are DATE columns. Write DATE 'YYYY-MM-DD'.")
    return ToolGuardrailFunctionOutput.allow()


GUARDRAILS = [no_artist_name_equality, no_setlistfm_dates]


@function_tool(tool_input_guardrails=GUARDRAILS)
def run_sql(sql: str) -> str:
    """Run a DuckDB SQL query against the concerts database and return up to 100 rows.

    Args:
        sql: A DuckDB SQL query.
    """
    return json.dumps(query(sql), default=str)


@function_tool(tool_input_guardrails=GUARDRAILS)
def make_chart(sql: str, chart_type: str, x: str, y: str, title: str, color: str | None = None) -> str:
    """Run a query and show its result to the user as a chart. Use it for rankings, comparisons and trends.

    Args:
        sql: A DuckDB SQL query whose columns include x and y (and color, if given).
        chart_type: 'bar', 'line' or 'point'.
        x: Column for the x axis (the category or date).
        y: Column for the y axis (the number).
        title: A short title that states the finding.
        color: Optional column to color by.
    """
    result = query(sql, max_rows=200)
    if "error" in result:
        return json.dumps(result)
    encoding = {
        "x": {"field": x, "type": "temporal" if chart_type == "line" else "nominal", "sort": None},
        "y": {"field": y, "type": "quantitative"},
    }
    if color:
        encoding["color"] = {"field": color, "type": "nominal"}
    if chart_type == "bar":  # horizontal bars read better with long names
        encoding["x"], encoding["y"] = encoding["y"], {**encoding["x"], "sort": "-x"}
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {"text": title, "anchor": "start", "fontSize": 13},
        "autosize": {"type": "fit-x", "contains": "padding"},
        "data": {"values": [dict(zip(result["columns"], row)) for row in result["rows"]]},
        "mark": {"type": chart_type, "tooltip": True},
        "encoding": encoding,
        "width": "container",
    }
    # The page finds make_chart in tool_calls and draws this Vega-Lite spec.
    return json.dumps({"chart": spec, "rows": len(result["rows"])}, default=str)


# --- The Agent ---

agent = Agent(
    name="NYC Setlist Analyst",
    instructions="""\
You are a music data analyst. You answer questions about what got played at concerts at 20 NYC venues
from 2022 to 2025 (setlist.fm), joined to the Billboard Hot 100 (1958 to now). The data is in DuckDB.

How to work:
- Call read_mappings first. It explains every table, the notes, and the limits.
- Before filtering on an artist, song or venue, call find_names and use the exact id or name it returns.
  If find_names finds several plausible artists, ask which one the user means.
- Before filtering on an enum column (role, size_tier), call get_enum_values.
- A "show" or "concert" means shows.role = 'headliner' unless the user asks about openers or festivals.
- A "hit" is a Hot 100 song. Exclude songs_played.is_partial when counting what was played.
- For club-to-arena questions, start from artist_ladder.
- When the answer is a ranking, comparison or trend, also call make_chart.

Be honest:
- If the data can't answer it (ticket prices, attendance, start times, reviews, streams, other cities,
  venues we don't track, years before 2022), say so plainly and say what the data CAN tell them.
- If a question is vague ("best show?", "biggest artist?"), ask one short clarifying question before querying.
- setlist.fm is fan-logged. Say so when a count could be off because setlists are missing.

Answer in plain English with the key numbers, and say which tables the numbers came from. Don't show
SQL unless asked; the user can see your tool calls.

Example queries (patterns only):

-- Songs an artist played most often in NYC (headline shows, full songs only)
SELECT sp.song_name, count(*) AS times, any_value(sp.chart_peak) AS hot100_peak
FROM songs_played sp JOIN shows s USING (show_id)
WHERE s.artist_mbid = '<id from find_names>' AND s.role = 'headliner' AND NOT sp.is_partial
GROUP BY 1 ORDER BY times DESC;

-- An artist's Hot 100 songs they never played at an NYC headline show (only songs out by their last show)
SELECT DISTINCT cs.title, cs.peak_position
FROM artist_chart_credits acc JOIN chart_songs cs USING (performer)
WHERE acc.artist_mbid = '<id>'
  AND cs.first_chart_week <= (SELECT max(show_date) FROM shows WHERE artist_mbid = '<id>')
  AND cs.title NOT IN (
    SELECT sp.chart_title FROM songs_played sp JOIN shows s USING (show_id)
    WHERE s.artist_mbid = '<id>' AND sp.chart_title IS NOT NULL)
ORDER BY cs.peak_position;

-- Was a song on the Hot 100 the week of a show?
SELECT h.chart_rank FROM hot100_weekly h
WHERE h.title = '<chart_title>' AND h.performer = '<chart_performer>'
  AND h.chart_week BETWEEN DATE '<show_date>' - 6 AND DATE '<show_date>';
""",
    # LiteLLM routes the call to Gemini, the same way agents-sdk-web does.
    # api_key is unused: Vertex AI authenticates with your gcloud credentials.
    model=LitellmModel(model=MODEL, api_key="unused"),
    model_settings=ModelSettings(extra_args={"vertex_location": "global"}),
    tools=[read_mappings, get_enum_values, find_names, run_sql, make_chart],
)


class RecordToolCalls(RunHooks):
    """Watches the run and records each tool call, for the page to show.

    Make a new one per request: it holds this run's calls. Each call records which agent made it.
    """

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # For a function tool, context is a ToolContext: it carries the call's arguments.
        self.tool_calls += [{
            "agent": agent.name,
            "name": tool.name,
            "args": json.loads(context.tool_arguments or "{}"),
            "result": str(result),
        }]


async def ask(agent: Agent, message: str, session: SQLiteSession, hooks: RecordToolCalls) -> str:
    """Run the agent. Flash-Lite sometimes ends a turn after its tool calls without writing anything,
    so if the answer is empty we nudge it once to finish from what it found."""
    result = await Runner.run(agent, message, session=session, hooks=hooks, max_turns=MAX_TURNS)
    if not (result.final_output or "").strip():
        result = await Runner.run(agent, "Finish answering my question from what you found.",
                                  session=session, hooks=hooks, max_turns=MAX_TURNS)
    return result.final_output


# --- Session Store ---

SESSIONS_DB = "conversations.db"

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Get or create the session. A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    hooks = RecordToolCalls()

    try:
        response, tool_calls = await ask(agent, request.message, session, hooks), hooks.tool_calls
    except Exception as e:
        # Auth, billing, max_turns, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", hooks.tool_calls

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run sets PORT and needs 0.0.0.0; locally we stay on 127.0.0.1:8000.
    port = os.getenv("PORT")
    uvicorn.run(app, host="0.0.0.0" if port else "127.0.0.1", port=int(port or 8000))
