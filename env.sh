# GPT 环境配置
# 兼容 zsh/bash/sh 的脚本目录获取
_get_script_dir() {
    if [ -n "$BASH_VERSION" ]; then
        echo "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    elif [ -n "$ZSH_VERSION" ]; then
        echo "$(cd "$(dirname "${(%):-%x}")" && pwd)"
    else
        echo "Error: Unsupported shell. Please use bash or zsh." >&2
        return 1
    fi
}

# 初始化基础环境变量
_init_gpt_env() {
    if [[ -z "$GPT_PATH" ]]; then
        local script_dir
        if ! script_dir=$(_get_script_dir); then
            return 1
        fi
        export GPT_PATH="$script_dir"
    fi

    export GPT_DOC="$GPT_PATH/obsidian"
    export PATH="$GPT_PATH/bin:$PATH"
    export GPT_PROMPTS_DIR="$GPT_PATH/prompts"
    export GPT_LOGS_DIR="$GPT_PATH/logs"
    export GPT_MAX_TOKEN=${GPT_MAX_TOKEN:-16384}
    export GPT_UUID_CONVERSATION=${GPT_UUID_CONVERSATION:-$(uuidgen)}
}

# 目录初始化
_init_directories() {
    mkdir -p "$GPT_PATH"/{bin,prompts,logs,conversation} 2>/dev/null
}

# 会话管理函数
_new_conversation() {
    export GPT_UUID_CONVERSATION=$(uuidgen)
    echo "新会话编号: $GPT_UUID_CONVERSATION"
}

# 会话列表核心逻辑
_conversation_core_logic() {
    local limit=$1
    CONVERSATION_LIMIT=$limit python3 -c '
import os, sys, json
from datetime import datetime

def scan_conversation_files(conversation_dir):
    files = []
    for root, _, filenames in os.walk(conversation_dir):
        for fname in filenames:
            if fname in ["index.json", ".DS_Store"] or not fname.endswith(".json"):
                continue
            path = os.path.join(root, fname)
            try:
                date_str = os.path.basename(os.path.dirname(path))
                time_uuid = os.path.splitext(fname)[0]
                uuid = "-".join(time_uuid.split("-")[3:])
                time_str = ":".join(time_uuid.split("-")[0:3])
                mtime = os.path.getmtime(path)
                preview = get_preview(path)
                files.append((mtime, date_str, time_str, uuid, preview, path))
            except Exception as e:
                continue
    return files

def get_preview(file_path):
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("content", "")[:32].replace("\n", " ").strip()
    except Exception as e:
        pass
    return "N/A"

conversation_dir = os.path.join(os.environ["GPT_PATH"], "conversation")
files = scan_conversation_files(conversation_dir)
files.sort(reverse=True, key=lambda x: x[0])

limit = int(os.getenv("CONVERSATION_LIMIT", "0"))
if limit > 0:
    files = files[:limit]

for idx, (_, date, time, uuid, preview, _) in enumerate(files):
    print(f"{idx+1}\t{date} {time}\t{uuid}\t{preview}")
'
}

# 显示会话选择菜单
_show_conversation_menu() {
    local selection=$1
    local title=$2

    echo "$title："
    echo "$selection" | awk -F '\t' '
    BEGIN { format = "\033[1m%2d)\033[0m \033[33m%-19s\033[0m \033[36m%-36s\033[0m %s\n" }
    {
        preview = length($4)>32 ? substr($4,1,32) "..." : $4
        printf format, $1, $2, $3, preview
    }'
}

# 处理用户选择
_handle_user_selection() {
    local selection=$1
    local item_count=$2

    echo -n "请选择对话 (1-${item_count}，直接回车取消): "
    read -r choice

    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= item_count )); then
        local selected_uuid=$(echo "$selection" | awk -F '\t' -v choice="$choice" 'NR==choice {print $3}')
        export GPT_UUID_CONVERSATION="$selected_uuid"
        echo "已切换到对话: $selected_uuid"
    else
        echo "操作已取消"
    fi
}

_conversation_list() {
    local limit=$1
    local title
    [[ $limit -gt 0 ]] && title="最近的${limit}条对话记录" || title="所有对话记录"

    local selection=$(_conversation_core_logic "$limit")
    [[ -z "$selection" ]] && { echo "没有找到历史对话"; return 1; }

    _show_conversation_menu "$selection" "$title"
    local item_count=$(echo "$selection" | wc -l)
    _handle_user_selection "$selection" "$item_count"
}

# 模型管理函数
_list_models() {
    local config_file="${1:-$GPT_PATH/model.json}"
    python3 -c "import json, sys; config=json.load(open('$config_file')); [print(f'{k}: {v[\"model_name\"]}') for k, v in config.items() if v.get('key')]" 2>/dev/null
}

_list_model_names() {
    local config_file="${1:-$GPT_PATH/model.json}"
    python3 -c "import json, sys; config=json.load(open('$config_file')); [print(k) for k, v in config.items() if v.get('key')]" 2>/dev/null
}


_read_model_config() {
    local model_name=$1
    local config_file=$2
    python3 -c "import json, sys; config=json.load(open('$config_file')).get('$model_name', {}); print(config.get('key', ''), config.get('base_url', ''), config.get('model_name', ''), config.get('max_tokens', ''), config.get('temperature', ''))"
}

_set_gpt_env_vars() {
    local key=$1
    local base_url=$2
    local model=$3
    local max_tokens=$4
    local temperature=$5

    export GPT_KEY="$key"
    export GPT_BASE_URL="$base_url"
    export GPT_MODEL="$model"
    [[ -n "$max_tokens" ]] && export GPT_MAX_TOKEN="$max_tokens"
    [[ -n "$temperature" ]] && export GPT_TEMPERATURE="$temperature"
}

usegpt() {
    local model_name="$1"
    local config_file="${2:-$GPT_PATH/model.json}"
    local no_verbose="$3"

    [[ -z "$model_name" ]] && { echo >&2 "错误：模型名称不能为空"; return 1; }
    [[ -f "$config_file" ]] || { echo >&2 "错误：未找到配置文件: $config_file"; return 1; }

    local key base_url model max_tokens temperature
    read key base_url model max_tokens temperature <<< $(_read_model_config "$model_name" "$config_file")

    [[ -z "$key" || -z "$base_url" || -z "$model" ]] && { echo >&2 "错误：未找到模型 '$model_name' 或配置不完整"; return 1; }

    _set_gpt_env_vars "$key" "$base_url" "$model" "$max_tokens" "$temperature"

    [[ -z "$no_verbose" ]] && {
        echo "成功设置GPT环境变量："
        echo "  GPT_KEY: ${key:0:4}****"
        echo "  GPT_BASE_URL: $base_url"
        echo "  GPT_MODEL: $model"
        [[ -n "$max_tokens" ]] && echo "  GPT_MAX_TOKEN: $max_tokens"
        [[ -n "$temperature" ]] && echo "  GPT_TEMPERATURE: $temperature"
    }
}

# 环境检查函数
_check_gpt_env() {
    if [[ -z "$GPT_KEY" || -z "$GPT_MODEL" || -z "$GPT_BASE_URL" ]]; then
        echo "错误：请先配置GPT_KEY、GPT_MODEL和GPT_BASE_URL环境变量"
        return 1
    fi
}

# 初始化流程
_init_gpt_env
_init_directories

# 公共工具函数
_debug_print() {
    [[ ${GPT_DEBUG:-0} -eq 1 ]] && echo "Debug: $1" >&2
}

# 会话管理命令
function newconversation() { _new_conversation; }
function allconversation() { _conversation_list "${1:-0}"; }
function recentconversation() { _conversation_list 10; }
function listgpt() { _list_models "$@"; }

# 核心功能函数
explaingpt() {
    local file="$1"
    local prompt_file="${2:-$GPT_PROMPTS_DIR/source-query.txt}"

    [[ -f "$file" ]] || { echo >&2 "Error: Source file not found: $file"; return 1; }
    [[ -f "$prompt_file" ]] || { echo >&2 "Error: Prompt file not found: $prompt_file"; return 1; }

    $GPT_PATH/.venv/bin/python $GPT_PATH/llm_query.py --file "$file" --prompt-file "$prompt_file"
}

chat() {
    _check_gpt_env || return 1
    [[ "$1" == "new" ]] && export GPT_UUID_CONVERSATION=$(uuidgen)
    $GPT_PATH/.venv/bin/python $GPT_PATH/llm_query.py --chatbot
}

askgpt() {
    [[ -z "$*" ]] && { echo >&2 "Error: Question cannot be empty"; return 1; }
    $GPT_PATH/.venv/bin/python $GPT_PATH/llm_query.py --ask "$*"
}

# 补全功能辅助函数
_get_prompt_files() {
    find "$GPT_PROMPTS_DIR" -maxdepth 1 -type f -exec basename {} \; 2>/dev/null
}

_get_api_completions() {
    local prefix="$1"
    [[ -z "$GPT_API_SERVER" || "$prefix" != symbol:* ]] && return

    local symbol_prefix="${prefix#symbol:}"
    if [[ "$symbol_prefix" != *.* ]]; then
        # 兼容zsh和bash的文件补全
        if type compgen &>/dev/null; then
            # bash环境使用compgen
            compgen -f "$symbol_prefix" | sed 's/^/symbol:/'
        else
            # zsh环境使用ls
            ls -p "$symbol_prefix"* 2>/dev/null | grep -v / | sed 's/^/symbol:/'
        fi
    else
        curl -s --noproxy "*" "${GPT_API_SERVER}complete_realtime?prefix=${prefix}"
    fi
}

# Shell 补全函数
_zsh_completion_setup() {
    _zsh_at_complete() {
        local orig_prefix=$PREFIX
        [[ "$PREFIX" != @* ]] && return

        local search_prefix=${PREFIX#@}
        IPREFIX="@"
        PREFIX=$search_prefix

        local prompt_files=($(_get_prompt_files))
        local api_completions=($(_get_api_completions "$search_prefix"))
        local symbol_items=($(ls -p | grep -v / | sed 's/^/symbol:/'))

        _alternative \
            'special:特殊选项:(clipboard tree treefull read listen symbol: glow last edit patch)' \
            'prompts:提示词文件:(${prompt_files[@]})' \
            'api:API补全:(${api_completions[@]})' \
            'symbols:本地符号:(${symbol_items[@]})' \
            'files:文件名:_files'

        PREFIX=$orig_prefix
        IPREFIX=""
    }

    _zsh_usegpt_complete() {
        local providers=($(_list_model_names))
        _alternative "providers:可用模型:(${providers[@]})"
    }

    compdef _zsh_at_complete askgpt naskgpt
    compdef _zsh_usegpt_complete usegpt
}

_bash_completion_setup() {
    _bash_at_complete() {
        local cur=${COMP_WORDS[COMP_CWORD]}
        [[ "$cur" != @* ]] && return

        local search_prefix=${cur#@}
        local prompt_files=($(_get_prompt_files))
        local api_completions=($(_get_api_completions "$search_prefix"))
        local symbol_items=($(ls -p | grep -v / | sed 's/^/symbol:/'))

        COMPREPLY=()
        COMPREPLY+=(${symbol_items[@]/#/@})
        COMPREPLY+=(${prompt_files[@]/#/@})
        COMPREPLY+=(${api_completions[@]/#/@})
        COMPREPLY+=($(compgen -f -- "$search_prefix" | sed 's/^/@/'))
        COMPREPLY=($(compgen -W "${COMPREPLY[*]}" -- "$cur"))
    }

    _bash_usegpt_complete() {
        local cur=${COMP_WORDS[COMP_CWORD]}
        COMPREPLY=($(compgen -W "$(_list_model_names)" -- "$cur"))
    }

    complete -F _bash_at_complete askgpt naskgpt
    complete -F _bash_usegpt_complete usegpt
}

# 设置补全
if [[ -n "$ZSH_VERSION" ]]; then
    _zsh_completion_setup
elif [[ -n "$BASH_VERSION" ]]; then
    _bash_completion_setup
fi

# 遗留函数保持兼容
function commitgpt() {
    newconversation
    askgpt @git-commit-message @git-stage @git-diff-summary.txt
    rm -f git-diff-summary.txt

    if [[ -f "$GPT_PATH/.lastgptanswer" ]]; then
        ${EDITOR:-vim} "$GPT_PATH/.lastgptanswer"
        git commit -F "$GPT_PATH/.lastgptanswer" && rm "$GPT_PATH/.lastgptanswer"
    else
        echo "错误：未找到提交信息文件"
        return 1
    fi
}

function chatbot() { chat "new"; }
function chatagain() { chat; }
function naskgpt() {
    local original_session=$GPT_SESSION_ID
    newconversation
    askgpt $@
    export GPT_SESSION_ID=$original_session
    echo "已恢复原会话: $original_session"
}

# 自动配置默认模型
if [[ -z "$GPT_KEY" || -z "$GPT_BASE_URL" || -z "$GPT_MODEL" ]]; then
    [[ $DEBUG -eq 1 ]] && echo "Debug: 尝试自动配置默认模型" >&2
    [[ -f "$GPT_PATH/model.json" ]] && usegpt $(_list_model_names | head -1) "$GPT_PATH/model.json" 1
fi

session_id=$(uuidgen)
export GPT_SESSION_ID=$session_id
