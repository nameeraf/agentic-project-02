import json
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

# --- The Database ---

# build_mappings.py builds both files once, offline. The app only reads them.
DATA = Path(__file__).parent / "data"
MAX_ROWS = 100

try:
    MAPPINGS = json.loads((DATA / "survey_mappings.json").read_text())
    # The security boundary is the connection, not the prompt: read-only, no file or network
    # access, and the agent can't SET its way out of either.
    db = duckdb.connect(
        str(DATA / "survey.duckdb"),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
except (FileNotFoundError, duckdb.Error):
    raise SystemExit("No database found. Run `uv run build_mappings.py` first.")

# --- Tools ---


@function_tool
def read_mappings() -> str:
    """Read the survey table's columns: name, type, a description, and whether it's an enum.

    Call this first. Call get_enum_values for an enum column's valid values.
    """
    return json.dumps([
        {"name": c["name"], "type": c["type"], "description": c["description"], "is_enum": "enum_values" in c}
        for c in MAPPINGS["columns"]
    ])


@function_tool
def get_enum_values(column: str) -> str:
    """Get the exact valid values of an enum column. Match them exactly: equality is case-sensitive.

    Args:
        column: The column name, e.g. 'overall_satisfaction'.
    """
    match = next((c for c in MAPPINGS["columns"] if c["name"] == column), None)
    if match is None:
        return json.dumps({"error": f"No column '{column}'. Call read_mappings for the columns."})
    if "enum_values" not in match:
        return json.dumps({"error": f"'{column}' is not an enum column."})
    return json.dumps({"column": column, "enum_values": match["enum_values"]}, default=str)


@tool_input_guardrail
def no_na_strings(data) -> ToolGuardrailFunctionOutput:
    """Reject SQL that compares against the string 'NA'. The data has no 'NA' strings, only NULLs."""
    query = json.loads(data.context.tool_arguments or "{}").get("query", "")
    if re.search(r"[!=<>]+\s*'NA'", query, re.IGNORECASE):
        # The model reads this instead of a result, and tries again.
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: missing values are NULL, not 'NA'. Use IS NULL / IS NOT NULL."
        )
    return ToolGuardrailFunctionOutput.allow()


@function_tool(tool_input_guardrails=[no_na_strings])
def run_sql(query: str) -> str:
    """Run a DuckDB SQL query against the survey table and return the results.

    Args:
        query: A DuckDB SQL query.
    """
    # The SDK runs tools in threads. Each call gets its own cursor: DuckDB connections aren't thread-safe.
    with db.cursor() as cursor:
        try:
            cursor.execute(query)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except duckdb.Error as e:
            # The model cannot see an exception. Return something it can reason about.
            return json.dumps({"error": str(e)})
        columns = [column[0] for column in cursor.description or []]
    return json.dumps({"columns": columns, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}, default=str)


# --- The Agent ---

agent = Agent(
    name="Subway Survey Analyst",
    instructions="""\
You are a data analyst. Answer questions about a 2019 survey of NYC subway riders.
The data is in one DuckDB table, `survey`, one row per response.

- Call read_mappings first to see the columns.
- Before filtering on an enum column, call get_enum_values. Never guess values.
- Missing values are NULL. Exclude them from filters, groups, and aggregates.
- top_three_complaints is a VARCHAR[] list. Unnest it with
  FROM survey, unnest(top_three_complaints) AS t(complaint).
- Answer in plain English. Don't show the SQL unless asked.

Example queries:

-- Percentage breakdown with a window function
SELECT overall_satisfaction, count(*) AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM survey WHERE overall_satisfaction IS NOT NULL
GROUP BY 1 ORDER BY n DESC;

-- Unnesting a list column
SELECT complaint, count(*) AS n
FROM survey, unnest(top_three_complaints) AS t(complaint)
GROUP BY 1 ORDER BY n DESC;

-- Mapping an ordinal enum to a score
SELECT subway_line_used_most_often AS line,
       round(avg(CASE overall_satisfaction
         WHEN 'Very satisfied' THEN 5 WHEN 'Somewhat satisfied' THEN 4
         WHEN 'Neutral' THEN 3 WHEN 'Not satisfied' THEN 2
         WHEN 'Highly unsatisfied' THEN 1 END), 2) AS avg_score
FROM survey
WHERE subway_line_used_most_often IS NOT NULL AND overall_satisfaction IS NOT NULL
GROUP BY 1 ORDER BY avg_score DESC;
""",
    # LiteLLM routes the call to Gemini, the same way agents-sdk-web does.
    # api_key is unused: Vertex AI authenticates with your gcloud credentials.
    model=LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused"),
    model_settings=ModelSettings(extra_args={"vertex_location": "global"}),
    tools=[read_mappings, get_enum_values, run_sql],
)


class RecordToolCalls(RunHooks):
    """Watches the run and records each tool call, for the page to show.

    Make a new one per request: it holds this run's calls.
    """

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # For a function tool, context is a ToolContext: it carries the call's arguments.
        self.tool_calls += [{"name": tool.name, "args": json.loads(context.tool_arguments), "result": str(result)}]


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
        result = await Runner.run(agent, request.message, session=session, hooks=hooks)
        response, tool_calls = result.final_output, hooks.tool_calls
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
