# linux_meta_query.py 用法和验证案例

`linux_meta_query.py` 读取 VS Code C/C++ 插件生成的
`BROWSE.VC.DB`，按函数名查询 Linux 源码元数据，并提供 3 个独立接口。

当前仓库默认 Linux 源码目录：

```text
linux-7.0
```

默认数据库路径：

```text
linux-7.0/.vscode/BROWSE.VC.DB
```

## 三个功能接口

脚本同时支持命令行接口和 Python import 接口。命令行接口如下。

### 1. source：输出源码片段并合并成 .c 文件

输入函数名，输出函数源码，以及源码涉及的结构体、typedef、枚举/枚举值、常量/宏、全局变量、静态变量等代码片段，合并为一个 `.c` 文件。对于结构体、union、enum、typedef 中继续引用的嵌套类型，脚本会递归解析并一并写入 `.c` 文件。

```powershell
python .\tools\linux_meta_query.py source vfs_read --file fs\read_write.c --output .\tools\vfs_read_bundle.c
```

递归深度默认是 4 层，可以调整：

```powershell
python .\tools\linux_meta_query.py source vfs_read --file fs\read_write.c --max-nesting-depth 6
```

如果不指定 `--output`，默认输出：

```text
<函数名>_source_bundle.c
```

例如：

```powershell
python .\tools\linux_meta_query.py source do_sys_openat2 --file fs\open.c
```

会生成：

```text
do_sys_openat2_source_bundle.c
```

生成的 `.c` 文件是分析包，便于阅读和后续处理，不保证能直接作为 Linux 编译单元编译。

### 2. calls：输出上层调用链路

输入函数名，递归查找“哪些上层函数调用了目标函数”，并用 `a -> b -> 目标函数` 的形式在命令行打印完整链路。

```powershell
python .\tools\linux_meta_query.py calls vfs_read --file fs\read_write.c --max-depth 3
```

输出示例：

```text
Target: vfs_read (LINUX-7.0\FS\READ_WRITE.C:554-583)
1. SYSCALL_DEFINE3 -> ksys_read -> vfs_read
   SYSCALL_DEFINE3@LINUX-7.0\FS\READ_WRITE.C:726 | ksys_read@LINUX-7.0\FS\READ_WRITE.C:717
```

### 3. params：输出函数入参约束和格式

输入函数名，根据函数上下文在命令行打印入参类型、格式、约束和证据行。

```powershell
python .\tools\linux_meta_query.py params vfs_read --file fs\read_write.c
```

输出示例：

```text
Parameter: buf
Type/format: char __user *buf
Constraints:
- 指针参数；需要调用者提供有效地址，除非函数显式允许 NULL。
- 用户态指针；通常需要配合 access_ok/copy_*_user 等检查。
- 通过 access_ok 校验可访问范围。
Evidence:
- 562:     if (unlikely(!access_ok(buf, count)))
```

## 兼容的完整报告接口

旧用法仍然保留，会自动走 `report`：

```powershell
python .\tools\linux_meta_query.py vfs_read --file fs\read_write.c
```

等价于：

```powershell
python .\tools\linux_meta_query.py report vfs_read --file fs\read_write.c
```

完整报告也支持 JSON：

```powershell
python .\tools\linux_meta_query.py report vfs_read --file fs\read_write.c --format json
```

## Python API 用法

也可以在其他 Python 脚本中直接 import 三个接口：

```python
from src.linux_meta_query import (
    export_source_bundle,
    print_function_call_sequence,
    print_function_param_constraints,
)

export_source_bundle(
    "vfs_read",
    output="vfs_read_bundle.c",
    file_filter=r"fs\read_write.c",
    max_nesting_depth=4,
)

print_function_call_sequence(
    "vfs_read",
    file_filter=r"fs\read_write.c",
    max_depth=3,
)

print_function_param_constraints(
    "vfs_read",
    file_filter=r"fs\read_write.c",
)
```

## 通用参数

`function`
: 必填，待查询函数名，例如 `vfs_read`、`start_kernel`、`do_sys_openat2`。

`--repo`
: Linux 源码根目录，默认 `linux-7.0`。

`--db`
: SQLite 元数据库路径。支持直接传 `BROWSE.VC.DB` 文件、包含该文件的 `.vscode` 目录，或包含 `.vscode/BROWSE.VC.DB` 的源码根目录。指定 DB 后脚本会尽量自动推断源码根目录。

示例：

```powershell
python .\tools\linux_meta_query.py calls can_send --db .\linux-7.0\.vscode\BROWSE.VC.DB --file net\can\af_can.c
python .\tools\linux_meta_query.py calls can_send --db .\linux-7.0\.vscode --file net\can\af_can.c
python .\tools\linux_meta_query.py calls can_send --db .\linux-7.0 --file net\can\af_can.c
```

`--file`
: 源文件路径子串，用来在同名函数中选择目标定义，例如 `fs\read_write.c`。

`--max-deps`
: 每类依赖最多输出多少项。`calls`、`params`、`report` 默认 `20`；`source` 默认 `200`，用于容纳递归嵌套类型。

`--max-candidates`
: 同名函数候选最多保留多少项，默认 `12`。

`--max-snippet-lines`
: 结构体、宏、变量等依赖项的源码片段最多输出多少行，默认 `80`。

`--max-depth`
: 仅 `calls` 子命令使用，控制向上追溯调用者的最大层数，默认 `5`。

`--max-chains`
: 仅 `calls` 子命令使用，控制最多打印多少条调用链，默认 `200`。

`--max-callers-per-level`
: 仅 `calls` 子命令使用，控制每个函数名最多展开多少个直接调用者，默认 `80`。

`calls` 性能说明
: 调用链分析会按层批量搜索待查函数名。遇到 `can_send` 这类上游调用链较多的函数时，可用 `--max-depth`、`--max-chains`、`--max-callers-per-level` 控制输出规模。

`--no-macros`
: 完整报告中的内部调用序列可用该选项跳过大写宏风格调用。

`--max-nesting-depth`
: 仅 `source` 子命令使用，控制结构体/union/enum/typedef 递归解析层数，默认 `4`。

## 实际验证案例

### 案例 1：vfs_read 生成 .c 分析包

命令：

```powershell
python .\tools\linux_meta_query.py source vfs_read --file fs\read_write.c --max-deps 4 --max-snippet-lines 8 --output .\test\fixtures\vfs_read_bundle_test.c
```

验证结果：

```text
Wrote source bundle: test\fixtures\vfs_read_bundle_test.c
```

生成文件包含：

```text
Target function:
  LINUX-7.0\FS\READ_WRITE.C:554-583

Constants/macros:
  EBADF
  EFAULT
  EINVAL
  FMODE_READ
  FMODE_CAN_READ
  MAX_RW_COUNT

Typedefs:
  loff_t
  size_t
  ssize_t
  fmode_t
  spinlock_t

Structures:
  struct file
  LINUX-7.0\INCLUDE\LINUX\FS.H:1259

Nested structures:
  struct file_operations
  struct address_space
  struct inode
  struct cred
  struct path
```

### 案例 2：vfs_read 输出上层调用链路

命令：

```powershell
python .\tools\linux_meta_query.py calls vfs_read --file fs\read_write.c --max-depth 3 --max-chains 20 --max-callers-per-level 20
```

### 案例 2.1：can_send 上层调用链路性能验证

命令：

```powershell
python .\tools\linux_meta_query.py calls can_send --file net\can\af_can.c
```

验证结果摘要：

```text
Target: can_send (LINUX-7.0\NET\CAN\AF_CAN.C:202-300)
1. can_can_gw_rcv -> can_send
2. isotp_sendmsg -> can_send
3. raw_sendmsg -> can_send
4. bcm_tx_timeout_handler -> bcm_can_tx -> can_send
...
```

该案例用于验证上游链路较多时不会卡死。需要缩小输出时可执行：

```powershell
python .\tools\linux_meta_query.py calls can_send --file net\can\af_can.c --max-depth 3 --max-chains 30 --max-callers-per-level 30
```

验证结果摘要：

```text
1. elf_fdpic_map_file -> elf_fdpic_map_file_constdisp_on_uclinux -> read_code -> vfs_read
2. load_flat_binary -> load_flat_file -> read_code -> vfs_read
3. SYSCALL_DEFINE3 -> ksys_read -> vfs_read
4. SYSCALL_DEFINE4 -> ksys_pread64 -> vfs_read
```

### 案例 3：vfs_read 输出入参约束

命令：

```powershell
python .\tools\linux_meta_query.py params vfs_read --file fs\read_write.c
```

验证结果摘要：

```text
file:
  Type/format: file *file
  约束：指针参数，会通过 file->f_mode、file->f_op 解引用。

buf:
  Type/format: char __user *buf
  约束：用户态指针，通过 access_ok(buf, count) 校验访问范围。

count:
  Type/format: size_t count
  约束：参与 access_ok；当 count > MAX_RW_COUNT 时被截断为 MAX_RW_COUNT。

pos:
  Type/format: loff_t *pos
  约束：指针参数，会传递给 rw_verify_area/new_sync_read/read 回调。
```

### 案例 4：start_kernel 上层调用链路

命令：

```powershell
python .\tools\linux_meta_query.py calls start_kernel --file init\main.c --max-depth 3
```

验证点：

```text
函数位置:
  LINUX-7.0\INIT\MAIN.C:1008-1219

调用链路说明:
  start_kernel 是内核启动入口之一，通常不会在 C 函数层面找到更上层普通函数调用者。
  如果没有上层调用者，脚本会输出 No upstream callers found.
```

### 案例 5：do_sys_openat2 结构体和入参约束

命令：

```powershell
python .\tools\linux_meta_query.py source do_sys_openat2 --file fs\open.c --max-deps 8 --max-snippet-lines 12
python .\tools\linux_meta_query.py calls do_sys_openat2 --file fs\open.c
python .\tools\linux_meta_query.py params do_sys_openat2 --file fs\open.c
```

验证结果摘要：

```text
函数位置:
  LINUX-7.0\FS\OPEN.C:1357-1367

关键结构体:
  struct open_how
  LINUX-7.0\INCLUDE\UAPI\LINUX\OPENAT2.H:19

  struct open_flags
  LINUX-7.0\FS\INTERNAL.H:186

上层调用链路:
  SYSCALL_DEFINE3 -> do_sys_open -> do_sys_openat2
  SYSCALL_DEFINE4 -> do_sys_open -> do_sys_openat2
  COMPAT_SYSCALL_DEFINE3 -> do_sys_open -> do_sys_openat2
  SYSCALL_DEFINE4 -> do_sys_openat2

入参约束:
  dfd:
    int 类型；本函数体内没有显式比较约束。

  filename:
    const char __user *，只读用户态路径指针。

  how:
    struct open_how *，会通过 how->flags 解引用，也会传入 build_open_flags()。
```

## 已知边界

当前数据库中 `symbols`、`symbol_refs`、`symbol_relations` 是空表，因此脚本无法直接从数据库获得精确调用图。现在采用的策略是：

1. 用 `code_items` 和 `files` 从 SQLite 中定位函数、参数、结构体、宏、变量等定义。
2. 读取函数源码，用启发式规则提取直接调用和入参约束。
3. 对函数指针调用，例如 `file->f_op->read()`，保留调用表达式但不强行解析到某个全局函数。
4. 对同名函数，建议使用 `--file` 指定路径子串，避免命中声明、架构实现或 `tools/` 测试代码。

如果后续数据库能生成 `symbol_refs` 或 `symbol_relations`，脚本可以扩展为更精确的调用图分析。
