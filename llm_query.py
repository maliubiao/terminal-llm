#!/usr/bin/env python
"""
LLM 查询工具模块

该模块提供与OpenAI兼容 API交互的功能，支持代码分析、多轮对话、剪贴板集成等功能。
包含代理配置检测、代码分块处理、对话历史管理等功能。
"""

import argparse
import datetime
import difflib
import fnmatch
import json
import logging
import marshal
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import trace
import traceback
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Union
from urllib.parse import urlparse

import requests
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments import highlight
from pygments.formatters.terminal import TerminalFormatter
from pygments.lexers.diff import DiffLexer
from pygments.lexers.markup import MarkdownLexer

# 初始化Markdown渲染器
from rich.console import Console
from rich.table import Table
from rich.text import Text

from tree import BlockPatch

MAX_FILE_SIZE = 32000
MAX_PROMPT_SIZE = int(os.environ.get("GPT_MAX_TOKEN", 16384))
LAST_QUERY_FILE = os.path.join(os.path.dirname(__file__), ".lastquery")
PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


@dataclass
class TextNode:
    """纯文本节点"""

    content: str


@dataclass
class CmdNode:
    """命令节点"""

    command: str
    command_type: str = None
    args: List[str] = None


@dataclass
class TemplateNode:
    """模板节点，可能包含多个命令节点"""

    template: CmdNode
    commands: List[CmdNode]


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="终端智能AI辅助工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="要分析的源代码文件路径")
    group.add_argument("--ask", help="直接提供提示词内容，与--file互斥")
    group.add_argument("--chatbot", action="store_true", help="进入聊天机器人UI模式，与--file和--ask互斥")
    parser.add_argument(
        "--prompt-file",
        default=os.path.expanduser("~/.llm/source-query.txt"),
        help="提示词模板文件路径（仅在使用--file时有效）",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=MAX_FILE_SIZE,
        help="代码分块大小（字符数，仅在使用--file时有效）",
    )
    parser.add_argument(
        "--obsidian-doc",
        default=os.environ.get("GPT_DOC", os.path.join(os.path.dirname(__file__), "obsidian")),
        help="Obsidian文档备份目录路径",
    )
    parser.add_argument("--trace", action="store_true", help="启用详细的执行跟踪")
    return parser.parse_args()


def sanitize_proxy_url(url):
    """隐藏代理地址中的敏感信息"""
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return parsed._replace(netloc=netloc).geturl()
        return url
    except ValueError as e:
        print(f"解析代理URL失败: {e}")
        return url


def detect_proxies():
    """检测并构造代理配置"""
    proxies = {}
    sources = {}
    proxy_vars = [
        ("http", ["http_proxy", "HTTP_PROXY"]),
        ("https", ["https_proxy", "HTTPS_PROXY"]),
        ("all", ["all_proxy", "ALL_PROXY"]),
    ]

    # 修改代理检测顺序，先处理具体协议再处理all_proxy
    for protocol, proxy_vars in reversed(proxy_vars):
        for var in proxy_vars:
            if var in os.environ and os.environ[var]:
                url = os.environ[var]
                if protocol == "all":
                    if not proxies.get("http"):
                        proxies["http"] = url
                        sources["http"] = var
                    if not proxies.get("https"):
                        proxies["https"] = url
                        sources["https"] = var
                else:
                    if protocol not in proxies:
                        proxies[protocol] = url
                        sources[protocol] = var
                break
    return proxies, sources


def split_code(content, chunk_size):
    """将代码内容分割成指定大小的块
    注意：当前实现适用于英文字符场景，如需支持多语言建议改用更好的分块算法
    """
    return [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]


INDEX_PATH = Path(__file__).parent / "conversation" / "index.json"


def _ensure_index():
    """确保索引文件存在，不存在则创建空索引"""
    if not INDEX_PATH.exists():
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf8") as f:
            json.dump({}, f)


def _update_index(uuid, file_path):
    """更新索引文件"""
    _ensure_index()
    with open(INDEX_PATH, "r+", encoding="utf8") as f:
        index = json.load(f)
        index[uuid] = str(file_path)
        f.seek(0)
        json.dump(index, f, indent=4)
        f.truncate()


def _build_index():
    """遍历目录构建索引"""
    index = {}
    conv_dir = Path(__file__).parent / "conversation"

    # 匹配文件名模式：任意时间戳 + UUID
    pattern = re.compile(r"^\d{1,2}-\d{1,2}-\d{1,2}-(.+?)\.json$")

    for root, _, files in os.walk(conv_dir):
        for filename in files:
            # 跳过索引文件本身
            if filename == "index.json":
                continue

            match = pattern.match(filename)
            if match:
                uuid = match.group(1)
                full_path = Path(root) / filename
                index[uuid] = str(full_path)

    with open(INDEX_PATH, "w", encoding="utf8") as f:
        json.dump(index, f, indent=4)

    return index


def get_conversation(uuid):
    """获取对话记录"""
    try:
        # 先尝试读取索引
        with open(INDEX_PATH, "r", encoding="utf8") as f:
            index = json.load(f)
            if uuid in index:
                path = Path(index[uuid])
                if path.exists():
                    return path
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 索引不存在或查找失败，重新构建索引
    index = _build_index()
    if uuid in index:
        return index[uuid]

    raise FileNotFoundError(f"Conversation with UUID {uuid} not found")


def new_conversation(uuid):
    """创建新对话记录"""
    current_datetime = datetime.datetime.now()

    # 生成日期路径组件（自动补零）
    date_dir = current_datetime.strftime("%Y-%m-%d")
    time_str = current_datetime.strftime("%H-%M-%S")

    # 构建完整路径
    base_dir = Path(__file__).parent / "conversation" / date_dir
    filename = f"{time_str}-{uuid}.json"
    file_path = base_dir / filename

    # 确保目录存在
    base_dir.mkdir(parents=True, exist_ok=True)

    # 写入初始数据并更新索引
    with open(file_path, "w", encoding="utf8") as f:
        json.dump([], f, indent=4)

    _update_index(uuid, file_path)
    return str(file_path)


def load_conversation_history(file_path):
    """加载对话历史文件"""
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []
    except (IOError, json.JSONDecodeError) as e:
        print(f"加载对话历史失败: {e}")
        return []


def save_conversation_history(file_path, history):
    """保存对话历史到文件"""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"保存对话历史失败: {e}")


def query_gpt_api(
    api_key: str,
    prompt: str,
    model: str = "gpt-4",
    **kwargs,
) -> dict:
    """支持多轮对话的OpenAI API流式查询

    参数:
        api_key (str): OpenAI API密钥
        prompt (str): 用户输入的提示词
        model (str): 使用的模型名称，默认为gpt-4
        kwargs: 其他可选参数，包括:
            base_url (str): API基础URL
            conversation_file (str): 对话历史存储文件路径
            console: 控制台输出对象
            temperature (float): 生成温度
            proxies: 代理设置

    返回:
        dict: 包含API响应结果的字典

    假设:
        - api_key是有效的OpenAI API密钥
        - prompt是非空字符串
        - conversation_file路径可写
        如果不符合上述假设，将记录错误并退出程序
    """
    try:
        # 初始化对话历史
        history = _initialize_conversation_history(kwargs)

        # 添加用户新提问到历史
        history.append({"role": "user", "content": prompt})

        # 获取API响应
        response = _get_api_response(api_key, model, history, kwargs)

        # 处理并保存响应
        return _process_and_save_response(response, history, kwargs)

    except Exception as e:
        print(f"OpenAI API请求失败: {e}")
        sys.exit(1)


def get_conversation_file(file):
    if file:
        return file
    cid = os.environ.get("GPT_UUID_CONVERSATION")

    if cid:
        try:
            conversation_file = get_conversation(cid)
        except FileNotFoundError:
            conversation_file = new_conversation(cid)
    else:
        conversation_file = os.path.join(os.path.dirname(__file__), "conversation_history.json")
    return conversation_file


def _initialize_conversation_history(kwargs: dict) -> list:
    """初始化对话历史

    参数:
        kwargs (dict): 包含conversation_file等参数

    返回:
        list: 对话历史列表
    """
    conversation_file = kwargs.get(
        "conversation_file",
    )
    return load_conversation_history(get_conversation_file(conversation_file))


def _get_api_response(
    api_key: str,
    model: str,
    history: list,
    kwargs: dict,
):
    """获取API流式响应

    参数:
        api_key (str): API密钥
        model (str): 模型名称
        history (list): 对话历史
        kwargs (dict): 其他参数

    返回:
        Generator: 流式响应生成器
    """
    client = OpenAI(api_key=api_key, base_url=kwargs.get("base_url"))

    return client.chat.completions.create(
        model=model,
        messages=history,
        temperature=kwargs.get("temperature", 0.0),
        max_tokens=MAX_PROMPT_SIZE,
        top_p=0.8,
        stream=True,
    )


def _process_and_save_response(
    stream,
    history: list,
    kwargs: dict,
) -> dict:
    """处理并保存API响应

    参数:
        stream (Generator): 流式响应
        history (list): 对话历史
        kwargs (dict): 包含conversation_file等参数

    返回:
        dict: 处理后的响应结果
    """
    content, reasoning = _process_stream_response(stream, kwargs.get("console"))

    # 将助理回复添加到历史
    history.append({"role": "assistant", "content": content})

    # 保存更新后的对话历史
    save_conversation_history(get_conversation_file(kwargs.get("conversation_file")), history)

    # 处理think标签
    content, reasoning = _handle_think_tags(content, reasoning)

    # 存储思维过程
    if reasoning:
        content = f"<think>\n{reasoning}\n</think>\n\n\n{content}"

    return {"choices": [{"message": {"content": content}}]}


def _process_stream_response(stream, console) -> tuple:
    """处理流式响应

    参数:
        stream (Generator): 流式响应
        console: 控制台输出对象

    返回:
        tuple: (正式内容, 推理内容)
    """
    content = ""
    reasoning = ""

    for chunk in stream:
        # 处理推理内容
        if hasattr(chunk.choices[0].delta, "reasoning_content") and chunk.choices[0].delta.reasoning_content:
            _print_content(chunk.choices[0].delta.reasoning_content, console, style="#00ff00")
            reasoning += chunk.choices[0].delta.reasoning_content

        # 处理正式回复内容
        if chunk.choices[0].delta.content:
            _print_content(chunk.choices[0].delta.content, console)
            content += chunk.choices[0].delta.content

    _print_newline(console)
    return content, reasoning


def _handle_think_tags(content: str, reasoning: str) -> tuple:
    """处理think标签

    参数:
        content (str): 原始内容
        reasoning (str): 推理内容

    返回:
        tuple: 处理后的内容和推理内容
    """
    thinking_end_tag = "</think>\n\n"
    thinking_start_tag = "<think>"

    if content and (content.find(thinking_end_tag) != -1 or content.find(thinking_start_tag) != -1):
        if content.find(thinking_start_tag) != -1:
            pos_start = content.find(thinking_start_tag)
            pos_end = content.find(thinking_end_tag)
            if pos_end != -1:
                reasoning = content[pos_start + len(thinking_start_tag) : pos_end]
                reasoning = reasoning.replace("\\n", "\n")
                content = content[pos_end + len(thinking_end_tag) :]
        else:
            pos = content.find(thinking_end_tag)
            reasoning = content[:pos]
            reasoning = reasoning.replace("\\n", "\n")
            content = content[pos + len(thinking_end_tag) :]

    return content, reasoning


def _print_content(content: str, console, style=None) -> None:
    """打印内容到控制台

    参数:
        content (str): 要打印的内容
        console: 控制台输出对象
        style: 输出样式
    """
    if console:
        console.print(content, end="", style=style)
    else:
        print(content, end="", flush=True)


def _print_newline(console) -> None:
    """打印换行符

    参数:
        console: 控制台输出对象
    """
    if console:
        console.print()
    else:
        print()


def _check_tool_installed(
    tool_name: str, install_url: str | None = None, install_commands: list[str] | None = None
) -> bool:
    """检查指定工具是否已安装

    Args:
        tool_name: 需要检查的命令行工具名称
        install_url: 该工具的安装文档URL
        install_commands: 适用于不同平台的安装命令列表

    Raises:
        ValueError: 当输入参数不符合约定时（非阻断性错误，会继续执行）

    输入假设:
        1. tool_name必须是有效的可执行文件名称
        2. install_commands应为非空列表（当需要显示安装指引时）
        3. 系统环境PATH配置正确，能正确找到已安装工具
    """
    # 参数前置校验
    if not isinstance(tool_name, str) or not tool_name:
        print(f"参数校验失败: tool_name需要非空字符串，收到类型：{type(tool_name)}")
        return False

    if install_commands and (
        not isinstance(install_commands, list) or any(not isinstance(cmd, str) for cmd in install_commands)
    ):
        print("参数校验失败: install_commands需要字符串列表")
        return False

    try:
        check_cmd = ["where", tool_name] if sys.platform == "win32" else ["which", tool_name]
        subprocess.run(check_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True)
        return True
    except subprocess.CalledProcessError:
        print(f"依赖缺失: {tool_name} 未安装")
        if install_url:
            print(f"|-- 安装文档: {install_url}")
        if install_commands:
            print("|-- 可用安装命令:")
            for cmd in install_commands:
                print(f"|   {cmd}")
        return False


def check_deps_installed() -> bool:
    """检查系统环境是否满足依赖要求

    Returns:
        bool: 所有必需依赖已安装返回True，否则False

    输入假设:
        1. GPT_FLAGS全局变量已正确初始化
        2. 当GPT_FLAG_GLOW标志启用时才需要检查glow
        3. Windows系统需要pywin32访问剪贴板
        4. Linux系统需要xclip或xsel工具
    """
    all_installed = True

    # 检查glow（条件性检查）
    if GPT_FLAGS.get(GPT_FLAG_GLOW, False):
        if not _check_tool_installed(
            tool_name="glow",
            install_url="https://github.com/charmbracelet/glow",
            install_commands=[
                "brew install glow  # macOS",
                "choco install glow  # Windows",
                "scoop install glow  # Windows",
                "winget install charmbracelet.glow  # Windows",
            ],
        ):
            all_installed = False

    # 检查剪贴板支持
    if sys.platform == "win32":
        try:
            import win32clipboard  # type: ignore
        except ImportError as e:
            print("剪贴板支持缺失: 需要pywin32包")
            print("解决方案: pip install pywin32")
            all_installed = False
    elif sys.platform == "linux":  # 精确匹配Linux平台
        clipboard_ok = any(
            [
                _check_tool_installed(
                    "xclip",
                    install_commands=[
                        "sudo apt install xclip  # Debian/Ubuntu",
                        "sudo yum install xclip  # RHEL/CentOS",
                    ],
                ),
                _check_tool_installed(
                    "xsel",
                    install_commands=["sudo apt install xsel  # Debian/Ubuntu", "sudo yum install xsel  # RHEL/CentOS"],
                ),
            ]
        )
        if not clipboard_ok:
            all_installed = False

    return all_installed


def get_directory_context_wrapper(tag):
    if tag.command == "treefull":
        text = get_directory_context(1024)
    else:
        text = get_directory_context(1)
    return f"\n[directory tree start]\n{text}\n[directory tree end]\n"


def get_directory_context(max_depth=1):
    """获取当前目录上下文信息（支持动态层级控制）"""
    try:
        current_dir = os.getcwd()

        # Windows系统处理
        if sys.platform == "win32":
            if max_depth == 1:
                # 当max_depth为1时使用dir命令
                dir_result = subprocess.run(["dir"], stdout=subprocess.PIPE, text=True, shell=True, check=True)
                msg = dir_result.stdout or "无法获取目录信息"
                return f"\n当前工作目录: {current_dir}\n\n目录结构:\n{msg}"
            # 其他情况使用tree命令
            cmd = ["tree"]
            if max_depth is not None:
                cmd.extend(["/A", "/F"])
        else:
            # 非Windows系统使用Linux/macOS的tree命令
            cmd = ["tree"]
            if max_depth is not None:
                cmd.extend(["-L", str(max_depth)])
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                text=True,
                shell=(sys.platform == "win32"),
            )
            output = result.stdout
            return f"\n当前工作目录: {current_dir}\n\n目录结构:\n{output}"
        except subprocess.CalledProcessError:
            # 当tree命令失败时使用替代命令
            if sys.platform == "win32":
                # Windows使用dir命令
                dir_result = subprocess.run(["dir"], stdout=subprocess.PIPE, check=True, text=True, shell=True)
                msg = dir_result.stdout or "无法获取目录信息"
            else:
                # 非Windows使用ls命令
                ls_result = subprocess.run(["ls", "-l"], stdout=subprocess.PIPE, text=True, check=True)
                msg = ls_result.stdout or "无法获取目录信息"

            return f"\n当前工作目录: {current_dir}\n\n目录结构:\n{msg}"

    except Exception as e:
        return f"获取目录上下文时出错: {str(e)}"


def get_clipboard_content(_):
    text = get_clipboard_content_string()
    text = f"\n[clipboard content start]\n{text}\n[clipboard content end]\n"
    return text


class ClipboardMonitor:
    def __init__(self, debug=False):
        self.collected_contents = []
        self.should_stop = False
        self.lock = threading.Lock()
        self.monitor_thread = None
        self.debug = debug
        self._debug_print("ClipboardMonitor 初始化完成")

    def _debug_print(self, message):
        """调试信息输出函数"""
        if self.debug:
            print(f"[DEBUG] {message}")

    def _monitor_clipboard(self):
        """后台线程执行的剪贴板监控逻辑"""
        last_content = ""
        initial_content = None  # 用于存储第一次获取的内容
        first_run = True  # 标记是否是第一次运行
        ignore_initial = True  # 标记是否继续忽略初始内容
        self._debug_print("开始执行剪贴板监控线程")
        while not self.should_stop:
            try:
                self._debug_print("尝试获取剪贴板内容...")
                current_content = get_clipboard_content_string()

                if first_run:
                    # 第一次运行，记录初始内容并跳过
                    initial_content = current_content
                    first_run = False
                    self._debug_print("忽略初始剪贴板内容")
                elif current_content and current_content != last_content:
                    # 当内容不为空且与上次不同时
                    if ignore_initial and current_content != initial_content:
                        # 如果还在忽略初始内容阶段，且当前内容不等于初始内容
                        ignore_initial = False  # 停止忽略初始内容
                        self._debug_print("检测到内容变化，停止忽略初始内容")

                    if not ignore_initial or current_content != initial_content:
                        # 如果已经停止忽略初始内容，或者当前内容不等于初始内容
                        with self.lock:
                            print(f"获得片断: ${current_content}")
                            self.collected_contents.append(current_content)
                            self._debug_print(
                                f"已捕获第 {len(self.collected_contents)} 段内容，内容长度: {len(current_content)}"
                            )
                    last_content = current_content
                else:
                    self._debug_print("内容未变化/为空，跳过保存")

                time.sleep(0.5)

            except Exception as e:
                self._debug_print(f"剪贴板监控出错: {str(e)}")
                self._debug_print("异常堆栈信息：")
                traceback.print_exc()
                break

    def start_monitoring(self):
        """启动剪贴板监控"""
        self._debug_print("准备启动剪贴板监控...")
        self.should_stop = False
        self.monitor_thread = threading.Thread(target=self._monitor_clipboard)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        self._debug_print(f"剪贴板监控线程已启动，线程ID: {self.monitor_thread.ident}")
        print("开始监听剪贴板，新复制**新**内容，按回车键结束...")

    def stop_monitoring(self):
        """停止剪贴板监控"""
        self._debug_print("准备停止剪贴板监控...")
        self.should_stop = True

        if self.monitor_thread and self.monitor_thread.is_alive():
            self._debug_print("等待监控线程结束...")
            self.monitor_thread.join(timeout=1)
            if self.monitor_thread.is_alive():
                self._debug_print("警告：剪贴板监控线程未正常退出")
            else:
                self._debug_print("监控线程已正常退出")

    def get_results(self):
        """获取监控结果"""
        self._debug_print("获取监控结果...")
        with self.lock:
            if self.collected_contents:
                result = ""
                for content in self.collected_contents:
                    result += f"\n[clipboard content start]\n{content}\n[clipboard content end]\n"
                self._debug_print(f"返回 {len(self.collected_contents)} 段内容")
                return result
            self._debug_print("未捕获到任何内容")
            return "未捕获到任何剪贴板内容"


def monitor_clipboard(_, debug=False):
    """主函数：启动剪贴板监控并等待用户输入"""
    monitor = ClipboardMonitor(debug=debug)
    monitor.start_monitoring()
    result = ""
    try:
        print("等待用户复制...")
        if sys.platform == "win32":
            import msvcrt

            while not monitor.should_stop:
                if msvcrt.kbhit():
                    if msvcrt.getch() == b"\r":
                        print("检测到回车键")
                        break
                time.sleep(0.1)
        else:
            import select

            while not monitor.should_stop:
                if select.select([sys.stdin], [], [], 0)[0]:
                    if sys.stdin.read(1) == "\n":
                        print("检测到回车键")
                        break
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n用户中断操作")
    finally:
        monitor.stop_monitoring()
        result = monitor.get_results()
    print("已停止监听", result)
    return result


def get_clipboard_content_string():
    """获取剪贴板内容的封装函数，统一返回字符串内容"""
    try:
        if sys.platform == "win32":
            win32clipboard = __import__("win32clipboard")
            win32clipboard.OpenClipboard()
            data = win32clipboard.GetClipboardData()
            win32clipboard.CloseClipboard()
            return data
        if sys.platform == "darwin":
            result = subprocess.run(["pbpaste"], stdout=subprocess.PIPE, text=True, check=True)
            return result.stdout
        else:
            try:
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                )
                return result.stdout
            except FileNotFoundError:
                try:
                    result = subprocess.run(
                        ["xsel", "--clipboard", "--output"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True,
                    )
                    return result.stdout
                except FileNotFoundError:
                    print("未找到 xclip 或 xsel")
                    return "无法获取剪贴板内容：未找到xclip或xsel"
    except Exception as e:
        print(f"获取剪贴板内容时出错: {str(e)}")
        return f"获取剪贴板内容时出错: {str(e)}"


def fetch_url_content(url, is_news=False):
    """通过API获取URL对应的Markdown内容"""
    try:
        api_url = f"http://127.0.0.1:8000/convert?url={url}&is_news={is_news}"
        # 确保不使用任何代理
        session = requests.Session()
        session.trust_env = False  # 禁用从环境变量读取代理
        response = session.get(api_url)
        response.raise_for_status()
        return response.text
    except Exception as e:
        return f"获取URL内容失败: {str(e)}"


def _handle_command(match: CmdNode, cmd_map: Dict[str, Callable]) -> str:
    """处理命令类型匹配

    根据输入的CmdNode或CmdNode列表，执行对应的命令处理函数。

    参数：
        match: 要处理的命令，可以是CmdNode或CmdNode列表
        cmd_map: 命令映射字典，key为命令前缀，value为对应的处理函数

    返回：
        命令处理函数的执行结果
    """
    # 处理单个CmdNode
    return cmd_map[match.command](match)


def _handle_any_script(match: CmdNode) -> str:
    """处理shell命令"""
    script_name = match.command.strip("=")
    file_path = os.path.join("prompts", script_name)
    # 检查文件是否有执行权限
    if not os.access(file_path, os.X_OK):
        # 获取当前文件权限
        current_mode = os.stat(file_path).st_mode
        # 添加用户执行权限
        new_mode = current_mode | stat.S_IXUSR
        # 修改文件权限
        os.chmod(file_path, new_mode)

    try:
        # 直接执行文件
        process = subprocess.Popen(
            f"./{file_path}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = process.communicate()
        output = f"\n\n[shell command]: ./{file_path}\n"
        output += f"[stdout begin]\n{stdout}\n[stdout end]\n"
        if stderr:
            output += f"[stderr begin]\n{stderr}\n[stderr end]\n"
        return output
    except Exception as e:
        return f"\n\n[shell command error]: {str(e)}\n"


def _handle_prompt_file(match: CmdNode) -> str:
    """处理prompts目录文件"""
    file_path = os.path.join(PROMPT_DIR, match.command)

    # 检查文件是否有可执行权限或以#!开头
    if os.access(file_path, os.X_OK):
        # 如果有可执行权限，则作为shell命令处理
        return _handle_any_script(match)

    # 检查文件是否以#!开头
    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline()
        if first_line.startswith("#!"):
            # 如果以#!开头，也作为shell命令处理
            return _handle_any_script(match)
        # 否则读取整个文件内容作为普通文件处理
        content = first_line + f.read()
        return f"\n{content}\n"


def _handle_local_file(match: CmdNode) -> str:
    """处理本地文件路径"""
    expanded_path, line_range_match = _expand_file_path(match.command)

    if os.path.isfile(expanded_path):
        return _process_single_file(expanded_path, line_range_match)
    elif os.path.isdir(expanded_path):
        return _process_directory(expanded_path)
    else:
        return f"\n\n[error]: 路径不存在 {expanded_path}\n\n"


def _expand_file_path(command: str) -> tuple:
    """展开文件路径并解析行号范围"""
    line_range_match = re.search(r":(\d+)?-(\d+)?$", command)
    expanded_path = os.path.abspath(
        os.path.expanduser(command[: line_range_match.start()] if line_range_match else command)
    )
    return expanded_path, line_range_match


def _process_single_file(file_path: str, line_range_match: re.Match) -> str:
    """处理单个文件内容"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = _read_file_content(f, line_range_match)
    except UnicodeDecodeError:
        content = "二进制文件或无法解码"
    except Exception as e:
        return f"\n\n[error]: 无法读取文件 {file_path}: {str(e)}\n\n"

    return _format_file_content(file_path, content)


def _read_file_content(file_obj, line_range_match: re.Match) -> str:
    """读取文件内容并处理行号范围"""
    lines = file_obj.readlines()
    if not line_range_match:
        return "".join(lines)

    start_str = line_range_match.group(1)
    end_str = line_range_match.group(2)
    start = int(start_str) - 1 if start_str else 0
    end = int(end_str) if end_str else len(lines)
    start = max(0, start)
    end = min(len(lines), end)
    return "".join(lines[start:end])


def _format_file_content(file_path: str, content: str) -> str:
    """格式化文件内容输出"""
    return f"\n\n[file name]: {file_path}\n[file content begin]\n{content}\n[file content end]\n\n"


def _process_directory(dir_path: str) -> str:
    """处理目录内容"""
    gitignore_path = _find_gitignore(dir_path)
    root_dir = os.path.dirname(gitignore_path) if gitignore_path else dir_path
    is_ignored = _parse_gitignore(gitignore_path, root_dir)

    replacement = f"\n\n[directory]: {dir_path}\n"
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d))]
        for file in files:
            file_path = os.path.join(root, file)
            if is_ignored(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    replacement += _format_file_content(file_path, content)
            except UnicodeDecodeError:
                replacement += (
                    f"[file name]: {file_path}\n[file content begin]\n二进制文件或无法解码\n[file content end]\n\n"
                )
            except Exception as e:
                replacement += f"[file error]: 无法读取文件 {file_path}: {str(e)}\n\n"
    replacement += f"[directory end]: {dir_path}\n\n"
    return replacement


def _find_gitignore(path: str) -> str:
    """向上查找最近的.gitignore文件"""
    current = os.path.abspath(path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        gitignore = os.path.join(parent, ".gitignore")
        if os.path.isfile(gitignore):
            return gitignore
        current = parent


def _parse_gitignore(gitignore_path: str, root_dir: str) -> callable:
    """解析.gitignore文件生成过滤函数"""
    patterns = []
    if gitignore_path and os.path.isfile(gitignore_path):
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except Exception as e:
            logging.warning(f"解析.gitignore失败: {str(e)}")

    default_patterns = [
        "__pycache__/",
        "node_modules/",
        "venv/",
        "dist/",
        "build/",
        "*.py[cod]",
        "*.so",
        "*.egg-info",
        "*.jpg",
        "*.jpeg",
        "*.png",
        "*.gif",
        "*.pdf",
        "*.zip",
        ".*",
    ]
    patterns.extend(default_patterns)

    def _is_ignored(file_path: str) -> bool:
        """判断文件路径是否被忽略"""
        try:
            rel_path = os.path.relpath(file_path, root_dir)
            rel_posix = rel_path.replace(os.sep, "/")

            for pattern in patterns:
                pattern = pattern.rstrip("/")
                if (
                    fnmatch.fnmatch(rel_posix, pattern)
                    or fnmatch.fnmatch(rel_posix, f"{pattern}/*")
                    or fnmatch.fnmatch(os.path.basename(file_path), pattern)
                ):
                    return True
        except ValueError:
            pass
        return False

    return _is_ignored


def _handle_url(match: CmdNode) -> str:
    """处理URL请求"""
    url = match.command[4:] if match.command.startswith("read") else match.command
    markdown_content = fetch_url_content(url, is_news=match.command.startswith("read"))
    return f"\n\n[reference url, content converted to markdown]: {url} \n[markdown content begin]\n{markdown_content}\n[markdown content end]\n\n"


def read_last_query(_):
    """读取最后一次查询的内容"""
    try:
        with open(LAST_QUERY_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def generate_patch_prompt(symbol_name, symbol_map, patch_require=False, file_ranges=None):
    """生成多符号补丁提示词字符串

    参数:
        symbol_names: 符号名称列表
        symbol_map: 包含符号信息的字典，key为符号名称，value为补丁信息字典
        patch_require: 是否需要生成修改指令
        file_ranges: 文件范围字典 {文件路径: {"range": 范围描述, "content": 字节内容}}

    输入假设:
        1. symbol_map中的文件路径必须真实存在
        2. file_ranges中的content字段必须可utf-8解码
        3. 当patch_require=True时用户会提供具体修改要求
    """
    prompt = ""

    if patch_require:
        prompt += """
# 任务说明
1. 积极帮助用户处理遇到的问题，提供超预期的解决方案
2. 主要是处理代码, 消除bug, 增加新功能，重构，或者用户要求的其它修改
3. 修改完代码要验证是否正确的解决了问题
4. 根据任务的需要增加，删除，拼接，改写原来的符号或者块

# 代码编写规范
1. 编写符合工业标准的高质量代码
2. 如果语言支持，就总是使用强类型
3. 高内聚，低耦合，易扩展
4. 多使用有意义的小函数，减少重复片段
5. 接口要便于编写单元测试
6. 在doc string里列出可能的输入假设，不符合要打日志，退出流程
7. 能复用就不手写
8. 函数参数不要太长，参数不要太多
9. 实现类时需要便于调试
10. 如果提供了__import__符号则可在此加入包导入语句, 否则不加入包导入语句，用户会自行处理

# 指令说明
1. 必须返回结构化内容，使用严格指定的标签格式
2. 若无修改需求，则忽视传入的符号或者块
3. 修改时必须包含完整文件内容，不得省略任何代码
4. 保持原有缩进和代码风格，不添注释
5. 输出必须为纯文本，禁止使用markdown或代码块
6. 允许在符号内容在前后添加新代码
7. 在非正式输出部分使用[modified symbol]需要转义成[modify symbol]
"""
    if not patch_require:
        prompt += "现有代码库里的一些符号和代码块:\n"
    if patch_require and symbol_name.args:
        prompt += """\
8. 可以修改任意符号，一个或者多个，但必须返回符号的完整路径，做为区分
9. 只输出你修改的那个符号
"""
    # 添加符号信息
    for symbol_name in symbol_name.args:
        patch_dict = symbol_map[symbol_name]
        prompt += f"""
[SYMBOL START]
符号名称: {symbol_name}
文件路径: {patch_dict["file_path"]}

[CONTENT START]
{patch_dict["block_content"].decode('utf-8')}
[CONTENT END]

[SYMBOL END]
"""

    # 添加文件范围信息
    if patch_require and file_ranges:
        prompt += """\
8. 可以修改任意块，一个或者多个，但必须返回块的完整路径，做为区分
9. 只输出你修改的那个块
"""
        for file_path, range_info in file_ranges.items():
            prompt += f"""
[FILE RANGE START]
文件路径: {file_path}:{range_info['range'][0]}-{range_info['range'][1]}

[CONTENT START]
{range_info['content'].decode('utf-8') if isinstance(range_info['content'], bytes) else range_info['content']}
[CONTENT END]

[FILE RANGE END]
"""

    if patch_require:
        prompt += (
            """
# 响应格式
[modified block]: 块路径
[source code start]
完整文件内容
[source code end]

或（无修改时）:
[modified block]: 块路径
[source code start]
完整原始内容
[source code end]

用户的要求如下:
"""
            if file_ranges
            else """
# 响应格式
[modified symbol]: 符号路径
[source code start]
完整文件内容
[source code end]

或（无修改时）:
[modified symbol]: 符号路径
[source code start]
完整原始内容
[source code end]

用户的要求如下:
"""
        )
    return prompt


class BlockPatchResponse:
    """大模型响应解析器"""

    def __init__(self, symbol_names=None):
        self.symbol_names = symbol_names

    def parse(self, response_text):
        """
        解析大模型返回的响应内容
        返回格式: [(identifier, source_code), ...]
        """
        import re

        results = []
        pending_code = []  # 暂存未注册符号的代码片段

        # 匹配两种响应格式
        pattern = re.compile(
            r"\[modified (symbol|block)\]:\s*([^\n]+)\s*\[source code start\](.*?)\[source code end\]", re.DOTALL
        )

        for match in pattern.finditer(response_text):
            section_type, identifier, source_code = match.groups()
            identifier = identifier.strip()
            source_code = source_code.strip()

            if section_type == "symbol":
                # 处理未注册符号的暂存逻辑
                if self.symbol_names is not None and identifier not in self.symbol_names:
                    pending_code.append(source_code)
                    continue

                # 合并暂存代码到当前合法符号
                combined_source = "\n".join(pending_code + [source_code]) if pending_code else source_code
                pending_code = []
                results.append((identifier, combined_source))
            else:
                # 块类型直接添加不处理暂存
                results.append((identifier, source_code))

        # 兼容旧格式校验
        if not results and ("[source code start]" in response_text or "[source code end]" in response_text):
            raise ValueError("响应包含代码块标签但格式不正确，请使用[modified symbol/block]:标签")

        return results

    def _extract_source_code(self, text):
        """提取源代码内容（保留旧方法兼容异常处理）"""
        start_tag = "[source code start]"
        end_tag = "[source code end]"

        start_idx = text.find(start_tag)
        end_idx = text.find(end_tag)

        if start_idx == -1 or end_idx == -1:
            raise ValueError("源代码块标签不完整")

        return text[start_idx + len(start_tag) : end_idx].strip()


def parse_llm_response(response_text, symbol_names=None):
    """
    快速解析响应内容
    返回格式: [(symbol_name, source_code), ...]
    """
    parser = BlockPatchResponse(symbol_names=symbol_names)
    return parser.parse(response_text)


def process_patch_response(response_text, symbol_detail):
    """
    处理大模型的补丁响应，生成差异并应用补丁

    参数:
        response_text: 大模型返回的响应文本（可能包含<thinking>标签）
        symbol_detail: 要处理的符号

    返回:
        如果用户确认应用补丁，则返回修改后的代码(bytes)
        否则返回None
    """
    # 过滤掉<thinking>标签内容（包含多行情况）
    filtered_response = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()  # 解析大模型响应
    results = parse_llm_response(filtered_response, symbol_detail.keys())

    # 准备BlockPatch参数
    file_paths = []
    patch_ranges = []
    block_contents = []
    update_contents = []

    # 遍历解析结果，构造参数
    for symbol_name, source_code in results:
        # 获取符号详细信息
        detail = symbol_detail[symbol_name]
        file_paths.append(detail["file_path"])
        patch_ranges.append(detail["block_range"])
        block_contents.append(detail["block_content"])
        update_contents.append(source_code.encode("utf-8"))

    # 创建BlockPatch对象
    patch = BlockPatch(
        file_paths=file_paths,
        patch_ranges=patch_ranges,
        block_contents=block_contents,
        update_contents=update_contents,
    )

    # 生成并显示差异
    diff = patch.generate_diff()
    highlighted_diff = highlight(diff, DiffLexer(), TerminalFormatter())
    print("\n高亮显示的diff内容：")
    print(highlighted_diff)

    # 询问用户是否应用补丁
    user_input = input("\n是否应用此补丁？(y/n): ").lower()
    if user_input == "y":
        file_map = patch.apply_patch()
        for file in file_map:
            with open(file, "wb+") as f:
                f.write(file_map[file])
        print("补丁已成功应用")
        return file_map
    else:
        print("补丁未应用")
        return None


def test_patch_response():
    """测试补丁响应处理功能"""
    # 读取前面生成的测试文件
    with open("diff_test.json", "rb") as f:
        args = marshal.load(f)

    process_patch_response(*args)


def find_nearest_newline(position: int, content: str, direction: str = "forward") -> int:
    """查找指定位置向前/向后的第一个换行符位置

    参数:
        position: 起始位置(包含)
        content: 要搜索的文本内容
        direction: 搜索方向 'forward' 或 'backward'

    返回:
        找到的换行符索引(从0开始)，未找到返回原position

    假设:
        - position在0到len(content)-1之间
        - direction只能是'forward'或'backward'
        - content不为空
    """
    if direction not in ("forward", "backward"):
        raise ValueError("Invalid direction, must be 'forward' or 'backward'")

    max_pos = len(content) - 1
    step = 1 if direction == "forward" else -1
    end = max_pos + 1 if direction == "forward" else -1

    for i in range(position, end, step):
        if content[i] == "\n":
            return i
    return position


def move_forward_from_position(current_pos: int, content: str) -> int:
    """从当前位置向前移动到下一个换行符之后的位置

    参数:
        current_pos: 当前光标位置
        content: 文本内容

    返回:
        新位置，如果到达文件末尾则返回len(content)

    假设:
        - current_pos在0到len(content)之间
        - content长度至少为1
    """
    if current_pos >= len(content):
        return current_pos

    newline_pos = find_nearest_newline(current_pos, content, "forward")
    return newline_pos + 1 if newline_pos != current_pos else len(content)


def patch_symbol_with_prompt(symbol_names: CmdNode):
    """获取符号的纯文本内容

    参数:
        symbol_names: CmdNode对象，包含要查询的符号名称列表

    返回:
        符号对应的纯文本内容
    """
    symbol_map = {}
    for symbol_name in symbol_names.args:
        symbol_result = get_symbol_detail(symbol_name)
        if len(symbol_result) == 1:
            symbol_name = symbol_result[0].get("symbol_name", symbol_name)
            symbol_map[symbol_name] = symbol_result[0]
        else:
            for symbol in symbol_result:
                symbol_map[symbol["symbol_name"]] = symbol
    GPT_VALUE_STORAGE[GPT_SYMBOL_PATCH] = symbol_map
    return generate_patch_prompt(
        CmdNode(command="symbol", args=list(symbol_map.keys())), symbol_map, GPT_FLAGS.get(GPT_FLAG_PATCH)
    )


def get_symbol_detail(symbol_names: str) -> list:
    """使用公共http函数请求符号补丁并生成BlockPatch对象

    输入假设:
    - symbol_names格式应为以下两种形式之一:
        - 多符号: "file.c/a,b,c" (使用逗号分隔多个符号)
        - 单符号: "file.c/a"
    - 环境变量GPT_SYMBOL_API_URL存在，否则使用默认值
    - API响应包含完整的symbol_data字段(content, location, file_path等)
    - 当存在特殊标记时才会验证文件内容一致性

    返回:
        list: 包含处理结果的字典列表，每个元素包含symbol详细信息
    """
    symbol_list = _parse_symbol_names(symbol_names)
    api_url = os.getenv("GPT_SYMBOL_API_URL", "http://127.0.0.1:9050")
    batch_response = _send_http_request(_build_api_url(api_url, symbol_names))
    if GPT_FLAGS.get(GPT_FLAG_CONTEXT):
        return [_process_symbol_data(symbol_data, "") for _, symbol_data in enumerate(batch_response)]
    else:
        return [_process_symbol_data(symbol_data, symbol_list[idx]) for idx, symbol_data in enumerate(batch_response)]


def _parse_symbol_names(symbol_names: str) -> list:
    """解析符号名称字符串为规范的符号列表

    输入假设:
    - 多符号格式必须包含'/'和','分隔符 (如file.c/a,b,c)
    - 单符号格式可以没有逗号分隔符
    - 非法格式会抛出ValueError异常
    """
    if "/" in symbol_names and "," in symbol_names:
        pos = symbol_names.rfind("/")
        if pos < 0:
            raise ValueError(f"Invalid symbol format: {symbol_names}")
        return [f"{symbol_names[:pos+1]}{symbol}" for symbol in symbol_names[pos + 1 :].split(",")]
    return [symbol_names]


def _build_api_url(api_url: str, symbol_names: str) -> str:
    """构造批量请求的API URL"""
    encoded_symbols = requests.utils.quote(symbol_names)
    lsp_enabled = GPT_FLAGS.get(GPT_FLAG_CONTEXT)
    return f"{api_url}/symbol_content?symbol_path=symbol:{encoded_symbols}&json=true&lsp_enabled={lsp_enabled}"


def _process_symbol_data(symbol_data: dict, symbol_name: str) -> dict:
    """处理单个symbol的响应数据为规范格式

    输入假设:
    - symbol_data必须包含content, location, file_path字段
    - location字段必须包含start_line/start_col和end_line/end_col
    """
    location = symbol_data["location"]
    if not symbol_name:
        symbol_name = f"{symbol_data["file_path"]}/{symbol_data["name"]}"
    return {
        "symbol_name": symbol_name,
        "file_path": symbol_data["file_path"],
        "code_range": ((location["start_line"], location["start_col"]), (location["end_line"], location["end_col"])),
        "block_range": location["block_range"],
        "block_content": symbol_data["content"].encode("utf-8"),
    }


def _fetch_symbol_data(symbol_name, file_path=None):
    """获取符号数据"""
    # 从环境变量获取API地址
    api_url = os.getenv("GPT_SYMBOL_API_URL", "http://127.0.0.1:9050")
    url = f"{api_url}/symbols/{symbol_name}/context?max_depth=2" + (f"&file_path={file_path}" if file_path else "")

    # 使用公共函数发送请求
    return _send_http_request(url)


def _send_http_request(url, is_plain_text=False):
    """发送HTTP请求的公共函数
    Args:
        url: 请求的URL
        is_plain_text: 是否返回纯文本内容，默认为False返回JSON
    """
    # 禁用所有代理
    proxies = {"http": None, "https": None, "http_proxy": None, "https_proxy": None, "all_proxy": None}
    # 同时清除环境变量中的代理设置
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("all_proxy", None)

    response = requests.get(url, proxies=proxies, timeout=5)
    response.raise_for_status()

    return response.text if is_plain_text else response.json()


def query_symbol(symbol_name):
    """查询符号定义信息，优化上下文长度"""
    # 如果符号名包含斜杠，则分离路径和符号名
    if "/" in symbol_name:
        parts = symbol_name.split("/")
        symbol_name = parts[-1]  # 最后一部分是符号名
        file_path = "/".join(parts[:-1])  # 前面部分作为文件路径
    else:
        file_path = None
    try:
        data = _fetch_symbol_data(symbol_name, file_path)
        # 构建上下文
        context = "\n[symbol context start]\n"
        context += f"符号名称: {data['symbol_name']}\n"

        # 查找当前符号的定义
        if data["definitions"]:
            # 查找匹配的定义
            matching_definitions = [d for d in data["definitions"] if d["name"] == symbol_name]
            if matching_definitions:
                # 将匹配的定义移到最前面
                main_definition = matching_definitions[0]
                data["definitions"].remove(main_definition)
                data["definitions"].insert(0, main_definition)
            else:
                # 如果没有完全匹配的，使用第一个定义
                main_definition = data["definitions"][0]

            # 显示主要定义
            context += "\n[main definition start]\n"
            context += f"函数名: {main_definition['name']}\n"
            context += f"文件路径: {main_definition['file_path']}\n"
            context += f"完整定义:\n{main_definition['full_definition']}\n"
            context += "[main definition end]\n"

        # 计算剩余可用长度
        remaining_length = MAX_PROMPT_SIZE - len(context) - 1024  # 保留1024字符余量

        # 添加其他定义，直到达到长度限制
        if len(data["definitions"]) > 1 and remaining_length > 0:
            context += "\n[other definitions start]\n"
            for definition in data["definitions"][1:]:
                definition_text = (
                    f"\n[function definition start]\n"
                    f"函数名: {definition['name']}\n"
                    f"文件路径: {definition['file_path']}\n"
                    f"完整定义:\n{definition['full_definition']}\n"
                    "[function definition end]\n"
                )
                if len(definition_text) > remaining_length:
                    break
                context += definition_text
                remaining_length -= len(definition_text)
            context += "[other definitions end]\n"

        context += "[symbol context end]\n"
        return context

    except requests.exceptions.RequestException as e:
        return f"\n[error] 符号查询失败: {str(e)}\n"
    except KeyError as e:
        return f"\n[error] 无效的API响应格式: {str(e)}\n"
    except Exception as e:
        return f"\n[error] 符号查询时发生错误: {str(e)}\n"


@dataclass
class ProjectSections:
    project_design: str
    readme: str
    dir_tree: str
    setup_script: str
    api_description: str
    # 可以根据需要扩展更多字段


def parse_project_text(text: str) -> ProjectSections:
    """
    从输入文本中提取结构化项目数据

    参数：
    text: 包含标记的原始文本

    返回：
    ProjectSections对象，可通过成员访问各字段内容
    """
    pattern = r"[(\w+)_START\](.*?)\[\1_END\]"
    matches = re.findall(pattern, text, re.DOTALL)

    section_dict = {}
    for name, content in matches:
        # 转换为小写蛇形命名，如 PROJECT_DESIGN -> project_design
        key = name.lower()
        section_dict[key] = content.strip()

    # 验证必要字段
    required_fields = {"project_design", "readme", "dir_tree", "setup_script", "api_description"}
    if not required_fields.issubset(section_dict.keys()):
        missing = required_fields - section_dict.keys()
        raise ValueError(f"缺少必要字段: {', '.join(missing)}")

    return ProjectSections(**section_dict)


# 定义正则表达式常量
CMD_PATTERN = r"(?<!\\)@[^ \u3000]+"  # 匹配@命令，排除转义@、英文空格和中文全角空格


class GPTContextProcessor:
    """文本处理类，封装所有文本处理相关功能"""

    def __init__(self):
        self.cmd_map = self._initialize_cmd_map()
        self.env_vars = {
            "os": sys.platform,
            "os_version": platform.version(),
            "current_path": os.getcwd(),
        }
        self.current_length = 0
        self._add_gpt_flags()

    def _initialize_cmd_map(self):
        """初始化命令映射表"""
        return {
            "clipboard": self.get_clipboard_content,
            "listen": self.monitor_clipboard,
            "tree": self.get_directory_context_wrapper,
            "treefull": self.get_directory_context_wrapper,
            "last": self.read_last_query,
            "symbol": self.patch_symbol_with_prompt,
        }

    def _add_gpt_flags(self):
        """添加GPT flags相关处理函数"""

        def update_gpt_flag(cmd):
            """更新GPT标志的函数"""
            GPT_FLAGS.update({cmd.command: True})
            return ""

        for flag in GPT_FLAGS:
            self.cmd_map[flag] = update_gpt_flag

    def preprocess_text(self, text) -> List[Union[TextNode, CmdNode, TemplateNode]]:
        """预处理文本，将文本按{}分段，并提取@命令"""
        result = []
        cmd_groups = defaultdict(list)

        # 首先按{}分割文本
        segments = re.split(r"({.*?})", text)

        for segment in segments:
            if segment.startswith("{") and segment.endswith("}"):  # 处理模板段
                template_content = segment.strip("{}")
                # 直接匹配所有命令
                commands = [CmdNode(command=cmd.lstrip("@")) for cmd in re.findall(CMD_PATTERN, template_content)]
                if commands:
                    result.append(TemplateNode(template=commands[0], commands=commands[1:]))
            else:  # 处理非模板段
                # 先匹配所有命令
                commands = re.findall(CMD_PATTERN, segment)
                # 将命令之间的文本作为普通文本处理
                text_parts = re.split(CMD_PATTERN, segment)
                for i, part in enumerate(text_parts):
                    if part:  # 处理普通文本
                        # 处理转义的@符号
                        part = part.replace("\\@", "@")
                        result.append(TextNode(content=part))
                    if i < len(commands):  # 处理命令
                        cmd = commands[i].lstrip("@")
                        if ":" in cmd and not cmd.startswith("http"):
                            symbol, _, arg = cmd.partition(":")
                            cmd_groups[symbol].append(arg)
                        else:
                            result.append(CmdNode(command=cmd))

        # 处理带参数的命令
        last_cmd_index = -1
        # 查找最后一个CmdNode的位置
        for i, node in enumerate(result):
            if isinstance(node, CmdNode):
                last_cmd_index = i

        for symbol, args in cmd_groups.items():
            if last_cmd_index != -1:
                # 在最后一个命令后插入
                result.insert(last_cmd_index + 1, CmdNode(command=symbol, args=args))
            else:
                # 如果没有找到命令，插入到第一位
                result.insert(0, CmdNode(command=symbol, args=args))
        return result

    def process_text_with_file_path(self, text: str) -> str:
        """处理包含@...的文本"""
        parts = self.preprocess_text(text)
        for i, node in enumerate(parts):
            if isinstance(node, TextNode):
                parts[i] = node.content
                self.current_length += len(node.content)
            elif isinstance(node, CmdNode):
                processed_text = self._process_match(node)
                parts[i] = processed_text
                self.current_length += len(processed_text)
            elif isinstance(node, TemplateNode):
                template_replacement = self._process_match(node.template)
                args = []
                for template_cmd in node.commands:
                    arg_replacement = self._process_match(template_cmd)
                    if arg_replacement:
                        args.append(arg_replacement)
                replacement = template_replacement.format(*args)
                parts[i] = replacement
                self.current_length += len(replacement)
            else:
                raise ValueError(f"无法识别的部分类型: {type(node)}")

        return self._finalize_text("".join(parts))

    def _process_match(self, match: CmdNode) -> Tuple[str]:
        """处理单个匹配项或匹配项列表"""
        try:
            return self._get_replacement(match)
        except Exception as e:
            error_match = " ".join([m.command for m in match]) if isinstance(match, list) else match.command
            handle_processing_error(error_match, e)

    def _get_replacement(self, match: CmdNode):
        """根据匹配类型获取替换内容"""
        if is_prompt_file(match.command):
            return _handle_prompt_file(match)
        elif is_local_file(match.command):
            return _handle_local_file(match)
        elif is_url(match.command):
            return _handle_url(match)
        elif self._is_command(match.command):
            return _handle_command(match, self.cmd_map)
        return ""

    def _finalize_text(self, text):
        """最终处理文本"""
        truncated_suffix = "\n[输入太长内容已自动截断]"
        if len(text) > MAX_PROMPT_SIZE:
            text = text[: MAX_PROMPT_SIZE - len(truncated_suffix)] + truncated_suffix

        with open(LAST_QUERY_FILE, "w+", encoding="utf8") as f:
            f.write(text)
        return text

    def _is_command(self, match):
        """判断是否为命令"""
        return any(match.startswith(cmd) for cmd in self.cmd_map) and not os.path.exists(match)

    @staticmethod
    def get_clipboard_content(_):
        """获取剪贴板内容"""
        text = get_clipboard_content_string()
        return f"\n[clipboard content start]\n{text}\n[clipboard content end]\n"

    @staticmethod
    def monitor_clipboard(_, debug=False):
        """监控剪贴板内容"""
        return monitor_clipboard(_, debug)

    @staticmethod
    def get_directory_context_wrapper(tag):
        """获取目录上下文"""
        return get_directory_context_wrapper(tag)

    @staticmethod
    def read_last_query(_):
        """读取最后一次查询内容"""
        return read_last_query(_)

    @staticmethod
    def patch_symbol_with_prompt(symbol_names):
        """处理符号补丁提示"""
        return patch_symbol_with_prompt(symbol_names)


GPT_FLAG_GLOW = "glow"
GPT_FLAG_EDIT = "edit"
GPT_FLAG_PATCH = "patch"
GPT_SYMBOL_PATCH = "patch"
GPT_FLAG_CONTEXT = "context"

GPT_FLAGS = {GPT_FLAG_GLOW: False, GPT_FLAG_EDIT: False, GPT_FLAG_PATCH: False, GPT_FLAG_CONTEXT: False}
GPT_VALUE_STORAGE = {GPT_SYMBOL_PATCH: False}


def finalize_text(text):
    """最终处理文本"""
    truncated_suffix = "\n[输入太长内容已自动截断]"
    if len(text) > MAX_PROMPT_SIZE:
        text = text[: MAX_PROMPT_SIZE - len(truncated_suffix)] + truncated_suffix

    with open(LAST_QUERY_FILE, "w+", encoding="utf8") as f:
        f.write(text)
    return text


def is_command(match, cmd_map):
    """判断是否为命令"""
    return any(match.startswith(cmd) for cmd in cmd_map) and not os.path.exists(match)


def is_prompt_file(match):
    """判断是否为prompt文件"""
    return os.path.exists(os.path.join(PROMPT_DIR, match))


def is_local_file(match):
    """判断是否为本地文件"""
    # 如果匹配包含行号范围（如:10-20），先去掉行号部分再判断
    if re.search(r":(\d+)?-(\d+)?$", match):
        match = re.sub(r":(\d+)?-(\d+)?$", "", match)
    return os.path.exists(os.path.expanduser(match))


def is_url(match):
    """判断是否为URL"""
    return match.startswith(("http", "read"))


def handle_processing_error(match, error):
    """统一错误处理"""
    print(f"处理 {match} 时出错: {str(error)}")
    traceback.print_exc()  # 打印完整的调用栈信息
    sys.exit(1)


# 获取.shadowroot的绝对路径，支持~展开
shadowroot = Path(os.path.expanduser("~/.shadowroot"))


def _save_response_content(content):
    """保存原始响应内容到response.md"""
    response_path = shadowroot / Path("response.md")
    response_path.parent.mkdir(parents=True, exist_ok=True)
    with open(response_path, "w+", encoding="utf-8") as dst:
        dst.write(content)
    return response_path


def _extract_file_matches(content):
    """从内容中提取文件匹配项"""
    return re.findall(
        r"\[(?:modified|created) file\]: (.*?)\n\[source code start\] *?\n(.*?)\n\[source code end\]", content, re.S
    )


def _process_file_path(file_path):
    """处理文件路径，将绝对路径转换为相对路径"""
    if file_path.is_absolute():
        parts = file_path.parts[1:]
        return Path(*parts)
    return file_path


def _save_file_to_shadowroot(shadow_file_path, file_content):
    """将文件内容保存到shadowroot目录"""
    shadow_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(shadow_file_path, "w", encoding="utf-8") as f:
        f.write(file_content)
    print(f"已保存文件到: {shadow_file_path}")


def _generate_unified_diff(old_file_path, shadow_file_path, original_content, file_content):
    """生成unified diff"""
    return difflib.unified_diff(
        original_content.splitlines(),
        file_content.splitlines(),
        fromfile=str(old_file_path),
        tofile=str(shadow_file_path),
        lineterm="",
    )


def _save_diff_content(diff_content):
    """将diff内容保存到文件"""
    if diff_content:
        diff_file = shadowroot / "changes.diff"
        with open(diff_file, "w", encoding="utf-8") as f:
            f.write(diff_content)
        print(f"已生成diff文件: {diff_file}")
        return diff_file
    return None


def _display_and_apply_diff(diff_file, auto_apply=False):
    """显示并应用diff"""
    if diff_file.exists():
        with open(diff_file, "r", encoding="utf-8") as f:
            diff_text = f.read()
            highlighted_diff = highlight(diff_text, DiffLexer(), TerminalFormatter())
            print("\n高亮显示的diff内容：")
            print(highlighted_diff)

        if auto_apply:
            print("自动应用变更...")
            _apply_patch(diff_file)
        else:
            print(f"\n申请变更文件，是否应用 {diff_file}？")
            apply = input("输入 y 应用，其他键跳过: ").lower()
            if apply == "y":
                _apply_patch(diff_file)


def _apply_patch(diff_file):
    """应用patch的公共方法"""
    try:
        subprocess.run(["patch", "-p0", "-i", str(diff_file)], check=True)
        print("已成功应用变更")
    except subprocess.CalledProcessError as e:
        print(f"应用变更失败: {e}")


def extract_and_diff_files(content, auto_apply=False):
    """从内容中提取文件并生成diff"""
    _save_response_content(content)
    matches = _extract_file_matches(content)
    if not matches:
        return

    diff_content = ""
    for filename, file_content in matches:
        file_path = Path(filename.strip()).absolute()
        old_file_path = file_path
        if not old_file_path.exists():
            old_file_path.parent.mkdir(parents=True, exist_ok=True)
            old_file_path.touch()
        file_path = _process_file_path(file_path)
        shadow_file_path = shadowroot / file_path
        _save_file_to_shadowroot(shadow_file_path, file_content)
        original_content = ""
        print("debug", old_file_path)
        with open(str(old_file_path), "r", encoding="utf8") as f:
            original_content = f.read()
        diff = _generate_unified_diff(old_file_path, shadow_file_path, original_content, file_content)
        diff_content += "\n".join(diff) + "\n\n"

    diff_file = _save_diff_content(diff_content)
    if diff_file:
        _display_and_apply_diff(diff_file, auto_apply=auto_apply)


def process_response(prompt, response_data, file_path, save=True, obsidian_doc=None, ask_param=None):
    """处理API响应并保存结果"""
    if not response_data["choices"]:
        raise ValueError("API返回空响应")

    content = response_data["choices"][0]["message"]["content"]

    # 处理文件路径
    file_path = Path(file_path)
    if save and file_path:
        with open(file_path, "w+", encoding="utf8") as f:
            # 删除<think>...</think>内容
            cleaned_content = re.sub(r"<think>\n?.*?\n?</think>\n*", "", content, flags=re.DOTALL)
            f.write(cleaned_content)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as tmp_file:
        tmp_file.write(content)
        save_path = tmp_file.name

    # 处理Obsidian文档存储
    if obsidian_doc:
        obsidian_dir = Path(obsidian_doc)
        obsidian_dir.mkdir(parents=True, exist_ok=True)

        # 创建按年月分组的子目录
        now = time.localtime()
        month_dir = obsidian_dir / f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}"
        month_dir.mkdir(exist_ok=True)

        # 生成时间戳文件名
        timestamp = f"{now.tm_hour}-{now.tm_min}-{now.tm_sec}.md"
        obsidian_file = month_dir / timestamp

        # 格式化内容：将非空思维过程渲染为绿色，去除背景色
        formatted_content = re.sub(
            r"<think>\n*([\s\S]*?)\n*</think>",
            lambda match: '<div style="color: #228B22; padding: 10px; border-radius: 5px; margin: 10px 0;">'
            + match.group(1).replace("\n", "<br>")
            + "</div>",
            content,
            flags=re.DOTALL,
        )

        # 添加提示词
        if prompt:
            formatted_content = f"### 问题\n\n```\n{prompt}\n```\n\n### 回答\n{formatted_content}"

        # 写入响应内容
        with open(obsidian_file, "w", encoding="utf-8") as f:
            f.write(formatted_content)

        # 更新main.md
        main_file = obsidian_dir / f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-索引.md"
        link_name = re.sub(r"[{}]", "", ask_param[:256]) if ask_param else timestamp
        link = f"[[{month_dir.name}/{timestamp}|{link_name}]]\n"

        with open(main_file, "a", encoding="utf-8") as f:
            f.write(link)

    if not check_deps_installed():
        sys.exit(1)

    if GPT_FLAGS.get(GPT_FLAG_GLOW):
        # 调用提取和diff函数
        try:
            subprocess.run(["glow", save_path], check=True)
            # 如果是临时文件，使用后删除
            if not save:
                os.unlink(save_path)
        except subprocess.CalledProcessError as e:
            print(f"glow运行失败: {e}")

    if GPT_FLAGS.get(GPT_FLAG_EDIT):
        extract_and_diff_files(content)
    if GPT_FLAGS.get(GPT_FLAG_PATCH):
        process_patch_response(content, GPT_VALUE_STORAGE[GPT_SYMBOL_PATCH])


def validate_environment():
    """验证必要的环境变量"""
    api_key = os.getenv("GPT_KEY")
    if not api_key:
        print("错误：未设置GPT_KEY环境变量")
        sys.exit(1)

    base_url = os.getenv("GPT_BASE_URL")
    if not base_url:
        print("错误：未设置GPT_BASE_URL环境变量")
        sys.exit(1)

    try:
        parsed_url = urlparse(base_url)
        if not all([parsed_url.scheme, parsed_url.netloc]):
            print(f"错误：GPT_BASE_URL不是有效的URL: {base_url}")
            sys.exit(1)
    except Exception as e:
        print(f"错误：解析GPT_BASE_URL失败: {e}")
        sys.exit(1)


def validate_files(program_args):
    """验证输入文件是否存在"""
    if not program_args.ask and not program_args.chatbot:  # 仅在未使用--ask参数时检查文件
        if not os.path.isfile(program_args.file):
            print(f"错误：源代码文件不存在 {program_args.file}")
            sys.exit(1)

        if not os.path.isfile(program_args.prompt_file):
            print(f"错误：提示词文件不存在 {program_args.prompt_file}")
            sys.exit(1)


def print_proxy_info(proxies, proxy_sources):
    """打印代理配置信息"""
    if proxies:
        print("⚡ 检测到代理配置：")
        max_len = max(len(p) for p in proxies)
        for protocol in sorted(proxies.keys()):
            source_var = proxy_sources.get(protocol, "unknown")
            sanitized = sanitize_proxy_url(proxies[protocol])
            print(f"  ├─ {protocol.upper().ljust(max_len)} : {sanitized}")
            print(f"  └─ {'via'.ljust(max_len)} : {source_var}")
    else:
        print("ℹ️ 未检测到代理配置")


def handle_ask_mode(program_args, api_key, proxies):
    """处理--ask模式"""
    program_args.ask = program_args.ask.replace("@symbol_", "@symbol:")

    base_url = os.getenv("GPT_BASE_URL")
    context_processor = GPTContextProcessor()
    text = context_processor.process_text_with_file_path(program_args.ask)
    print(text)
    response_data = query_gpt_api(
        api_key,
        text,
        proxies=proxies,
        model=os.environ["GPT_MODEL"],
        base_url=base_url,
        temperature=float(os.getenv("GPT_TEMPERATURE", "0.0")),
    )
    process_response(
        text,
        response_data,
        os.path.join(os.path.dirname(__file__), ".lastgptanswer"),
        save=True,
        obsidian_doc=program_args.obsidian_doc,
        ask_param=program_args.ask,
    )


# 定义UI样式
class EyeCareStyle:
    """护眼主题配色方案"""

    def __init__(self):
        self.styles = {
            # 基础界面元素
            "": "#4CAF50",  # 默认文本颜色
            "prompt": "#4CAF50 bold",
            "input": "#4CAF50",
            "output": "#81C784",
            "status": "#4CAF50",
            # 自动补全菜单
            "completion.current": "bg:#4CAF50 #ffffff",
            "completion": "bg:#E8F5E9 #4CAF50",
            "progress-button": "bg:#C8E6C9",
            "progress-bar": "bg:#4CAF50",
            # 滚动条
            "scrollbar.button": "bg:#E8F5E9",
            "scrollbar": "bg:#4CAF50",
            # Markdown渲染
            "markdown.heading": "#4CAF50 bold",
            "markdown.code": "#4CAF50",
            "markdown.list": "#4CAF50",
            "markdown.blockquote": "#81C784",
            "markdown.link": "#4CAF50 underline",
            # GPT响应相关
            "gpt.response": "#81C784",
            "gpt.prefix": "#4CAF50 bold",
            # 特殊符号
            "special-symbol": "#4CAF50 italic",
        }

    def invalidation_hash(self):
        """生成样式哈希值用于缓存失效检测"""
        return hash(frozenset(self.styles.items()))


class ChatbotUI:
    """终端聊天机器人UI类，支持流式响应、Markdown渲染和自动补全

    输入假设:
    - 环境变量GPT_KEY、GPT_MODEL、GPT_BASE_URL必须已正确配置
    - 当使用@符号补全时，prompts目录需存在于GPT_PATH环境变量指定路径下
    - 温度值设置命令参数应为0-1之间的浮点数
    """

    _COMMAND_HANDLERS = {
        "clear": lambda self: os.system("clear"),
        "help": lambda self: self.display_help(),
        "exit": lambda self: sys.exit(0),
        "temperature": lambda self, cmd: self.handle_temperature_command(cmd),
    }

    _SYMBOL_DESCRIPTIONS = [
        ("@clipboard", "插入剪贴板内容"),
        ("@tree", "显示当前目录结构"),
        ("@treefull", "显示完整目录结构"),
        ("@read", "读取文件内容"),
        ("@listen", "语音输入"),
        ("@symbol:", "插入特殊符号(如@symbol:check)"),
    ]

    _COMMAND_LIST = [
        ("/clear", "清空屏幕内容", "/clear"),
        ("/help", "显示本帮助信息", "/help"),
        ("/exit", "退出程序", "/exit"),
        ("/temperature", "设置生成温度(0-1)", "/temperature 0.8"),
    ]

    def __init__(self, gpt_processor: GPTContextProcessor = None):
        """初始化UI组件和配置
        Args:
            gpt_processor: GPT上下文处理器实例，允许依赖注入便于测试
        """
        self.style = self._configure_style()
        self.session = PromptSession(style=self.style)
        self.bindings = self._setup_keybindings()
        self.console = Console()
        self.temperature = 0.6
        self.gpt_processor = gpt_processor or GPTContextProcessor()

    def __str__(self) -> str:
        return (
            f"ChatbotUI(temperature={self.temperature}, "
            f"style={self.style.styles}, "
            f"gpt_processor={type(self.gpt_processor).__name__})"
        )

    def _configure_style(self) -> Style:
        """配置终端样式为护眼风格"""
        return Style.from_dict(EyeCareStyle().styles)

    def _setup_keybindings(self) -> KeyBindings:
        """设置快捷键绑定"""
        bindings = KeyBindings()
        bindings.add("escape")(self._exit_handler)
        bindings.add("c-c")(self._exit_handler)
        bindings.add("c-l")(self._clear_screen_handler)
        return bindings

    def _exit_handler(self, event):
        event.app.exit()

    def _clear_screen_handler(self, event):
        event.app.renderer.clear()

    def handle_command(self, cmd: str):
        """处理斜杠命令
        Args:
            cmd: 用户输入的命令字符串，需以/开头
        """
        cmd_parts = cmd.split(maxsplit=1)
        base_cmd = cmd_parts[0]

        if base_cmd not in self._COMMAND_HANDLERS:
            self.console.print(f"[red]未知命令: {cmd}[/]")
            return

        try:
            if base_cmd == "temperature":
                self._COMMAND_HANDLERS[base_cmd](self, cmd)
            else:
                self._COMMAND_HANDLERS[base_cmd](self)
        except Exception as e:
            self.console.print(f"[red]命令执行失败: {str(e)}[/]")

    def display_help(self):
        """显示详细的帮助信息"""
        self._print_command_help()
        self._print_symbol_help()

    def _print_command_help(self):
        """输出命令帮助表格"""
        table = Table(show_header=True, header_style="bold #4CAF50", box=None)
        table.add_column("命令", width=15, style="#4CAF50")
        table.add_column("描述", style="#4CAF50")
        table.add_column("示例", style="dim #4CAF50")

        for cmd, desc, example in self._COMMAND_LIST:
            table.add_row(Text(cmd, style="#4CAF50 bold"), desc, Text(example, style="#81C784"))

        self.console.print("\n[bold #4CAF50]可用命令列表:[/]")
        self.console.print(table)

    def _print_symbol_help(self):
        """输出符号帮助表格"""
        symbol_table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
        symbol_table.add_column("符号", style="#4CAF50 bold", width=12)
        symbol_table.add_column("描述", style="#81C784")

        for symbol, desc in self._SYMBOL_DESCRIPTIONS:
            symbol_table.add_row(symbol, desc)

        self.console.print("\n[bold #4CAF50]符号功能说明:[/]")
        self.console.print(symbol_table)
        self.console.print("\n[dim #4CAF50]提示: 输入时使用Tab键触发自动补全，按Ctrl+L清屏，Esc键退出程序[/]")

    def handle_temperature_command(self, cmd: str):
        """处理温度设置命令
        Args:
            cmd: 完整的温度设置命令字符串，例如'temperature 0.8'
        """
        try:
            parts = cmd.split()
            if len(parts) == 1:
                self.console.print(f"当前temperature: {self.temperature}")
                return

            temp = float(parts[1])
            if not 0 <= temp <= 1:
                raise ValueError("temperature必须在0到1之间")

            self.temperature = temp
            self.console.print(f"temperature已设置为: {self.temperature}", style="#4CAF50")

        except (ValueError, IndexError) as e:
            self.console.print(f"[red]参数错误: {str(e)}[/]")

    def get_completer(self) -> WordCompleter:
        """获取自动补全器，支持@和/两种补全模式"""
        prompt_files = self._get_prompt_files()
        all_items = [s[0] for s in self._SYMBOL_DESCRIPTIONS] + prompt_files + [c[0] for c in self._COMMAND_LIST]

        meta_dict = {**{s[0]: s[1] for s in self._SYMBOL_DESCRIPTIONS}, **{c[0]: c[1] for c in self._COMMAND_LIST}}

        return WordCompleter(
            words=all_items,
            meta_dict=meta_dict,
            ignore_case=True,
            # 启用句子模式补全（允许部分匹配）
            sentence=False,
            match_middle=True,
            WORD=False,
        )

    def _get_prompt_files(self) -> list:
        """获取提示文件列表"""
        prompts_dir = os.path.join(os.getenv("GPT_PATH", ""), "prompts")
        if os.path.exists(prompts_dir):
            return ["@" + f for f in os.listdir(prompts_dir)]
        return []

    def stream_response(self, prompt: str):
        """流式获取GPT响应并实时渲染Markdown
        Args:
            prompt: 用户输入的提示文本
        """
        processed_text = self.gpt_processor.process_text_with_file_path(prompt)
        return query_gpt_api(
            api_key=os.getenv("GPT_KEY"),
            prompt=processed_text,
            model=os.environ["GPT_MODEL"],
            base_url=os.getenv("GPT_BASE_URL"),
            stream=True,
            console=self.console,
            temperature=self.temperature,
        )

    def run(self):
        """启动聊天机器人主循环"""
        self.console.print("欢迎使用终端聊天机器人！输入您的问题，按回车发送。按ESC退出", style="#4CAF50")

        while True:
            try:
                text = self.session.prompt(
                    ">",
                    key_bindings=self.bindings,
                    completer=self.get_completer(),
                    complete_while_typing=True,
                    bottom_toolbar=lambda: (
                        f"状态: 就绪 [Ctrl+L 清屏] [@ 触发补全] [/ 触发命令] | " f"temperature: {self.temperature}"
                    ),
                    lexer=PygmentsLexer(MarkdownLexer),
                )

                if not self._process_input(text):
                    break

            except KeyboardInterrupt:
                self.console.print("\n已退出聊天。", style="#4CAF50")
                break
            except EOFError:
                self.console.print("\n已退出聊天。", style="#4CAF50")
                break
            except Exception as e:
                traceback.print_exc()
                self.console.print(f"\n[red]发生错误: {str(e)}[/]\n")

    def _process_input(self, text: str) -> bool:
        """处理用户输入
        Returns:
            bool: 是否继续运行主循环
        """
        if not text:
            return False
        if text.strip().lower() == "q":
            self.console.print("已退出聊天。", style="#4CAF50")
            return False
        if not text.strip():
            return True
        if text.startswith("/"):
            self.handle_command(text[1:])
            return True

        self.console.print("BOT:", style="#4CAF50 bold")
        self.stream_response(text)
        return True


def handle_code_analysis(program_args, api_key, proxies):
    """处理代码分析模式"""
    try:
        with open(program_args.prompt_file, "r", encoding="utf-8") as f:
            prompt_template = f.read().strip()
        with open(program_args.file, "r", encoding="utf-8") as f:
            code_content = f.read()

        if len(code_content) > program_args.chunk_size:
            response_data = handle_large_code(program_args, code_content, prompt_template, api_key, proxies)
        else:
            response_data = handle_small_code(program_args, code_content, prompt_template, api_key, proxies)

        process_response(
            "",
            response_data,
            "",
            save=False,
            obsidian_doc=program_args.obsidian_doc,
            ask_param=program_args.file,
        )

    except Exception as e:
        print(f"运行时错误: {e}")
        sys.exit(1)


def handle_large_code(program_args, code_content, prompt_template, api_key, proxies):
    """处理大文件分块分析"""
    code_chunks = split_code(code_content, program_args.chunk_size)
    responses = []
    total_chunks = len(code_chunks)
    base_url = os.getenv("GPT_BASE_URL")
    for i, chunk in enumerate(code_chunks, 1):
        pager = f"这是代码的第 {i}/{total_chunks} 部分：\n\n"
        print(pager)
        chunk_prompt = prompt_template.format(path=program_args.file, pager=pager, code=chunk)
        response_data = query_gpt_api(
            api_key,
            chunk_prompt,
            proxies=proxies,
            model=os.environ["GPT_MODEL"],
            base_url=base_url,
        )
        response_pager = f"\n这是回答的第 {i}/{total_chunks} 部分：\n\n"
        responses.append(response_pager + response_data["choices"][0]["message"]["content"])
    return {"choices": [{"message": {"content": "\n\n".join(responses)}}]}


def handle_small_code(program_args, code_content, prompt_template, api_key, proxies):
    """处理小文件分析"""
    full_prompt = prompt_template.format(path=program_args.file, pager="", code=code_content)
    base_url = os.getenv("GPT_BASE_URL")
    return query_gpt_api(
        api_key,
        full_prompt,
        proxies=proxies,
        model=os.environ["GPT_MODEL"],
        base_url=base_url,
    )


def main(args):
    shadowroot.mkdir(parents=True, exist_ok=True)

    validate_environment()
    validate_files(args)
    proxies, proxy_sources = detect_proxies()
    print_proxy_info(proxies, proxy_sources)

    if args.ask:
        handle_ask_mode(args, os.getenv("GPT_KEY"), proxies)
    elif args.chatbot:
        ChatbotUI().run()
    else:
        handle_code_analysis(args, os.getenv("GPT_KEY"), proxies)


if __name__ == "__main__":
    args = parse_arguments()
    if args.trace:
        tracer = trace.Trace(trace=1)
        tracer.runfunc(main, args)
    else:
        main(args)
