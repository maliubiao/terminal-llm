# GPT 环境配置
# 兼容 zsh/bash/sh 的脚本目录获取
# 初始化基础环境变量
_init_gpt_env() {
  if [[ -z "$GPT_PATH" ]]; then
    echo "Warning: GPT_PATH is not set. Skipping initialization."
    return 1
  fi

  export GPT_DOC="$GPT_PATH/obsidian"
  export PATH="$GPT_PATH/bin:$PATH"
  export PYTHONPATH="$GPT_PATH:$PYTHONPATH"
  export GPT_PROMPTS_DIR="$GPT_PATH/prompts"
  export GPT_LOGS_DIR="$GPT_PATH/logs"
  export GPT_MAX_TOKEN=${GPT_MAX_TOKEN:-16384}
  export GPT_UUID_CONVERSATION=${GPT_UUID_CONVERSATION:-$(uuidgen)}
  export GPT_PYTHON_BIN="$GPT_PATH/.venv/bin/python3"

  if [[ -f "$GPT_PATH/.tree/rc.sh" ]]; then
    source "$GPT_PATH/.tree/rc.sh"
  fi
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
  CONVERSATION_LIMIT=$limit "$GPT_PYTHON_BIN" -c '
import os, sys
from shell import scan_conversation_files, get_preview

conversation_dir = os.path.join(os.environ["GPT_PATH"], "conversation")
files = scan_conversation_files(conversation_dir, int(os.getenv("CONVERSATION_LIMIT", "0")))
for idx, (t, date, uuid, preview) in enumerate(files):
    print(f"{idx+1}\t{date}\t{uuid}\t{preview}")
'
}

# 显示会话选择菜单
_show_conversation_menu() {
  local selection=$1
  local title=$2

  {
    echo "$title"
    echo "$selection"
  } | "$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" format-conversation-menu
}

# 处理用户选择
_handle_user_selection() {
  local selection=$1
  local item_count=$2

  echo -n "请选择对话 (1-${item_count}，直接回车取消): "
  read -r choice

  if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= item_count)); then
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
  [[ -z "$selection" ]] && {
    echo "没有找到历史对话"
    return 1
  }

  _show_conversation_menu "$selection" "$title"
  local item_count=$(echo "$selection" | wc -l)
  _handle_user_selection "$selection" "$item_count"
}

# 模型管理函数
_list_models() {
  local config_file="${1:-$GPT_PATH/model.json}"
  "$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" list-models "$config_file"
}

_list_model_names() {
  local config_file="${1:-$GPT_PATH/model.json}"
  "$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" list-model-names "$config_file"
}

_read_model_config() {
  local model_name=$1
  local config_file=$2
  "$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" read-model-config "$model_name" "$config_file"
}

_set_gpt_env_vars() {
  local key=$1
  local base_url=$2
  local model=$3
  local max_context_size=$4
  local max_tokens=$5
  local temperature=$6
  local is_thinking=$7

  # 清空可能存在的旧环境变量
  unset GPT_KEY GPT_BASE_URL GPT_MODEL GPT_MAX_CONTEXT_SIZE GPT_MAX_TOKENS GPT_TEMPERATURE GPT_IS_THINKING

  # 设置新的环境变量
  export GPT_KEY="$key"
  export GPT_BASE_URL="$base_url"
  export GPT_MODEL="$model"
  [[ -n "$max_context_size" ]] && export GPT_MAX_CONTEXT_SIZE="$max_context_size"
  [[ -n "$max_tokens" ]] && export GPT_MAX_TOKENS="$max_tokens"
  [[ -n "$temperature" ]] && export GPT_TEMPERATURE="$temperature"
  [[ -n "$is_thinking" ]] && export GPT_IS_THINKING="$is_thinking"
}

usegpt() {
  local model_name="$1"
  local config_file="${2:-$GPT_PATH/model.json}"
  local no_verbose="$3"

  [[ -z "$model_name" ]] && {
    echo >&2 "错误：模型名称不能为空"
    return 1
  }
  [[ -f "$config_file" ]] || {
    echo >&2 "错误：未找到配置文件: $config_file"
    return 1
  }

  local key base_url model max_context_size max_tokens temperature is_thinking
  read key base_url model max_context_size max_tokens temperature is_thinking <<<$(_read_model_config "$model_name" "$config_file")

  [[ -z "$key" || -z "$base_url" || -z "$model" ]] && {
    echo >&2 "错误：未找到模型 '$model_name' 或配置不完整"
    return 1
  }
  export GPT_MODEL_KEY=$model_name
  _set_gpt_env_vars "$key" "$base_url" "$model" "$max_context_size" "$max_tokens" "$temperature" "$is_thinking"

  [[ -z "$no_verbose" ]] && {
    echo "成功设置GPT环境变量："
    echo "  GPT_KEY: ${key:0:4}****"
    echo "  GPT_BASE_URL: $base_url"
    echo "  GPT_MODEL: $model"
    [[ -n "$max_context_size" ]] && echo "  GPT_MAX_CONTEXT_SIZE: $max_context_size"
    [[ -n "$max_tokens" ]] && echo "  GPT_MAX_TOKENS: $max_tokens"
    [[ -n "$temperature" ]] && echo "  GPT_TEMPERATURE: $temperature"
    [[ -n "$is_thinking" ]] && echo "  GPT_IS_THINKING: $is_thinking"
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

  [[ -f "$file" ]] || {
    echo >&2 "Error: Source file not found: $file"
    return 1
  }
  [[ -f "$prompt_file" ]] || {
    echo >&2 "Error: Prompt file not found: $prompt_file"
    return 1
  }

  "$GPT_PYTHON_BIN" "$GPT_PATH/llm_query.py" --file "$file" --prompt-file "$prompt_file"
}

chat() {
  _check_gpt_env || return 1
  [[ "$1" == "new" ]] && export GPT_UUID_CONVERSATION=$(uuidgen)
  "$GPT_PYTHON_BIN" "$GPT_PATH/llm_query.py" --chatbot
}

askgpt() {
  [[ -z "$*" ]] && {
    echo >&2 "Error: Question cannot be empty"
    return 1
  }
  "$GPT_PYTHON_BIN" "$GPT_PATH/llm_query.py" --ask "$*"
}

codegpt() {
  naskgpt @edit @edit-file @tree $@
}

patchgpt() {
  naskgpt @patch $@
}

archgpt() {
  local original_session=$GPT_SESSION_ID
  newconversation
  [[ -z "$*" ]] && {
    echo >&2 "Error: Question cannot be empty"
    return 1
  }
  "$GPT_PYTHON_BIN" "$GPT_PATH/llm_query.py" --workflow --architect hyperbolic-r1 --coder deepseek-v3.2 --ask "$*"
  export GPT_SESSION_ID=$original_session
  echo "已恢复原会话: $original_session"
}

fixgpt() {
  local last_command=$(fc -ln -1 | sed 's/^[[:space:]]*//')
  echo "上一条命令：$last_command"
  echo -n "确定执行该命令？(Y/n) "
  read confirm
  case $confirm in
  n | N)
    echo "已取消"
    return 1
    ;;
  *) ;;
  esac

  local safe_command=$(echo "$last_command" | tr ' /\\' '___')
  local timestamp=$(date +%Y%m%d_%H%M%S)
  local log_dir="/tmp/fixgpt_logs/${timestamp}"
  mkdir -p "$log_dir"

  echo "$last_command" >"${log_dir}/command.txt"
  eval "$last_command" >"${log_dir}/output.log" 2>&1

  naskgpt @cmd "$last_command" "@${log_dir}/output.log"

  rm -rf "$log_dir"
}

# 补全功能辅助函数

_get_prompt_files() {
  local dir="${GPT_PROMPTS_DIR:-}"
  local files=()

  if [[ -d "$dir" ]]; then
    # 设置 Shell 选项（兼容 Bash/Zsh）
    if [[ -n "$BASH_VERSION" ]]; then
      shopt -s nullglob
      files=("$dir"/*)
      shopt -u nullglob
    else
      setopt local_options nullglob # Zsh 的 null_glob 选项
      files=("$dir"/*)
    fi

    # 移除路径前缀（兼容数组操作）
    files=("${files[@]##*/}")
    # 将文件名中的:替换为_
    files=("${files[@]//:/_}")
  fi

  # 输出结果供其他函数使用
  printf '%s\n' "${files[@]}"
}

_get_api_completions() {
  local prefix="$1"
  [[ -z "$GPT_SYMBOL_API_URL" || "$prefix" != symbol_* ]] && return

  _debug_print "api $prefix"
  local local_path="${prefix#symbol_}"

  "$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" complete "$prefix" | while read -r item; do
    echo "$item"
  done
}

# Shell 补全函数
_zsh_completion_setup() {
  _zsh_at_complete() {
    local orig_prefix=$PREFIX
    if [[ "$PREFIX" == @* ]]; then
      local search_prefix=${PREFIX#@}
      IPREFIX="@"
      PREFIX=$search_prefix

      local prompt_files=($(_get_prompt_files))
      local api_completions=($(_get_api_completions "$search_prefix"))
      local symbol_items=($(ls -p | grep -v / | sed 's/^/symbol_/'))

      _alternative \
        'special:特殊选项:(clipboard tree treefull read listen symbol_ glow last edit patch context)' \
        'prompts:提示词文件:(${prompt_files[@]})' \
        'api:API补全:(${api_completions[@]})' \
        'symbols:本地符号:(${symbol_items[@]})' \
        'files:文件名:_files'

      PREFIX=$orig_prefix
      IPREFIX=""
    else
      _files
    fi
  }

  _zsh_usegpt_complete() {
    local providers=($(_list_model_names))
    _alternative "providers:可用模型:(${providers[@]})"
  }

  compdef _zsh_at_complete askgpt naskgpt codegpt patchgpt archgpt
  compdef _zsh_usegpt_complete usegpt
}

_bash_completion_setup() {
  _bash_at_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    local prev=${COMP_WORDS[COMP_CWORD - 1]}
    _debug_print "cur $cur ${COMP_WORDS[*]}"
    if [[ "$cur" != @* && "$prev" != "@" ]]; then
      COMPREPLY=($(compgen -o default -- "$cur"))
      return
    fi
    local array=$("$GPT_PYTHON_BIN" "$GPT_PATH/shell.py" shell-complete "@$cur")
    COMPREPLY=()
    for item in $array; do
      COMPREPLY+=("@$item")
    done
    if [[ "$prev" == "@" ]]; then
      COMPREPLY=($(compgen -W "${COMPREPLY[*]}" -- "@$cur"))
    else
      COMPREPLY=($(compgen -W "${COMPREPLY[*]}" -- "$cur"))
    fi
  }

  _bash_usegpt_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    COMPREPLY=($(compgen -W "$(_list_model_names)" -- "$cur"))
  }

  complete -F _bash_at_complete askgpt naskgpt codegpt patchgpt archgpt
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
  if [[ -z "$VIRTUAL_ENV" ]]; then
    echo "错误：不在Python虚拟环境中，请先激活虚拟环境"
    return 1
  fi

  if ! git diff --quiet; then
    echo "错误：存在未暂存的更改，请先使用git add添加更改"
    return 1
  fi

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

function trace() {
  $GPT_PYTHON_BIN $GPT_PATH/debugger/tracer_main.py --open-report $@
}

function symbolgpt() {
  $GPT_PYTHON_BIN -c "import llm_query; llm_query.start_symbol_service(False)"
}

function symbolgptrestart() {
  $GPT_PYTHON_BIN -c "import llm_query; llm_query.start_symbol_service(True)"
}

function patchgpttrace() {
  python -m debugger.tracer_main --open-report llm_query.py --ask "@patch @patch-rule @symbol-path-rule-v2 $@"
}

# 自动配置默认模型
if [[ -z "$GPT_KEY" || -z "$GPT_BASE_URL" || -z "$GPT_MODEL" ]]; then
  [[ $DEBUG -eq 1 ]] && echo "Debug: 尝试自动配置默认模型" >&2
  [[ -f "$GPT_PATH/model.json" ]] && usegpt $(_list_model_names | head -1) "$GPT_PATH/model.json" 1
fi

session_id=$(uuidgen)
export GPT_SESSION_ID=$session_id
