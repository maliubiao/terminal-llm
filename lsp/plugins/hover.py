from .. import GenericLSPClient
from ..utils import _validate_args
from . import LSPCommandPlugin, format_response_panel


class HoverPlugin(LSPCommandPlugin):
    command_name = "hover"
    command_params = ["file_path", "line", "character"]
    description = "获取悬停信息"

    @staticmethod
    async def handle_command(console, lsp_client: GenericLSPClient, parts):
        if not _validate_args(console, parts, 4):
            return
        _, file_path, line, char = parts
        try:
            line = int(line)
            char = int(char)
        except ValueError:
            console.print("[red]行号和列号必须是数字[/red]")
            return

        result = await lsp_client.get_hover_info(file_path, line, char)
        if result:
            console.print(format_response_panel(result, "悬停信息", "green"))

    def __str__(self):
        return f"{self.command_name}: {self.description}"
