"""
Natural-language review assistant.

Lets a non-technical user fix flagged loads by typing plain English, e.g.
  "load 5621 carrier amount should be 2100"
  "mark 5507 as resolved, the invoice is correct"
  "clear the red flag on 5634"

Backed by Claude with a small set of sheet-editing tools.
"""

import json

import anthropic
from loguru import logger

from config import Config
from tools.sheets import GoogleSheetsClient
import review as review_store

ACCOUNTS = "Accounts"

TOOLS = [
    {
        "name": "get_load_row",
        "description": "Read the current Accounts-sheet row for a load number. "
                       "Returns all column values so you can see what's there before editing.",
        "input_schema": {
            "type": "object",
            "properties": {"load_no": {"type": "string"}},
            "required": ["load_no"],
        },
    },
    {
        "name": "update_load_cells",
        "description": "Update one or more columns in a load's row. "
                       "Pass updates as {column_header: value}. Use exact column headers "
                       "from get_load_row (e.g. 'Carrier name', 'Carrier Amount', 'Currency', "
                       "'Shipper Amt', 'Carrier In#', 'HST', 'Remarks', 'STATUS').",
        "input_schema": {
            "type": "object",
            "properties": {
                "load_no": {"type": "string"},
                "updates": {"type": "object"},
            },
            "required": ["load_no", "updates"],
        },
    },
    {
        "name": "clear_flag",
        "description": "Clear the red/orange highlight on a load's row once the issue is fixed. "
                       "Optionally also marks the review item resolved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "load_no": {"type": "string"},
                "mark_resolved": {"type": "boolean"},
            },
            "required": ["load_no"],
        },
    },
    {
        "name": "resolve_review",
        "description": "Mark a load's review item as resolved (removes it from the review list) "
                       "without changing the sheet — use when the data is actually fine as-is.",
        "input_schema": {
            "type": "object",
            "properties": {"load_no": {"type": "string"}},
            "required": ["load_no"],
        },
    },
]

SYSTEM = """You are the review assistant for Demo Logistics' invoice agent.

A user is reviewing loads the agent flagged (discrepancies, errors). They'll ask you
in plain English to fix data in the Google Sheet or to mark items resolved.

Guidelines:
- ALWAYS call get_load_row first to see current values before updating.
- Make the smallest change that satisfies the request. Don't touch unrelated columns.
- After fixing a discrepancy, offer to clear the red flag (or do it if they asked).
- Confirm back in one short sentence what you changed, with the load number.
- If a request is ambiguous (which column? which value?), ask a brief clarifying question
  instead of guessing.
- Amounts: write plain numbers (no $ or commas). Dates: MM/DD/YYYY.
"""


def _find_row(sheets: GoogleSheetsClient, load_no: str):
    records = sheets.get_all_records(ACCOUNTS)
    for i, r in enumerate(records):
        if str(r.get("Load no", "")).strip() == str(load_no).strip():
            r["__row_number__"] = i + 2
            return r
    return None


def _execute(sheets: GoogleSheetsClient, name: str, inp: dict) -> str:
    if name == "get_load_row":
        row = _find_row(sheets, inp["load_no"])
        if not row:
            return json.dumps({"error": f"No row found for load {inp['load_no']}"})
        # Drop internal/noisy empty fields for clarity
        clean = {k: v for k, v in row.items() if str(v).strip() and not k.startswith("__")}
        clean["__row_number__"] = row["__row_number__"]
        return json.dumps(clean)

    if name == "update_load_cells":
        row = _find_row(sheets, inp["load_no"])
        if not row:
            return json.dumps({"error": f"No row found for load {inp['load_no']}"})
        sheets.update_cells_by_header(ACCOUNTS, row["__row_number__"], inp["updates"])
        return json.dumps({"status": "ok", "updated": list(inp["updates"].keys())})

    if name == "clear_flag":
        row = _find_row(sheets, inp["load_no"])
        if not row:
            return json.dumps({"error": f"No row found for load {inp['load_no']}"})
        sheets.clear_row_highlight(ACCOUNTS, row["__row_number__"])
        if inp.get("mark_resolved"):
            for it in review_store.list_review():
                if it["load_no"] == str(inp["load_no"]):
                    review_store.resolve_review_item(it["key"])
        return json.dumps({"status": "ok", "cleared": True})

    if name == "resolve_review":
        done = False
        for it in review_store.list_review():
            if it["load_no"] == str(inp["load_no"]):
                review_store.resolve_review_item(it["key"])
                done = True
        return json.dumps({"status": "ok" if done else "not_found"})

    return json.dumps({"error": f"unknown tool {name}"})


def chat(messages: list[dict]) -> dict:
    """Run one turn of the review chat.
    messages: [{"role":"user"/"assistant","content":"..."}]
    Returns {"reply": str, "actions": [str]}.
    """
    config = Config()
    ai     = anthropic.Anthropic(api_key=config.anthropic_api_key)
    sheets = GoogleSheetsClient(config)

    convo = list(messages)
    actions: list[str] = []

    while True:
        resp = ai.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=convo,
        )
        convo.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                out = _execute(sheets, block.name, block.input)
                actions.append(f"{block.name}({json.dumps(block.input)})")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })
            convo.append({"role": "user", "content": results})
            continue

        # Final text reply
        reply = next((b.text for b in resp.content if hasattr(b, "text")), "")
        return {"reply": reply, "actions": actions}
