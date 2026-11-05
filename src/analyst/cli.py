"""CLI: chat plus observability and library commands."""

from __future__ import annotations

import json
import logging

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import tracing
from .config import Settings, load_users
from .errors import AnalystError
from .runtime import build_live_agent, build_runtime

app = typer.Typer(help="Retail analyst agent: chat with your sales data.", no_args_is_help=True)
console = Console()

HELP = """Commands: /help, /reports, /prefs, /trace (last trace id), /persona, /quit
Examples: "top 5 products by revenue last month", "why is Texas underspending vs California?",
"create a Q1 report with action items", "delete all reports from this conversation"."""


def _confirm(preview: dict) -> bool:
    """Show the preview and ask for a typed yes. Used after the agent paused with needs_confirmation."""
    table = Table(title=f"{preview['matched']} report(s) will be deleted", show_lines=False)
    table.add_column("id", justify="right")
    table.add_column("title")
    table.add_column("created")
    for r in preview["reports"]:
        table.add_row(str(r["id"]), r["title"], r["created_at"][:19])
    console.print(table)
    answer = console.input("[bold red]Type 'yes' to delete these reports, anything else to cancel:[/] ").strip()
    return answer.lower() == "yes"


@app.command()
def chat(
    user: str = typer.Option("alice", help="User id from config/users.yaml"),
    conversation: str = typer.Option(None, help="Continue an earlier conversation by id (shown in the banner)"),
    verbose: bool = False,
) -> None:
    """Start an interactive chat as USER."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        rt, agent = build_live_agent(user, conversation_id=conversation)
    except (AnalystError, KeyError) as e:
        console.print(f"[red]{e.args[0] if e.args else e}[/]")
        raise typer.Exit(1)
    u = agent.user
    console.print(Panel(f"Hi {u.name} ({u.role}). Scope: {u.scope_text()}. Model: {rt.settings.model}. "
                        f"Conversation: {agent.conversation_id}.\n{HELP}", title="retail analyst"))
    while True:
        try:
            msg = console.input("[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye")
            break
        if not msg:
            continue
        if msg in ("/quit", "/exit", "/q"):
            break
        if msg == "/help":
            console.print(HELP)
            continue
        if msg == "/reports":
            _print_reports(rt.reports.find(u.id))
            continue
        if msg == "/prefs":
            console.print(rt.prefs.all(u.id) or "no preferences yet")
            continue
        if msg == "/trace":
            console.print(f"last trace: {rt.tracer.last_trace_id}  (python -m analyst trace {rt.tracer.last_trace_id})")
            continue
        if msg == "/persona":
            console.print(agent.persona_file.read())
            continue
        with console.status("thinking..."):
            result = agent.handle(msg)
        if result.status == "needs_confirmation":
            # The turn is parked; nothing is deleted until the user types yes.
            approved = _confirm(result.pending or {})
            with console.status("thinking..."):
                result = agent.resume(approved)
        style = {"ok": "green", "refused": "yellow", "error": "red"}.get(result.status, "red")
        console.print(Panel(Markdown(result.text), title=f"[{style}]analyst[/] · trace {result.trace_id}", border_style=style))


def _print_reports(rows: list[dict]) -> None:
    if not rows:
        console.print("no reports")
        return
    table = Table(title="saved reports")
    table.add_column("id", justify="right")
    table.add_column("title")
    table.add_column("created")
    table.add_column("conversation")
    for r in rows:
        table.add_row(str(r["id"]), r["title"], r["created_at"][:19], r["conversation_id"])
    console.print(table)


@app.command()
def reports(user: str = "alice", show: int = typer.Option(None, help="Print the full report with this id")) -> None:
    """List (or show) a user's saved reports."""
    rt = build_runtime()
    if show:
        r = rt.reports.get(user, show)
        console.print(Markdown(r["content"]) if r else "not found")
    else:
        _print_reports(rt.reports.find(user))


@app.command()
def stats() -> None:
    """Agent-level metrics over all traces."""
    rt = build_runtime()
    s = tracing.stats(rt.conn)
    table = Table(title="agent metrics")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for k, v in s.items():
        table.add_row(k, f"{v:,}" if isinstance(v, int) else str(v))
    console.print(table)


@app.command()
def traces(last: int = 10) -> None:
    """Recent turns with status, latency and cost."""
    rt = build_runtime()
    table = Table(title=f"last {last} traces")
    for col in ("trace_id", "user", "status", "guard", "ms", "llm", "tools", "sql", "fail", "tokens", "message"):
        table.add_column(col)
    for t in tracing.recent(rt.conn, last):
        table.add_row(
            t["trace_id"], t["user_id"], t["status"], t["guard_result"], str(t["duration_ms"]), str(t["llm_calls"]),
            str(t["tool_calls"]), str(t["sql_attempts"]), str(t["sql_failures"]),
            str((t["prompt_tokens"] or 0) + (t["completion_tokens"] or 0)), (t["user_message"] or "")[:40],
        )
    console.print(table)


@app.command()
def trace(trace_id: str) -> None:
    """Deep dive: every LLM call, tool call and SQL of one turn."""
    rt = build_runtime()
    t = tracing.get_trace(rt.conn, trace_id)
    if not t:
        console.print("not found")
        raise typer.Exit(1)
    spans = t.pop("spans")
    console.print(Panel(json.dumps(t, indent=2, default=str), title="trace"))
    for s in spans:
        console.print(Panel(json.dumps(s["payload"], indent=2, default=str)[:4000], title=f"{s['kind']} · {s['name']} · {s['status']} · {s['duration_ms']} ms"))


@app.command()
def golden(
    action: str = typer.Argument("list", help="list | approve"),
    trio_id: str = typer.Argument(None),
    note: str = typer.Option(None, help="Short analyst note stored with the approved trio (default: first lines of the report)"),
    no_check: bool = typer.Option(False, help="Skip the BigQuery dry run of the candidate's SQL"),
) -> None:
    """Golden bucket: list trios or approve a candidate (its SQL is scoped and dry-run first)."""
    rt = build_runtime()
    if action == "approve":
        cand = next((c for c in rt.golden.list("candidate") if c["id"] == trio_id), None)
        if cand is None:
            console.print("not found")
            raise typer.Exit(1)
        if not no_check:
            from .bq import BigQueryRunner
            from .config import User
            from .scope import make_safe_sql

            bq = BigQueryRunner(rt.settings.project, rt.settings.max_bytes_per_query)
            for i, sql in enumerate(cand["sql"].split("\n\n-- next query\n"), 1):
                try:
                    bq.dry_run(make_safe_sql(sql, User(id="reviewer", name="reviewer", role="reviewer", brands=[])))
                except AnalystError as e:
                    console.print(f"[red]query {i} does not run: {e.args[0] if e.args else e}[/] (use --no-check to approve anyway)")
                    raise typer.Exit(1)
                except Exception as e:
                    console.print(f"[red]query {i} does not run: {str(e)[:300]}[/] (use --no-check to approve anyway)")
                    raise typer.Exit(1)
        console.print("approved" if rt.golden.approve(trio_id, note) else "not found")
        return
    table = Table(title="golden trios")
    table.add_column("id")
    table.add_column("status")
    table.add_column("question")
    table.add_column("thumbs", justify="right")
    thumbs = tracing.trio_feedback(rt.conn)
    for t in rt.golden.list():
        th = thumbs.get(t["id"], {"up": 0, "down": 0})
        flag = " [red]review[/]" if th["down"] >= 2 and th["down"] > th["up"] else ""
        table.add_row(t["id"], t.get("status", ""), t["question"][:80], f"+{th['up']}/-{th['down']}{flag}")
    console.print(table)


@app.command()
def feedback(
    trace_id: str = typer.Argument(..., help="trace id printed after an answer (prefix is enough)"),
    verdict: str = typer.Argument(..., help="up | down"),
    user: str = typer.Option("alice"),
    comment: str = typer.Option("", help="what was wrong or right"),
) -> None:
    """Thumbs up/down on an answer; attached to the golden trios that shaped it."""
    rt = build_runtime()
    try:
        res = tracing.record_feedback(rt.conn, trace_id, user, verdict, comment)
    except (KeyError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"recorded {res['verdict']} on {res['trace_id']}" + (f" (trios: {', '.join(res['golden_ids'])})" if res["golden_ids"] else ""))


@app.command()
def persona() -> None:
    """Print the active persona text (edit prompts/persona.md to change it, no restart needed)."""
    s = Settings()
    console.print((s.prompts_dir / "persona.md").read_text(encoding="utf-8"))


@app.command()
def users() -> None:
    """List demo users and their scope."""
    s = Settings()
    for uid, u in load_users(s.users_path).items():
        console.print(f"{uid}: {u.name}, {u.role}, {u.scope_text()}")


if __name__ == "__main__":
    app()
