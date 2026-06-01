# kRepo

`kRepo` 是一个面向 C/C++ 工程源码的代码知识库构建工具集。项目读取
VS Code C/C++ 插件 `vscode-cpptools` 生成的 SQLite 元数据库
`BROWSE.VC.DB`，结合源码启发式分析，围绕指定函数抽取后续生成单元测试
测试用例和灰盒 Fuzz 测试 harness 所需的上下文知识。

## 项目目标

本项目不是某个特定 C/C++ 项目的源码仓库，也不是直接生成 harness 的最终工具。它的定位是
构建函数级知识库，为后续自动化测试生成提供高质量输入：

- 函数源码和依赖代码片段：宏、常量、typedef、枚举、全局变量、静态变量、
  结构体、嵌套结构体、目标函数源码和下游子函数源码。
- 上层调用链：按 `a -> b -> target` 形式展示目标函数被哪些上层函数调用。
- 入参约束：根据函数体上下文推断参数类型、指针属性、用户态指针、范围检查、
  常量约束和关键证据行。

这些信息可用于：

- 选择适合做单元测试或 Fuzz 的入口函数。
- 构造函数参数、初始化结构体和全局上下文。
- 推断有效输入格式、边界条件和错误路径。
- 为灰盒 Fuzz harness 生成种子约束和调用前置条件。

## 元数据库来源

脚本依赖的 `BROWSE.VC.DB` 由 VS Code C/C++ 插件生成：

1. 安装 VS Code 扩展 `ms-vscode.cpptools`。
2. 使用 VS Code 打开待分析的 C/C++ 工程源码目录，例如 `my_project`。
3. 等待 C/C++ 插件完成 browse database 构建。
4. 默认数据库路径通常为：

```text
my_project/.vscode/BROWSE.VC.DB
```

如果数据库路径不同，可以通过 `--db` 显式指定。

`--db` 支持三种形式：

```text
path/to/BROWSE.VC.DB
path/to/.vscode
path/to/cpp-source-root
```

## 目录结构

```text
kRepo/
  src/
    cpp_meta_query.py        推荐 CLI 入口和 Python API re-export
    cpp_meta/                核心实现包和通用 C/C++ Python API
      base.py                  命令基类和通用配置
      models.py                SQLite 元数据模型和常量
      db.py                    SQLite 访问和函数定位
      parsing.py               源码切片、清洗和 token 提取
      dependencies.py          宏/类型/变量依赖收集和嵌套类型展开
      calls.py                 调用点解析、上游/下游调用图搜索
      params.py                入参约束推断
      engine.py                公共分析报告聚合
      source_bundle.py         功能 1：单函数源码分析包
      call_chains.py           功能 2：上层调用链
      param_constraints.py     功能 3：入参约束
      subfunction_bundle.py    功能 4：目标函数和下游子函数分析包
      renderer.py              Markdown/.c 输出渲染
      cli.py                   argparse 命令行装配
  docs/
    cpp_meta_query_usage.md  详细用法和验证案例
  test/
    test_cpp_meta_query.py   Python API 和核心能力烟测
  .gitignore
```

本地待分析源码目录、VS Code 生成的数据库和测试生成物默认不入库，例如：

```text
linux-*/
**/.vscode/BROWSE.VC.DB
test/fixtures/*.c
```

## 快速开始

查看主帮助：

```powershell
python .\src\cpp_meta_query.py --help
```

查看某个功能接口的参数：

```powershell
python .\src\cpp_meta_query.py source --help
python .\src\cpp_meta_query.py subsource --help
python .\src\cpp_meta_query.py calls --help
python .\src\cpp_meta_query.py params --help
python .\src\cpp_meta_query.py report --help
```

指定函数导出 `.c` 分析包：

```powershell
python .\src\cpp_meta_query.py source parse_config --repo .\my_project --file src\config.c --output .\parse_config_bundle.c
```

查询目标函数的上层调用链：

```powershell
python .\src\cpp_meta_query.py calls parse_config --repo .\my_project --file src\config.c --max-depth 3
```

推断目标函数入参约束：

```powershell
python .\src\cpp_meta_query.py params parse_config --repo .\my_project --file src\config.c
```

导出目标函数及其下游子函数源码分析包：

```powershell
python .\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --max-depth 1 --output .\parse_config_subfunctions_bundle.c
```

`subsource` 默认会跳过日志、trace、debug、统计/accounting、instrumentation
等对核心控制流影响较小的辅助子函数。需要完整保留这些子函数源码时：

```powershell
python .\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --include-auxiliary-calls
```

显式指定元数据库：

```powershell
python .\src\cpp_meta_query.py calls parse_config --db .\my_project\.vscode\BROWSE.VC.DB --file src\config.c
```

## 四个核心能力

### 1. source

`source` 根据函数名输出函数源码及其涉及的依赖代码片段，并合并成一个 `.c`
格式分析包。输出顺序为：

```text
常量/宏 -> typedef -> 枚举/枚举值 -> 全局变量 -> 静态变量 -> 结构体/union -> 函数
```

结构体、union、enum、typedef 中继续引用的嵌套类型会按层递归解析。默认嵌套解析层数为
`4`，可通过 `--max-nesting-depth` 调整。

### 2. subsource

`subsource` 从目标函数出发，递归解析可直接定位的下游子函数，并将目标函数、
子函数源码和这些函数共同涉及的依赖片段合并成一个 `.c` 分析包。

输出仍按依赖优先组织：

```text
常量/宏 -> typedef -> 枚举/枚举值 -> 全局变量 -> 静态变量 -> 结构体/union -> 子函数/目标函数
```

函数体部分会尽量按“被调用者在前，调用者在后”排序，减少先引用后定义的情况。
对于调用规模较大的函数，可以通过 `--max-depth`、`--max-functions` 控制输出范围。
依赖片段中的嵌套类型默认递归解析 `4` 层，可通过 `--max-nesting-depth` 调整。
默认会跳过日志、trace、debug、统计/accounting、instrumentation 等辅助函数，
例如 `add_rchar`、`inc_syscr` 这类统计调用；生成文件会在
`Skipped auxiliary callees` 段落中记录跳过项。
同时会排除 `test`、`tests`、`testing`、`selftests`、`DT`、`ST` 等测试目录下的符号索引，
避免测试代码中的同名符号混入下游函数分析包后造成重定义。
最终 `.c` 输出还会按符号类别和名称去重；多个子函数共享同一个宏、typedef、枚举、
全局变量、静态变量、结构体或函数实现时，只保留一份定义。

### 3. calls

`calls` 查询目标函数被哪些上层函数调用，并以链路形式输出：

```text
upper_func -> middle_func -> target_func
```

对于调用关系较多的函数，可以通过 `--max-depth`、`--max-chains`、
`--max-callers-per-level` 控制搜索和输出规模。

### 4. params

`params` 根据函数源码上下文推断入参约束，包括：

- 指针参数是否被解引用。
- 是否为 `__user` 用户态指针。
- 是否经过 `access_ok`、`copy_*_user` 等检查。
- 参数是否参与大小、范围、flag、NULL 判断。
- 相关源码证据行。

### report

`report` 是四个核心能力的统一汇总。它会在一个 Markdown 或 JSON 报告中同时给出：

- 与 `source` 一致的目标函数源码和依赖片段，包含默认 `4` 层嵌套类型解析。
- 与 `calls` 一致的上层调用链，按 `a -> b -> target` 展示。
- 与 `params` 一致的入参约束和证据行。
- 与 `subsource` 一致的下游子函数源码分析摘要、辅助调用跳过项和限制信息。
长代码片段会被压缩为文件路径和行号，避免报告输出被大段源码撑得过长。

## Python API

核心能力也可以作为 Python API 使用：

```python
from src.cpp_meta_query import (
    export_source_bundle,
    export_subfunction_source_bundle,
    print_function_call_sequence,
    print_function_param_constraints,
)

export_source_bundle(
    "parse_config",
    output="parse_config_bundle.c",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

export_subfunction_source_bundle(
    "parse_config",
    output="parse_config_subfunctions_bundle.c",
    repo=r".\my_project",
    file_filter=r"src\config.c",
    max_depth=1,
)

print_function_call_sequence(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

print_function_param_constraints(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)
```

## 验证

运行单元测试：

```powershell
python -m unittest discover -s test -p "test_*.py"
```

当前测试覆盖：

- 函数定位和源码读取。
- `.c` 分析包导出。
- 目标函数和下游子函数 `.c` 分析包导出。
- 嵌套结构体递归解析。
- 上层调用链输出。
- 入参约束输出。

## 已知边界

- `BROWSE.VC.DB` 中部分符号关系表可能为空，因此调用链和参数约束包含源码
  启发式分析结果，不等价于完整编译器级调用图。
- `source` 输出的 `.c` 文件是知识库分析包，便于阅读和后续处理，不保证可直接
  作为独立 C/C++ 编译单元编译。
- 同名函数较多时建议使用 `--file` 指定源码路径子串。
- 大型源码树和 `BROWSE.VC.DB` 体积较大，默认由 `.gitignore` 排除。

## Linux 验证样例

本仓库的本地测试使用 Linux 源码树作为大型 C 工程验证样例，例如：

```powershell
python .\src\cpp_meta_query.py source vfs_read --repo .\linux-7.0 --file fs\read_write.c
python .\src\cpp_meta_query.py calls can_send --repo .\linux-7.0 --file net\can\af_can.c
```

这些案例用于验证工具可处理大型 C 工程，并不代表工具只支持 Linux。

更多命令、参数和实际验证案例见
[docs/cpp_meta_query_usage.md](docs/cpp_meta_query_usage.md)。

