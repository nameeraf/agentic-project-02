"""Build the database and its schema mappings. Run once, offline, before app.py.

1. Load the survey CSV into data/survey.duckdb, cleaned: 'NA' and '' become NULL, the
   complaints column becomes a real list, and timestamps become timestamps.
2. Describe every column: its type, and either its valid values (enums) or samples.
3. Ask the model for a one-sentence description of each column.
4. Write it all to data/survey_mappings.json, the context app.py retrieves.
"""

import json
from pathlib import Path

import duckdb
import litellm

DATA = Path(__file__).parent / "data"
CSV_PATH = DATA / "subway_rider_survey_2019.csv"
DB_PATH = DATA / "survey.duckdb"
MAPPINGS_PATH = DATA / "survey_mappings.json"
MAX_ENUM_VALUES = 50

# --- 1. The database ---

DB_PATH.unlink(missing_ok=True)
db = duckdb.connect(str(DB_PATH))
db.execute(f"""
    CREATE TABLE survey AS
    SELECT * REPLACE (
        json_extract_string(top_three_complaints, '$[*]')::VARCHAR[] AS top_three_complaints,
        strptime(submitted_at, '%m/%d/%Y @ %I:%M %p') AS submitted_at
    )
    FROM read_csv('{CSV_PATH}', nullstr = ['NA', ''], types = {{'zip_code': 'VARCHAR', 'subway_line_used_most_often': 'VARCHAR'}})
""")

# --- 2. Describe each column ---

columns = []
for name, dtype, *_ in db.execute("DESCRIBE survey").fetchall():
    # List columns (top_three_complaints) take their values from the unnested items.
    values = f"unnest({name})" if dtype.endswith("[]") else name
    distinct = f"SELECT DISTINCT {values} AS v FROM survey WHERE {name} IS NOT NULL"
    count = db.execute(f"SELECT count(*) FROM ({distinct})").fetchone()[0]
    column = {"name": name, "type": dtype}
    if count <= MAX_ENUM_VALUES and dtype != "TIMESTAMP":
        column["enum_values"] = [row[0] for row in db.execute(f"{distinct} ORDER BY v").fetchall()]
    else:
        column["samples"] = [row[0] for row in db.execute(f"{distinct} LIMIT 5").fetchall()]
    columns.append(column)
db.close()

# --- 3. Describe each column, with the model ---

prompt = (
    "These are the columns of a 2019 NYC subway rider survey, with their types and either "
    "their valid values or samples. Write a concise one-sentence description of what each "
    "column represents. Don't just list the values. Return a JSON object mapping each column "
    "name to its description.\n\n" + json.dumps(columns, default=str)
)
response = litellm.completion(
    model="vertex_ai/gemini-3.5-flash-lite",
    vertex_location="global",
    messages=[{"role": "user", "content": prompt}],
    response_format={"type": "json_object"},
)
descriptions = json.loads(response.choices[0].message.content)

# --- 4. Write the mappings ---

for column in columns:
    column["description"] = descriptions.get(column["name"], "")
    column.pop("samples", None)  # samples helped the model describe the column; the agent doesn't need them
MAPPINGS_PATH.write_text(json.dumps({"table": "survey", "columns": columns}, indent=2, default=str))
print(f"Built {DB_PATH.name} and {MAPPINGS_PATH.name} ({len(columns)} columns)")
