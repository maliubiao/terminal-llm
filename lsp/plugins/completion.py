from rich.syntax import Syntax

from ..utils import (
    _create_completion_table,
    _validate_args,
    format_completion_item,
)
from . import LSPCommandPlugin


class CompletionPlugin(LSPCommandPlugin):
    command_name = "completion"
    command_params = ["file_path", "line", "character"]
    description = "获取代码补全建议"

    @staticmethod
    async def handle_command(console, lsp_client, parts):
        if not _validate_args(console, parts, 4):
            return
        _, file_path, line, char = parts
        try:
            line = int(line)
            char = int(char)
        except ValueError:
            console.print("[red]行号和列号必须是数字[/red]")
            return

        result = await lsp_client.get_completion(file_path, line, char)
        if result and isinstance(result, dict):
            items = result.get("items", [])
            formatted = [format_completion_item(item) for item in items]
            console.print(_create_completion_table(formatted))
