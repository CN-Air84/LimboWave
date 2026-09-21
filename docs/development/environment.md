# 开发环境事实与决策

本文记录**这台机器**上已经验证过的环境事实，以及由此产生的工程决策。
每条结论都标了它是怎么被验证的，方便换机器时快速判断哪些仍然成立。

验证日期：2026-09-20

---

## 一、机器事实

| 项目 | 值 | 来源 |
| --- | --- | --- |
| 操作系统 | Windows 11，`10.0.26200` | `platform.platform()` |
| 工程根目录 | `G:\LimboWave`（纯 ASCII） | — |
| uv | `0.10.12` | `uv --version` |
| git | `2.53.0.windows.1` | `git --version` |

### Python 解释器

机器上存在多个解释器，只有一个可用：

| 解释器 | 状态 |
| --- | --- |
| `D:\Software\Python\python.exe`（3.11.4） | 系统默认，版本低于要求 |
| py 启动器登记的 3.12 | **已损坏**，指向 `Accio\pre-install` 残留目录，任何调用都报 "Unable to create process" |
| uv 托管的 `cpython-3.12.13` | **采用** |

**决策：** 用 `uv` 托管解释器，`.python-version` 固定 `3.12`，`requires-python = ">=3.12,<3.14"`。
不依赖系统 `python`，也不依赖已损坏的 py 启动器登记项。

### 依赖版本（锁定于 `uv.lock`）

运行时：PySide6 6.11.2、shiboken6 6.11.2、qasync 0.28.0、httpx 0.28.1、pydantic 2.13.5、
cryptography 50.0.1、argon2-cffi 25.1.0、platformdirs 4.11.11、markdown-it-py 4.2.0、pygments 2.21.0。

开发：pytest 9.1.1、pytest-qt 4.5.0、pytest-asyncio 1.4.0、ruff 0.16.8、mypy 2.3.1。

---

## 二、PowerShell 现状（影响设计计划 §10）

### pwsh 未安装

`pwsh` 不在 PATH 上，只有 Windows PowerShell 5.1
（`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE`）。

**影响：** 设计计划 §10.1 把 PowerShell 7 定为终端模式默认、缺失时回退并**在界面明确显示**——
这条回退路径在本机不是理论分支，而是唯一路径。Phase 7 必须真实实现并在界面呈现，
不能只写代码却无从验证。

### .ps1 必须带 UTF-8 BOM（实测踩坑）

Windows PowerShell 5.1 对**无 BOM** 的脚本文件按系统 ANSI 代码页（本机 GBK）解码。
含中文的脚本因此直接解析失败：

```
字符串缺少终止符: '。
```

PowerShell 7 默认按 UTF-8 读取，且同样接受 BOM，所以 BOM 对两边都成立。

**决策：** 仓库内所有 `.ps1` 统一 **UTF-8 BOM + CRLF**，
由 `scripts/normalize-ps1.ps1`（纯 ASCII，破了约定也能跑）批量修复，
并由 `tests/unit/test_script_encoding.py` 守住。

> ⚠️ **对 Phase 7 的提醒：** 设计计划 §10.3 要求应用生成的临时 `.ps1` 用"UTF-8 无 BOM"写入。
> 实测表明这对**纯 ASCII 命令**成立，但一旦命令里含中文（例如中文路径），
> 在 PowerShell 5.1 下会被错误解码而失败。该条实现时需要按实际 shell 版本区别处理，
> 并建立真实测试用例，不能默认无 BOM 就安全。

### PowerShell 5.1 会剥离原生进程参数的引号（实测踩坑）

向原生 exe 传含引号的参数时，5.1 会吞掉引号。表现为：

```
uv run python -c "import sys; print('x', sys.version)"   # 实际收到 print(x, sys.version)
→ NameError: name 'python' is not defined
```

**决策：** 不把代码内联进 `-c`，一律写成脚本文件执行（`scripts/selfcheck.py`）。
这条与设计计划 §10.3"不要把任意长命令拼入单行"的结论一致，本机是它的一手证据。

---

## 三、其他实测结论

### platformdirs 在 Windows 上会重复一层目录

未显式指定 `appauthor` 时，platformdirs 用 `appname` 顶替，得到
`...\Local\LimboWave\LimboWave`。

**已修复：** `resolve_paths()` 显式传 `appauthor=False`，并有回归测试。
实际资料库根目录：

```
C:\Users\<用户>\AppData\Local\LimboWave
C:\Users\<用户>\AppData\Local\LimboWave\Logs
```

### uv 硬链接回退警告

`uv sync` 会提示 `Failed to hardlink files; falling back to full copy`——
uv 缓存在 C: 盘，工程在 G: 盘，跨卷无法硬链接。仅影响安装速度，不影响正确性。
如要消除警告可设 `UV_LINK_MODE=copy`。**未写入工程配置**：这是本机磁盘布局问题，不是工程约定。

### 质量门的两处有意放宽

- **ruff 关闭 `RUF001/002/003`**：这三条报告中文全角标点"有歧义"，
  而本项目注释与文档字符串就是中文写的，全角标点是正确排版。原因是这个，不是图省事。
- **mypy 对 `qasync` 定向豁免 `ignore_missing_imports`**：qasync 未发布 `py.typed`，
  上游也无 stub 包。只豁免这一个模块，未使用全局开关。

### 包名偏离设计计划

设计计划的推荐结构写的是 `src/harness/`，实际采用 `src/limbowave/`。

**原因：** `harness` 是过于通用的顶层导入名，与第三方包重名风险高；
`limbowave` 与项目名一致且唯一。子结构（domain / application / infrastructure / tools / ui）
完全按计划保留。

---

## 四、固定命令

均在工程根目录执行，兼容 Windows PowerShell 5.1：

```powershell
.\scripts\bootstrap.ps1              # 建立/修复 .venv 并安装依赖，然后自检
.\scripts\lock.ps1                   # 重算 uv.lock（-Upgrade 主动升级）
.\scripts\run.ps1                    # 启动应用
.\scripts\run.ps1 --smoke            # 冒烟：启动事件循环、打印环境与窗口状态后退出
.\scripts\test.ps1                   # 全部测试
.\scripts\test.ps1 tests/unit -k bootstrap   # 透传 pytest 参数
.\scripts\lint.ps1                   # ruff check + ruff format --check + mypy
.\scripts\verify.ps1                 # 提交前基线：静态检查 + 全部测试
```

---

## 五、验收记录

以下均已在本机实际执行并观察结果，不是推断：

| 验收项 | 结果 |
| --- | --- |
| `uv sync` 安装全部依赖 | 39 个包，`uv.lock` 生成 |
| 删除 `.venv` 后用 `uv sync --locked` 重建 | 11.4 秒完成，无需重新解析，可复现 |
| 单元测试 + Qt UI 测试 | 28 项通过 |
| ruff check / format / mypy strict | 全部通过 |
| 应用启动（真实 GUI 进程） | 窗口存在，标题 `LimboWave / 灵波`，进程响应正常 |
| 冒烟模式 | `window_visible=True`，有效原生 `win_id`，Qt 6.11.2 |
| 中文路径：应用从中文目录启动 | 通过，窗口正常 |
| 中文路径：uv 在中文路径建虚拟环境 | 通过，环境内中文文件读写正常 |
| 中文路径：Python 读写中文文件名 | 通过（含回归测试） |

### 已知边界

- Qt 从**非 ASCII 路径**加载 DLL 未被验证。本工程路径为 ASCII，且这不是本项目的需求场景；
  用户数据与文件读取路径的中文支持已在 Python 层验证，完整覆盖属于 Phase 4。
- 尚未验证打包（PyInstaller/Nuitka）后的行为，属于 Phase 10。
