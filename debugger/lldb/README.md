# LLDB Tracer

高级LLDB调试工具，提供增强的调试功能和可视化界面。

## 新架构说明

项目已重构为模块化结构：

```
tracer/
├── __init__.py         # 包入口
├── core.py             # Tracer核心类
├── config.py           # 配置管理
├── logging.py          # 日志管理
├── symbols.py          # 符号处理和渲染
├── utils.py            # 工具函数
├── events.py           # 事件处理
└── breakpoints.py      # 断点相关
tracer_main.py          # 主入口脚本
```

## 安装

```bash
pip install -r requirements.txt
```

## 使用示例

基本使用：
```bash
./tracer_main.py -e /path/to/program -a arg1 -a arg2
```

启用详细日志：
```bash
./tracer_main.py -e /path/to/program --verbose
```

生成跳过模块配置：
```bash
./tracer_main.py -e /path/to/program --dump-modules-for-skip
```

## 新功能

### 环境变量配置
在配置文件中设置环境变量：
```yaml
# tracer_config.yaml
environment:
  DEBUG: "1"
  PATH: "/custom/path:$PATH"
  CUSTOM_SETTING: "special_value"
```

### 模块跳过配置
使用`--dump-modules-for-skip`生成配置，工具会交互式显示所有模块并让用户选择保留的模块，其余模块将被跳过。

### 符号可视化
运行后会生成`symbols.html`文件，在浏览器中打开可查看交互式符号信息。

## 测试

运行测试脚本：
```bash
./test_tracer.sh
```

测试环境变量功能：
```bash
./test_env_vars.sh
```

## libc函数参数自动跟踪功能

### 功能概述
1. 自动根据ABI规范解析libc函数的参数
2. 在函数调用时记录参数值
3. 在函数返回时记录返回值
4. 只需要配置函数名列表即可工作

### 实现方案

#### 1. 配置扩展
在config.yaml中添加`libc_functions`配置项，包含要跟踪的函数名列表：

```yaml
libc_functions:
  - fopen
  - fclose 
  - read
  - write
  - malloc
  - free
```

#### 2. 参数解析器
根据平台ABI规范解析参数：
- ARM64: 使用x0-x7寄存器传递前8个参数
- x86_64: 使用rdi, rsi, rdx, rcx, r8, r9寄存器传递前6个参数
- 栈参数通过frame.FindVariable()获取

#### 3. 返回值处理
- 在函数入口设置断点时，同时设置返回地址断点
- 返回值通常存储在x0/rax寄存器中

#### 4. 日志格式
函数调用日志示例：
```
[time] CALL fopen(path="/etc/passwd", mode="r") 
[time] RET fopen => 0x1234 (FILE*)
```
