"""Tool declarations (JSON schema) given to the model. Execution lives in agent.py."""

TOOLS = [
    {
        "name": "get_schema",
        "description": "Get the columns of one table or of all four tables (orders, order_items, products, users), with join hints.",
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "orders | order_items | products | users. Omit for all."}},
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run a BigQuery Standard SQL SELECT over orders, order_items, products, users (short names, no dataset prefix). "
            "Returns up to 50 rows plus stats. The user's data scope and PII rules are applied automatically. "
            "On error you get the error message and a hint: fix the SQL and call again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The SELECT query."},
                "purpose": {"type": "string", "description": "One line: what this query is for."},
            },
            "required": ["sql", "purpose"],
        },
    },
    {
        "name": "save_report",
        "description": "Save a finished markdown report (title, summary, findings with numbers, action items) to the user's report library.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "markdown": {"type": "string", "description": "Full report body in markdown."},
            },
            "required": ["title", "markdown"],
        },
    },
    {
        "name": "list_reports",
        "description": "List the user's saved reports, optionally only those mentioning a text or only those from this conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "mentioning": {"type": "string", "description": "Text that must appear in the title or body."},
                "this_conversation": {"type": "boolean", "description": "Only reports created in this conversation."},
            },
        },
    },
    {
        "name": "request_delete_reports",
        "description": (
            "Start deleting the user's own reports that match the filter. This only previews the matches and asks the "
            "user to confirm; nothing is deleted until the user types yes. Use mentioning for 'reports about X' and "
            "this_conversation for 'reports we made in this conversation'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mentioning": {"type": "string"},
                "this_conversation": {"type": "boolean"},
            },
        },
    },
    {
        "name": "remember_preference",
        "description": "Remember a lasting preference of this user, e.g. format=table, depth=brief, charts=no, tone=formal.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
        },
    },
]
