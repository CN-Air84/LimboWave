# 打包发布（PyInstaller）

> 更新日期：2026-10-03

`scripts/packager.py` 用 PyInstaller 把应用打成可分发的 Windows 程序，服务于第一个
测试版的发布需求：关于页内容可填写、图标可选、单文件/标准打包方式可选、默认隐藏
cmd 窗口、UPX 压缩可选。它与 `scripts/package.ps1`（便携 Python 目录包）是两条并行
的发布路线，产物互不覆盖：

| 路线 | 产物 | 适用场景 |
| --- | --- | --- |
| `packager.py`（PyInstaller） | `dist/pyinstaller/` 下的 exe | 面向使用者的发布形态 |
| `package.ps1`（便携目录） | `dist/LimboWave/` 自带解释器 | 开发者/内测的自包含目录 |

## 使用

```powershell
uv sync                                   # pyinstaller 在 dev 依赖组
uv run python scripts/packager.py         # 图形界面
```

图形界面里可以填写：

- **打包选项**：应用名称、标准目录/单文件方式、「隐藏控制台窗口」（默认勾选，调试时
  可取消以保留 cmd 看日志/报错）、应用图标、UPX 压缩（及其所在目录）；
- **关于页内容**：一句话介绍、顶部提示、页脚寄语，以及四个小节的全部字段。留空的
  字段在运行时回退为页面默认的「待填写 · …」占位文案，只有填了的才会覆盖。

设置会记住上次填写（`%LOCALAPPDATA%\LimboWave\packager-gui.json`），跨次发布不必
重填。关于页内容也可以「保存 JSON…」导出、手工编辑后再「载入 JSON…」。

### 命令行模式

同一套能力供脚本与 CI 使用：

```powershell
uv run python scripts/packager.py --cli --mode onefile `
  --icon docs/branding/limbowave-logo.svg `
  --about build/about.json
```

参数见 `--cli --help`；`--console` 可保留控制台，`--upx` 启用 UPX 压缩。相对路径
按当前工作目录解析后转绝对路径传给 PyInstaller。

### 图标

接受 `.ico` / `.png` / `.jpg` / `.svg`。`.ico` 直接使用；位图与 SVG 自动转换（位图
走 Pillow，SVG 经 QtSvg 栅格化），居中裁方后生成 256→16 的多档图标，缓存在
`build/packager/icon/`。品牌源文件在 `docs/branding/`。

## 关于页内容的注入机制

- 打包时表单内容被写成 `about.json`，作为资源打进产物根目录；
- 运行时 `limbowave.ui.about_content` 按「环境变量 `LIMBOWAVE_ABOUT` → PyInstaller
  解包目录（`sys._MEIPASS`）→ exe 同目录」发现文件并加载；
- 加载结果与默认占位内容**按小节标题、字段名合并**：填写的显示填写值，留空的回退
  占位文案；JSON 损坏时整体回退占位页，绝不阻止启动。

开发态不用打包也能预览注入效果：

```powershell
$env:LIMBOWAVE_ABOUT = "G:\LimboWave\build\about-smoke.json"
uv run python -m limbowave
```

`about.json` 结构（`sections[].fields` 也接受 `{"label": ..., "value": ...}` 写法；
打包器只会写「打包选项 + 默认四节」的表单范围，手工编辑可自由增删小节）：

```json
{
  "tagline": "一句话介绍",
  "notice": "顶部提示",
  "footer": "页脚寄语",
  "sections": [
    {
      "title": "版本与更新",
      "description": "小节描述",
      "fields": [["当前版本", "0.0.1"]],
      "actions": ["检查更新"]
    }
  ]
}
```

## Linux 构建

在 Linux 上运行 `bash scripts/linux.sh package --console`。目录模式产物为
`dist/pyinstaller/LimboWave/LimboWave`（无 `.exe`）；资源参数使用 POSIX 分隔符，
不传 Windows `--windowed` / ICO 参数。需分发整个目录，Node/Pi 仍需单独安装。
Linux 打包与桌面验收尚待实际执行，详见 [Linux 支持说明](linux.md)。

## 实现要点与边界

- **资源显式收集**：代码里用 `Path(__file__)` 相对加载的文件（`ui/assets/*.svg`、
  `infrastructure/extensions/`、`infrastructure/crypto/hello_check.ps1`）PyInstaller
  静态分析看不到，由 `DATA_BUNDLES` 显式注入；新增此类资源要同步维护这个表。
- **UPX 默认关闭**：PyInstaller 发现 UPX 会默认启用，因此未启用时显式传 `--noupx`，
  「可选」才成立。另外 UPX 压缩常触发杀软误报，对外发布前建议先实测。
- **windowed 产物没有控制台**：隐藏 cmd 后 `secret` / `vault` 子命令无法交互输密码
  （stdin 盲等），需要 CLI 子命令时用源码方式或「保留控制台」的构建运行；GUI 主路径
  不受影响。
- **单文件方式**：每次启动要解包到临时目录，PySide6 体积下首启明显变慢；标准目录
  方式启动快、便于排障，两种方式都支持。
- **用户数据不进产物**：资料库、日志等始终在 `%LOCALAPPDATA%\LimboWave`，删除产物
  目录等于卸载，不会碰用户数据。
- 入口是 `build/packager/entry_app.py` 垫片（等价 `python -m limbowave`），打包方式
  与资源收集全部经命令行参数表达，不落 `.spec` 文件进仓库。

## 测试

- `tests/unit/test_about_content.py`：默认占位形态、JSON 往返与宽松解析、合并回退、
  注入文件发现顺序与坏文件回退；
- `tests/unit/test_packager_core.py`：命令拼装（默认隐藏 cmd、显式 `--noupx`、资源
  收集清单）、入口垫片、图标转换、CLI 相对路径归一；
- `tests/ui/test_about_page.py`：注入内容渲染与空值回退。
