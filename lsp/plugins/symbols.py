import os

from rich.panel import Panel
from rich.tree import Tree

from ..utils import (
    _build_symbol_tree,
    _create_symbol_table,
    _validate_args,
)
from . import LSPCommandPlugin


class SymbolsPlugin(LSPCommandPlugin):
    command_name = "symbols"
    command_params = ["file_path"]
    description = "获取文档符号列表（支持层次结构/扁平列表）"

    @staticmethod
    async def handle_command(console, lsp_client, parts):
        if not _validate_args(console, parts, 2):
            return

        file_path = os.path.abspath(parts[1])
        console.print(f"[dim]正在从LSP服务器获取符号: {file_path}...[/]")

        try:
            result = await lsp_client.get_document_symbols(file_path)
        except Exception as e:
            console.print(f"[red]请求失败: {str(e)}[/red]")
            return

        if not result:
            console.print(Panel("🕳️ 没有找到任何文档符号", title="空结果", border_style="blue"))
            return

        if isinstance(result, list) and len(result) > 0:
            # 判断是DocumentSymbol还是SymbolInformation
            if "location" in result[0]:
                console.print(
                    Panel(
                        _create_symbol_table(result),
                        title="📋 符号列表（扁平结构）",
                        border_style="yellow",
                        subtitle=f"共 {len(result)} 个符号",
                    )
                )
            else:
                tree = Tree("📂 文档符号层次结构", highlight=True, guide_style="dim")
                total_count = 0
                for sym in result:
                    _build_symbol_tree(sym, tree)
                    total_count += _count_symbols(sym)

                console.print(
                    Panel(tree, title=f"🌳 符号树（共 {total_count} 个符号）", border_style="green", padding=(1, 2))
                )
        else:
            console.print(Panel("⚠️ 收到非预期的响应格式", title="解析错误", border_style="red"))


def _count_symbols(symbol):
    """递归统计符号数量"""
    count = 1
    for child in symbol.get("children", []):
        count += _count_symbols(child)
    return count
