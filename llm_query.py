#!/usr/bin/env python
"""
LLM 查询工具模块

该模块提供与OpenAI兼容 API交互的功能，支持代码分析、多轮对话、剪贴板集成等功能。
包含代理配置检测、代码分块处理、对话历史管理等功能。
"""

import argparse
import datetime
import difflib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from openai import OpenAI
from pygments import highlight
from pygments.formatters.terminal import TerminalFormatter
from pygments.lexers.diff import DiffLexer

# Windows平台相关导入
if sys.platform == "win32":
    try:
        import win32clipboard
    except ImportError:
        win32clipboard = None

MAX_FILE_SIZE = 32000
MAX_PROMPT_SIZE = int(os.environ.get("GPT_MAX_TOKEN", 16384))


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="使用Groq API分析源代码",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="要分析的源代码文件路径")
    group.add_argument("--ask", help="直接提供提示词内容，与--file互斥")
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
    api_key,
    prompt,
    model="gpt-4",
    **kwargs,
):
    """支持多轮对话的OpenAI API流式查询

    参数:
        conversation_file (str): 对话历史存储文件路径
        其他参数同上
    """
    # proxies = kwargs.get('proxies')
    base_url = kwargs.get("base_url")
    conversation_file = kwargs.get("conversation_file", "conversation_history.json")

    cid = os.environ.get("GPT_UUID_CONVERSATION")
    if cid:
        try:
            conversation_file = get_conversation(cid)
            # print("旧对话: %s\n" % conversation_file)
        except FileNotFoundError:
            conversation_file = new_conversation(cid)
            # print("开新对话: %s\n" % conversation_file)

    # 加载历史对话
    history = load_conversation_history(conversation_file)

    # 添加用户新提问到历史
    history.append({"role": "user", "content": prompt})

    # 初始化OpenAI客户端
    client = OpenAI(api_key=api_key, base_url=base_url)

    try:
        # 创建流式响应（使用完整对话历史）
        stream = client.chat.completions.create(
            model=model,
            messages=history,
            temperature=0.0,
            max_tokens=MAX_PROMPT_SIZE,
            top_p=0.8,
            stream=True,
        )

        content = ""
        reasoning = ""
        # 处理流式响应
        for chunk in stream:
            # 处理推理内容（仅打印不保存）
            if hasattr(chunk.choices[0].delta, "reasoning_content") and chunk.choices[0].delta.reasoning_content:
                print(chunk.choices[0].delta.reasoning_content, end="", flush=True)
                reasoning += chunk.choices[0].delta.reasoning_content
            # 处理正式回复内容
            if chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="", flush=True)
                content += chunk.choices[0].delta.content
        print()  # 换行

        # 将助理回复添加到历史（仅保存正式内容）
        history.append({"role": "assistant", "content": content})

        # 保存更新后的对话历史
        save_conversation_history(conversation_file, history)

        # 存储思维过程
        if reasoning:
            content = f"<think>\n{reasoning}\n</think>\n{content}"

        return {"choices": [{"message": {"content": content}}]}

    except Exception as e:
        print(f"OpenAI API请求失败: {e}")
        sys.exit(1)


def _check_tool_installed(tool_name, install_url=None, install_commands=None):
    """检查指定工具是否已安装"""
    try:
        if sys.platform == "win32":
            # Windows系统使用where命令
            subprocess.run(["where", tool_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        else:
            # 非Windows系统使用which命令
            subprocess.run(["which", tool_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        print(f"错误：{tool_name} 未安装")
        if install_url:
            print(f"请访问 {install_url} 安装{tool_name}")
        if install_commands:
            print("请使用以下命令安装：")
            for cmd in install_commands:
                print(f"  {cmd}")
        return False


def check_deps_installed():
    """检查glow、tree和剪贴板工具是否已安装"""
    all_installed = True

    # 检查glow
    if not _check_tool_installed(
        "glow",
        install_url="https://github.com/charmbracelet/glow",
        install_commands=[
            "brew install glow  # macOS",
            "choco install glow  # Windows Chocolatey",
            "scoop install glow  # Windows Scoop",
            "winget install charmbracelet.glow  # Windows Winget",
        ],
    ):
        all_installed = False

    # 检查剪贴板工具
    if sys.platform == "win32":
        try:
            import win32clipboard as _
        except ImportError:
            print("错误：需要安装pywin32来访问Windows剪贴板")
            print("请执行：pip install pywin32")
            all_installed = False
    elif sys.platform != "darwin":  # Linux系统
        clipboard_installed = _check_tool_installed(
            "xclip",
            install_commands=[
                "Ubuntu/Debian: sudo apt install xclip",
                "CentOS/Fedora: sudo yum install xclip",
            ],
        ) or _check_tool_installed(
            "xsel",
            install_commands=[
                "Ubuntu/Debian: sudo apt install xsel",
                "CentOS/Fedora: sudo yum install xsel",
            ],
        )
        if not clipboard_installed:
            all_installed = False

    return all_installed


def get_directory_context_wrapper(max_depth=1):
    text = get_directory_context(max_depth)
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
            else:
                # 其他情况使用tree命令
                cmd = ["tree"]
                if max_depth is not None:
                    cmd.extend(["/A", "/F"])
        else:
            # 非Windows系统使用Linux/macOS的tree命令
            cmd = ["tree", "-I", ".*"]
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


def get_clipboard_content():
    text = get_clipboard_content_real()
    text = f"\n[clipboard content start]\n{text}\n[clipboard content end]\n"
    return text


def get_clipboard_content_real():
    """获取系统剪贴板内容，支持Linux、Mac、Windows"""
    try:
        # 判断操作系统
        if sys.platform == "win32":
            # Windows系统
            win32clipboard = __import__("win32clipboard")
            win32clipboard.OpenClipboard()
            data = win32clipboard.GetClipboardData()
            win32clipboard.CloseClipboard()
            return data
        elif sys.platform == "darwin":
            # Mac系统
            with subprocess.Popen(["pbpaste"], stdout=subprocess.PIPE) as process:
                stdout, _ = process.communicate()
                return stdout.decode("utf-8")
        else:
            # Linux系统
            # 尝试xclip
            try:
                with subprocess.Popen(
                    ["xclip", "-selection", "clipboard", "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process:
                    stdout, _ = process.communicate()
                    if process.returncode == 0:
                        return stdout.decode("utf-8")
            except FileNotFoundError:
                pass

            # 尝试xsel
            try:
                with subprocess.Popen(
                    ["xsel", "--clipboard", "--output"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process:
                    stdout, _ = process.communicate()
                    if process.returncode == 0:
                        return stdout.decode("utf-8")
            except FileNotFoundError:
                pass

            return "无法获取剪贴板内容：未找到xclip或xsel"
    except Exception as e:
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


def _handle_command(match, cmd_map):
    """处理命令类型匹配"""
    return cmd_map[match]()


def _handle_shell_command(match):
    """处理shell命令"""
    with open(os.path.join("prompts", match), "r", encoding="utf-8") as f:
        content = f.read()
    try:
        process = subprocess.Popen(content, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        output = f"\n\n[shell command]: {content}\n"
        # if stdout:
        output += f"[stdout begin]\n{stdout}\n[stdout end]\n"
        if stderr:
            output += f"[stderr begin]\n{stderr}\n[stderr end]\n"
        return output
    except Exception as e:
        return f"\n\n[shell command error]: {str(e)}\n"


def _handle_prompt_file(match, env_vars):
    """处理prompts目录文件"""
    with open(os.path.join("prompts", match), "r", encoding="utf-8") as f:
        content = f.read()
        return f"\n{content.format(**env_vars)}\n"


def _handle_local_file(match):
    """处理本地文件路径"""
    expanded_path = os.path.abspath(os.path.expanduser(match))
    with open(expanded_path, "r", encoding="utf-8") as f:
        content = f.read()
        replacement = f"\n\n[file name]: {expanded_path}\n[file content begin]\n{content}"
        replacement += "\n[file content end]\n\n"
        return replacement


def _handle_url(match):
    """处理URL请求"""
    url = match[4:] if match.startswith("read") else match
    markdown_content = fetch_url_content(url, is_news=match.startswith("read"))
    return (
        "\n\n[reference url, content converted to markdown]: {url} \n"
        "[markdown content begin]\n"
        "{markdown_content}\n"
        "[markdown content end]\n\n"
    ).format(url=url, markdown_content=markdown_content)


def process_text_with_file_path(text):
    """处理包含@...的文本，支持@cmd命令、@path文件路径、@http网址和prompts目录下的模板文件"""
    current_length = len(text)
    cmd_map = {
        "clipboard": get_clipboard_content,
        "tree": get_directory_context_wrapper,
        "treefull": lambda: get_directory_context_wrapper(max_depth=None),
    }
    env_vars = {
        "os": sys.platform,
        "os_version": platform.version(),
        "current_path": os.getcwd(),
    }
    matches = re.findall(r"(\\?@[^\s]+)", text)
    truncated_suffix = "\n[输入太长内容已自动截断]"

    for match in matches:
        if current_length >= MAX_PROMPT_SIZE:
            break

        if text.endswith(match):
            match_key = f"{match}"
        else:
            match_key = f"{match} "

        match_key_length = len(match_key)
        match = match.strip("\\@")
        try:
            replacement = ""
            if match in cmd_map:
                replacement = _handle_command(match, cmd_map)
            elif match.endswith("="):
                replacement = _handle_shell_command(match[:-1])
            elif os.path.exists(os.path.join("prompts", match)):
                replacement = _handle_prompt_file(match, env_vars)
            elif os.path.exists(os.path.expanduser(match)):
                replacement = _handle_local_file(match)
            elif match.startswith(("http", "read")):
                replacement = _handle_url(match)
            else:
                continue

            new_segment_length = len(replacement)
            old_segment_length = match_key_length

            if current_length - old_segment_length + new_segment_length > MAX_PROMPT_SIZE:
                allowable_length = MAX_PROMPT_SIZE - (current_length - old_segment_length)
                replacement = replacement[: allowable_length - len(truncated_suffix)] + truncated_suffix

            text = text.replace(match_key, replacement, 1)
            current_length = current_length - old_segment_length + len(replacement)

            if current_length > MAX_PROMPT_SIZE:
                text = text[:MAX_PROMPT_SIZE]
                break

        except Exception as e:
            print(f"处理 {match} 时出错: {str(e)}")
            sys.exit(1)

    if len(text) > MAX_PROMPT_SIZE:
        suffix_len = len(truncated_suffix)
        text = text[: MAX_PROMPT_SIZE - suffix_len] + truncated_suffix

    return text


# 获取.shadowroot的绝对路径，支持~展开
shadowroot = Path(os.path.expanduser("~/.shadowroot"))


def _save_response_content(content):
    """保存原始响应内容到response.md"""
    response_path = shadowroot / Path("response.md")
    with open(response_path, "w+", encoding="utf-8") as dst:
        dst.write(content)
    return response_path


def _extract_file_matches(content):
    """从内容中提取文件匹配项"""
    return re.findall(r"\[modified file\]: (.*?)\n\[source code start\] *?\n(.*?)\n\[source code end\]", content, re.S)


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


def _display_and_apply_diff(diff_file):
    """显示并应用diff"""
    if diff_file.exists():
        with open(diff_file, "r", encoding="utf-8") as f:
            diff_text = f.read()
            highlighted_diff = highlight(diff_text, DiffLexer(), TerminalFormatter())
            print("\n高亮显示的diff内容：")
            print(highlighted_diff)

        print(f"\n申请变更文件，是否应用 {diff_file}？")
        apply = input("输入 y 应用，其他键跳过: ").lower()
        if apply == "y":
            try:
                subprocess.run(["patch", "-p0", "-i", str(diff_file)], check=True)
                print("已成功应用变更")
            except subprocess.CalledProcessError as e:
                print(f"应用变更失败: {e}")


def extract_and_diff_files(content):
    """从内容中提取文件并生成diff"""
    _save_response_content(content)
    matches = _extract_file_matches(content)
    if not matches:
        return

    diff_content = ""
    for filename, file_content in matches:
        file_path = Path(filename)
        old_file_path = file_path
        file_path = _process_file_path(file_path)
        shadow_file_path = shadowroot / file_path

        _save_file_to_shadowroot(shadow_file_path, file_content)
        original_content = ""
        if old_file_path.exists():
            with open(old_file_path, "r", encoding="utf8") as f:
                original_content = f.read()
        diff = _generate_unified_diff(old_file_path, shadow_file_path, original_content, file_content)
        diff_content += "\n".join(diff) + "\n\n"

    diff_file = _save_diff_content(diff_content)
    if diff_file:
        _display_and_apply_diff(diff_file)


def process_response(response_data, file_path, save=True, obsidian_doc=None, ask_param=None):
    """处理API响应并保存结果"""
    if not response_data["choices"]:
        raise ValueError("API返回空响应")

    content = response_data["choices"][0]["message"]["content"]

    # 处理文件路径
    file_path = Path(file_path)
    if save and file_path:
        with open(file_path, "w+", encoding="utf8") as f:
            # 删除<think>...</think>内容
            cleaned_content = re.sub(r"<think>\n.*?\n</think>", "", content, flags=re.DOTALL)
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

        # 写入响应内容
        with open(obsidian_file, "w", encoding="utf-8") as f:
            f.write(content)

        # 更新main.md
        main_file = obsidian_dir / f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-索引.md"
        link_name = re.sub(r"[{}]", "", ask_param[:256]) if ask_param else timestamp
        link = f"[[{month_dir.name}/{timestamp}|{link_name}]]\n"

        with open(main_file, "a", encoding="utf-8") as f:
            f.write(link)

    if not check_deps_installed():
        sys.exit(1)

    # 调用提取和diff函数
    try:
        subprocess.run(["glow", save_path], check=True)
        # 如果是临时文件，使用后删除
        if not save:
            os.unlink(save_path)
    except subprocess.CalledProcessError as e:
        print(f"glow运行失败: {e}")

    extract_and_diff_files(content)


def main():
    args = parse_arguments()

    # 如果目录不存在则创建
    shadowroot.mkdir(parents=True, exist_ok=True)
    # 集中检查环境变量

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

    if not args.ask:  # 仅在未使用--ask参数时检查文件
        if not os.path.isfile(args.file):
            print(f"错误：源代码文件不存在 {args.file}")
            sys.exit(1)

        if not os.path.isfile(args.prompt_file):
            print(f"错误：提示词文件不存在 {args.prompt_file}")
            sys.exit(1)

    proxies, proxy_sources = detect_proxies()
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

    if args.ask:
        ask_param = args.ask
    else:
        ask_param = args.file
    if args.ask:
        text = process_text_with_file_path(args.ask)
        print(text)
        response_data = query_gpt_api(
            api_key,
            text,
            proxies=proxies,
            model=os.environ["GPT_MODEL"],
            base_url=base_url,
        )
        process_response(
            response_data,
            os.path.join(os.path.dirname(__file__), ".lastgptanswer"),
            save=True,
            obsidian_doc=args.obsidian_doc,
            ask_param=ask_param,
        )
        return

    try:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt_template = f.read().strip()
        with open(args.file, "r", encoding="utf-8") as f:
            code_content = f.read()

        # 如果代码超过分块大小，则分割处理
        if len(code_content) > args.chunk_size:
            code_chunks = split_code(code_content, args.chunk_size)
            responses = []
            total_chunks = len(code_chunks)
            for i, chunk in enumerate(code_chunks, 1):
                # 在提示词中添加当前分块信息
                pager = f"这是代码的第 {i}/{total_chunks} 部分：\n\n"
                print(pager)
                chunk_prompt = prompt_template.format(path=args.file, pager=pager, code=chunk)
                response_data = query_gpt_api(
                    api_key,
                    chunk_prompt,
                    proxies=proxies,
                    model=os.environ["GPT_MODEL"],
                    base_url=base_url,
                )
                response_pager = f"\n这是回答的第 {i}/{total_chunks} 部分：\n\n"
                responses.append(response_pager + response_data["choices"][0]["message"]["content"])
            final_content = "\n\n".join(responses)
            response_data = {"choices": [{"message": {"content": final_content}}]}
        else:
            full_prompt = prompt_template.format(path=args.file, pager="", code=code_content)
            response_data = query_gpt_api(
                api_key,
                full_prompt,
                proxies=proxies,
                model=os.environ["GPT_MODEL"],
                base_url=base_url,
            )
        process_response(
            response_data,
            "",
            save=False,
            obsidian_doc=args.obsidian_doc,
            ask_param=ask_param,
        )

    except Exception as e:
        print(f"运行时错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
