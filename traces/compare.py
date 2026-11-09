"""Make the README's evidence: run questions through three setups and save every trace to traces/.

- plain: the same model and database with one run_sql tool and a one-line prompt (the lecture's
  plain Text2SQL agent). No mappings, no name lookup, no guardrails, no notes.
- ours:  the agent in app.py.
- no_tools: the same model with no tools and no system prompt (README sample queries only).

    uv run traces/compare.py            # all questions
    uv run traces/compare.py t1 s2      # just these ids

None of these questions appear as few-shot examples in app.py's prompt.
"""

import asyncio
import json
import sys
from pathlib import Path

import litellm
from agents import Agent, SQLiteSession, function_tool

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py is at the repo root
import app  # noqa: E402

TRACES = Path(__file__).parent  # this script lives in traces/

QUESTIONS = [
    # Sample queries for the grader (README): expected answers checked by hand with SQL.
    {"id": "s1", "kind": "sample", "question": "How many times did Billy Joel play Piano Man at Madison Square Garden?"},
    {"id": "s2", "kind": "sample", "question": "Which of Olivia Rodrigo's Hot 100 songs has she never played at an NYC show?"},
    {"id": "s3", "kind": "sample", "question": "Which artists went from playing NYC clubs to headlining an NYC arena, and who had the most hits in between?"},
    # Where a plain run_sql agent goes wrong.
    {"id": "t1", "kind": "trap", "question": "Which of SZA's Hot 100 hits has she never played at an NYC show?"},
    {"id": "t2", "kind": "trap", "question": "How many times did Madonna perform Causing a Commotion in NYC?"},
]


@function_tool
def run_sql(sql: str) -> str:
    """Run a DuckDB SQL query and return the results.

    Args:
        sql: A DuckDB SQL query.
    """
    return json.dumps(app.query(sql), default=str)


plain = Agent(
    name="Plain run_sql",
    instructions="You are a data analyst. Answer questions about NYC concerts (setlist.fm) and the Billboard Hot 100. "
    "The data is in DuckDB. Use run_sql.",
    model=app.agent.model,
    model_settings=app.agent.model_settings,
    tools=[run_sql],
)


async def run_agent(agent: Agent, question: str) -> dict:
    hooks = app.RecordToolCalls()
    try:
        # In-memory session, so the empty-answer retry in app.ask has the history to continue from.
        answer = await app.ask(agent, question, SQLiteSession(f"compare-{id(question)}"), hooks)
    except Exception as e:
        answer = f"FAILED: {type(e).__name__}: {e}"
    return {"agent": agent.name, "question": question, "answer": answer, "tool_calls": hooks.tool_calls}


async def run_no_tools(question: str) -> dict:
    response = await litellm.acompletion(
        model=app.MODEL, vertex_location="global", messages=[{"role": "user", "content": question}]
    )
    return {"agent": "no tools, no system prompt", "question": question, "answer": response.choices[0].message.content}


async def run_question(q: dict) -> None:
    jobs = {"ours": run_agent(app.agent, q["question"])}
    if q["kind"] == "trap":
        jobs["plain"] = run_agent(plain, q["question"])
    else:
        jobs["no_tools"] = run_no_tools(q["question"])
    for label, trace in zip(jobs, await asyncio.gather(*jobs.values())):
        (TRACES / f"{q['id']}_{label}.json").write_text(json.dumps(trace, indent=2, default=str))
        print(f"--- {q['id']} [{label}] {len(trace.get('tool_calls', []))} tool calls\n{trace['answer']}\n")


async def main(ids: list[str]):
    await asyncio.gather(*(run_question(q) for q in QUESTIONS if not ids or q["id"] in ids))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
