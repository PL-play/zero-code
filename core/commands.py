from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.runtime import AGENT_DIR, SKILLS_DIR
from core.state import COMPACT_THRESHOLD, CTX, SKILL_LOADER, UI

SLASH_COMMANDS = [
    {"name": "/help", "args": "", "description": "Show available commands"},
    {"name": "/compact", "args": "[focus]", "description": "Compress conversation context (optional focus topic)"},
    {"name": "/context", "args": "", "description": "Show token usage and context stats"},
    {"name": "/skills", "args": "", "description": "List loaded skills"},
]


def _handle_help(**_):
    table = Table(show_header=True, border_style="blue", padding=(0, 1))
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    for cmd in SLASH_COMMANDS:
        name_col = cmd["name"]
        if cmd["args"]:
            name_col += f" {cmd['args']}"
        table.add_row(name_col, cmd["description"])
    table.add_row("[dim]exit / quit / q[/dim]", "[dim]Exit the program[/dim]")
    panel = Panel(
        table,
        title="[bold blue]Commands[/bold blue]",
        border_style="blue",
        padding=(0, 1),
    )
    UI.console.print(panel)


def _handle_compact(raw_query: str, history: list):
    focus = raw_query.strip()[len("/compact"):].strip() or None
    UI.console.print(f"[dim]{UI._ts()} Compacting conversation...[/dim]")
    history[:] = CTX.compact(history, focus=focus)
    CTX.reset_usage()
    UI.console.print(f"[dim]{UI._ts()} Done. Context compacted. {len(history)} messages remaining.[/dim]")


def _handle_context(**_):
    usage_text = CTX.all_usage_summary()
    panel = Panel(
        Text(f"{usage_text}\nmessages={len(_['history'])} | compact_threshold={COMPACT_THRESHOLD:,}"),
        title="[bold yellow]Context & Token Usage[/bold yellow]",
        border_style="yellow",
        padding=(0, 1),
    )
    UI.console.print(panel)


def _handle_skills(**_):
    if not SKILL_LOADER.skills:
        UI.console.print(f"[dim]{UI._ts()} No skills found in {SKILLS_DIR}/[/dim]")
        return
    table = Table(show_header=True, border_style="magenta", padding=(0, 1))
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("Tags", style="dim")
    table.add_column("Path", style="dim")
    for name, skill in SKILL_LOADER.skills.items():
        desc = skill["meta"].get("description", "-")
        tags = skill["meta"].get("tags", "-")
        rel_path = str(Path(skill["path"]).relative_to(AGENT_DIR))
        table.add_row(name, desc, str(tags), rel_path)
    panel = Panel(
        table,
        title=f"[bold magenta]Skills[/bold magenta] [dim]({len(SKILL_LOADER.skills)} loaded)[/dim]",
        subtitle=f"[dim]{SKILLS_DIR}/[/dim]",
        border_style="magenta",
        padding=(0, 1),
    )
    UI.console.print(panel)


COMMAND_DISPATCH = {
    "/help": _handle_help,
    "/compact": _handle_compact,
    "/context": _handle_context,
    "/skills": _handle_skills,
}

