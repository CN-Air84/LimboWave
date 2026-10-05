"""PyInstaller 打包器：把 LimboWave 搓成可分发的 Windows 应用。

对应第一个测试版的发布需求：
- 关于页内容可填写：在 GUI 里填写，随产物注入 about.json，应用「关于」页直接展示；
- 图标可选：``.ico`` 直接用，位图用 Pillow 转，``.svg`` 经 QtSvg 栅格化再转；
- 打包方式可选：标准目录（--onedir，默认，启动快）或单文件（--onefile，分发方便）；
- 默认隐藏 cmd 窗口（--windowed），调试时可勾选保留控制台；
- UPX 压缩可选：不启用时显式传 ``--noupx``（PyInstaller 发现 UPX 会默认启用，
  必须显式关掉，「可选」才是真的可选）。

用法：
  uv run python scripts/packager.py                # 图形界面
  uv run python scripts/packager.py --cli --help   # 命令行（适合脚本与 CI）

产物在 ``dist/pyinstaller/`` 下，与 package.ps1 的便携包互不覆盖。
用户资料库始终在 %LOCALAPPDATA%\\LimboWave，不在产物目录里。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
DEFAULT_DIST = REPO_ROOT / "dist" / "pyinstaller"
WORK_DIR = REPO_ROOT / "build" / "packager"

# 代码里用 Path(__file__) 相对加载的文件资源，PyInstaller 的静态分析看不到，
# 必须显式收集；目标目录与包内相对路径一一对应。
DATA_BUNDLES: tuple[tuple[Path, str], ...] = (
    (SRC_DIR / "limbowave/ui/assets", "limbowave/ui/assets"),
    (SRC_DIR / "limbowave/infrastructure/extensions", "limbowave/infrastructure/extensions"),
    (
        SRC_DIR / "limbowave/infrastructure/crypto/hello_check.ps1",
        "limbowave/infrastructure/crypto",
    ),
)

_ICON_SIZES = ((256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16))
_QT_APP_HOLDER: list[object] = []  # 保住 QGuiApplication 引用，防止被回收后崩溃


class BuildError(RuntimeError):
    """打包失败（命令非零退出 / 资源准备失败）。"""


@dataclass(frozen=True)
class BuildOptions:
    name: str = "LimboWave"
    onefile: bool = False
    hide_console: bool = True
    icon: Path | None = None
    use_upx: bool = False
    upx_dir: Path | None = None
    about: Path | None = None
    dist_dir: Path = DEFAULT_DIST


def write_entry_script(work_dir: Path) -> Path:
    """PyInstaller 需要一个脚本入口；垫片等价于 ``python -m limbowave``。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    entry = work_dir / "entry_app.py"
    entry.write_text(
        '"""PyInstaller 入口垫片（打包器生成）。"""\n'
        "from multiprocessing import freeze_support\n"
        "from limbowave.cli import main\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    freeze_support()\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return entry


def build_arguments(
    options: BuildOptions, work_dir: Path, *, platform: str | None = None
) -> list[str]:
    """拼出 PyInstaller 命令。除生成入口垫片外是纯函数，方便单测。"""
    host = platform or sys.platform
    separator = ";" if host == "win32" else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", options.name,
        "--distpath", str(options.dist_dir),
        "--workpath", str(work_dir / "build"),
        "--specpath", str(work_dir),
        "--paths", str(SRC_DIR),
    ]
    for source, target in DATA_BUNDLES:
        args += ["--add-data", f"{source}{separator}{target}"]
    if options.about is not None:
        args += ["--add-data", f"{options.about}{separator}."]
    if options.icon is not None and host == "win32":
        args += ["--icon", str(options.icon)]
    if options.hide_console and host == "win32":
        args.append("--windowed")
    if options.use_upx:
        if options.upx_dir is not None:
            args += ["--upx-dir", str(options.upx_dir)]
    else:
        args.append("--noupx")
    if options.onefile:
        args.append("--onefile")
    args.append(str(write_entry_script(work_dir)))
    return args


def exe_path(options: BuildOptions, *, platform: str | None = None) -> Path:
    suffix = ".exe" if (platform or sys.platform) == "win32" else ""
    exe_name = f"{options.name}{suffix}"
    if options.onefile:
        return options.dist_dir / exe_name
    return options.dist_dir / options.name / exe_name


def prepare_icon(source: Path, work_dir: Path) -> Path:
    """把 .ico / 位图 / .svg 统一转成 PyInstaller 可用的 .ico，返回工作副本。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "app.ico"
    if source.suffix.lower() == ".ico":
        shutil.copyfile(source, target)
        return target
    image = _render_svg(source) if source.suffix.lower() == ".svg" else Image.open(source)

    width, height = image.size
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    image = image.convert("RGBA").crop((left, top, left + side, top + side))
    sizes = [size for size in _ICON_SIZES if size[0] <= side] or [(16, 16)]
    image.save(target, format="ICO", sizes=sizes)
    return target


def _render_svg(source: Path, size: int = 256) -> Image.Image:
    """用 QtSvg 把矢量图标栅格化成 Pillow 图像（打包环境自带 PySide6）。"""
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    if QGuiApplication.instance() is None:
        _QT_APP_HOLDER.append(QGuiApplication(["limbowave-packager", "-platform", "offscreen"]))
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise BuildError(f"无法解析 SVG 图标：{source}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data())))


def run_build(
    options: BuildOptions,
    work_dir: Path,
    log: Callable[[str], None] = print,
    on_spawn: Callable[[subprocess.Popen[str]], None] = lambda process: None,
) -> Path:
    """同步执行打包；返回 exe 路径，失败抛 BuildError。输出逐行回调 log。

    on_spawn 在启动子进程后立刻被调用（拿到句柄以便取消）；它运行在调用线程。
    """
    missing = _missing_dependency()
    if missing is not None:
        raise BuildError(missing)
    work_dir.mkdir(parents=True, exist_ok=True)
    icon = options.icon
    if icon is not None and sys.platform != "win32":
        log("Linux ELF 不嵌入 ICO；请在 .desktop 启动器中指定 PNG/SVG 图标。")
        icon = None
    if icon is not None and icon.suffix.lower() != ".ico":
        log(f"==> 转换图标：{icon}")
        icon = prepare_icon(icon, work_dir / "icon")
    args = build_arguments(replace(options, icon=icon), work_dir)
    log("$ " + subprocess.list2cmdline(args))
    process = subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    on_spawn(process)
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip("\n"))
    if process.wait() != 0:
        raise BuildError(f"PyInstaller 退出码 {process.returncode}，详见上方日志。")
    exe = exe_path(options)
    if not exe.is_file():
        raise BuildError(f"PyInstaller 报告成功但找不到产物：{exe}")
    return exe


def _missing_dependency() -> str | None:
    if importlib.util.find_spec("PyInstaller") is None:
        return "未找到 PyInstaller。请先安装开发依赖：uv sync（pyinstaller 已在 dev 依赖组）。"
    return None


# ---------- 命令行模式 ----------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="packager.py --cli",
        description="LimboWave PyInstaller 打包器（命令行模式）。",
    )
    parser.add_argument("--name", default="LimboWave", help="应用名（默认 LimboWave）")
    parser.add_argument(
        "--mode", choices=("onedir", "onefile"), default="onedir",
        help="打包方式：onedir=标准目录（默认），onefile=单文件",
    )
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（默认隐藏 cmd）")
    parser.add_argument("--icon", type=Path, default=None, help="应用图标（.ico/.png/.jpg/.svg）")
    parser.add_argument("--upx", action="store_true", help="启用 UPX 压缩")
    parser.add_argument("--upx-dir", type=Path, default=None, help="UPX 所在目录（默认查 PATH）")
    parser.add_argument("--about", type=Path, default=None, help="要注入的 about.json 路径")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST, help="输出目录")
    return parser.parse_args(argv)


def stage_about(source: Path) -> Path:
    """--add-data 会保留源文件名，而应用只认 about.json；先统一暂存为规范名。"""
    if source.name == "about.json":
        return source
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    staged = WORK_DIR / "about.json"
    shutil.copyfile(source, staged)
    return staged


def options_from_cli(ns: argparse.Namespace) -> BuildOptions:
    """CLI 参数允许写相对路径，但 PyInstaller 的 --add-data 按 --specpath 解析
    相对路径，所以这里先统一转成绝对路径。"""
    return BuildOptions(
        name=ns.name,
        onefile=ns.mode == "onefile",
        hide_console=not ns.console,
        icon=ns.icon.resolve() if ns.icon is not None else None,
        use_upx=ns.upx,
        upx_dir=ns.upx_dir.resolve() if ns.upx_dir is not None else None,
        about=ns.about.resolve() if ns.about is not None else None,
        dist_dir=ns.dist.resolve(),
    )


def cli(argv: list[str]) -> int:
    options = options_from_cli(_parse_args(argv))
    if options.about is not None:
        try:
            options = replace(options, about=stage_about(options.about))
        except OSError as exc:
            print(f"打包失败：无法暂存关于页内容：{exc}", file=sys.stderr)
            return 1
    try:
        exe = run_build(options, WORK_DIR)
    except BuildError as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1
    print(f"\n打包完成：{exe}")
    return 0


# ---------- 图形界面 ----------


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from limbowave import __version__
    from limbowave.ui.about_content import (
        ABOUT_FILENAME,
        AboutContent,
        AboutField,
        AboutSection,
        default_content,
    )

    class GuiApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            root.title("LimboWave 打包器")
            root.minsize(720, 660)
            self.process: subprocess.Popen[str] | None = None
            self.events: queue.Queue[tuple[str, str]] = queue.Queue()
            self._pending_footer: str | None = None

            defaults = default_content()
            # fields[(小节标题, 字段名)] → 输入框变量；空值渲染时回退默认占位文案。
            self.fields: dict[tuple[str, str], tk.StringVar] = {}
            for section in defaults.sections:
                for field in section.fields:
                    initial = __version__ if field.label == "当前版本" else ""
                    self.fields[(section.title, field.label)] = tk.StringVar(value=initial)

            self.name = tk.StringVar(value="LimboWave")
            self.onefile = tk.BooleanVar(value=False)
            self.hide_console = tk.BooleanVar(value=True)
            self.icon = tk.StringVar(value="")
            self.use_upx = tk.BooleanVar(value=False)
            self.upx_dir = tk.StringVar(value="")
            self.tagline = tk.StringVar(value="")
            self.notice = tk.StringVar(value="")
            self._load_settings()
            self._build_layout()
            root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- 布局 ----

        def _build_layout(self) -> None:
            outer = ttk.Frame(self.root, padding=12)
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(2, weight=1)

            basic = ttk.LabelFrame(outer, text=" 打包选项 ", padding=10)
            basic.grid(row=0, column=0, sticky="ew")
            basic.columnconfigure(1, weight=1)
            self._grid_entry(basic, 0, "应用名称", self.name)
            ttk.Label(basic, text="打包方式").grid(row=1, column=0, sticky="w", pady=2)
            modes = ttk.Frame(basic)
            modes.grid(row=1, column=1, sticky="w")
            ttk.Radiobutton(
                modes, text="标准目录（启动快）", variable=self.onefile, value=False,
            ).pack(side="left")
            ttk.Radiobutton(
                modes, text="单文件 exe（分发方便，首次启动慢）", variable=self.onefile, value=True,
            ).pack(side="left", padx=(16, 0))
            ttk.Checkbutton(
                basic, text="隐藏控制台窗口（调试时取消勾选）", variable=self.hide_console,
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
            self._grid_picker(basic, 3, "应用图标", self.icon, self._pick_icon)
            upx_box = ttk.Frame(basic)
            upx_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)
            ttk.Checkbutton(upx_box, text="启用 UPX 压缩", variable=self.use_upx).pack(side="left")
            ttk.Label(upx_box, text="UPX 目录（留空查 PATH）").pack(side="left", padx=(16, 6))
            ttk.Entry(upx_box, textvariable=self.upx_dir).pack(side="left", fill="x", expand=True)
            ttk.Button(upx_box, text="浏览…", width=6, command=self._pick_upx_dir).pack(
                side="left", padx=(6, 0))

            about_frame = ttk.LabelFrame(
                outer, text=f" 关于页内容（留空显示默认占位；随产物写入 {ABOUT_FILENAME}） ",
                padding=10,
            )
            about_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            about_frame.columnconfigure(0, weight=1)
            self._build_about_form(about_frame)

            log_frame = ttk.LabelFrame(outer, text=" 构建日志 ", padding=6)
            log_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log_text = scrolledtext.ScrolledText(
                log_frame, height=10, state="disabled", font=("Consolas", 9),
            )
            self.log_text.grid(row=0, column=0, sticky="nsew")

            actions = ttk.Frame(outer)
            actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            self.build_button = ttk.Button(actions, text="开始打包", command=self._start_build)
            self.build_button.pack(side="left")
            self.cancel_button = ttk.Button(
                actions, text="取消", command=self._cancel_build, state="disabled",
            )
            self.cancel_button.pack(side="left", padx=(8, 0))
            self.status = ttk.Label(actions, text="就绪。")
            self.status.pack(side="left", padx=(16, 0))

        def _build_about_form(self, parent: ttk.Frame) -> None:
            canvas = tk.Canvas(parent, height=240, highlightthickness=0)
            scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, padding=2)
            inner.bind(
                "<Configure>",
                lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
            )
            window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.bind(
                "<Configure>",
                lambda _event: canvas.itemconfigure(window_id, width=canvas.winfo_width()),
            )
            canvas.configure(yscrollcommand=scroll.set)
            # 滚轮跟随鼠标位置滚动表单；打包器只有这一个窗口，bind_all 足够。
            canvas.bind_all(
                "<MouseWheel>",
                lambda event: canvas.yview_scroll(-event.delta // 120, "units"),
            )
            canvas.grid(row=0, column=0, sticky="ew")
            scroll.grid(row=0, column=1, sticky="ns")

            for row, (label, var) in enumerate((
                ("一句话介绍", self.tagline),
                ("顶部提示", self.notice),
            )):
                ttk.Label(inner, text=label).grid(row=row, column=0, sticky="w", pady=2)
                ttk.Entry(inner, textvariable=var).grid(
                    row=row, column=1, sticky="ew", pady=2, padx=(12, 0))
            ttk.Label(inner, text="页脚寄语").grid(row=2, column=0, sticky="nw", pady=2)
            self.footer_text = tk.Text(inner, height=2)
            self.footer_text.grid(row=2, column=1, sticky="ew", pady=2, padx=(12, 0))
            if self._pending_footer is not None:
                self.footer_text.insert("1.0", self._pending_footer)
                self._pending_footer = None

            buttons = ttk.Frame(parent)
            buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
            ttk.Button(buttons, text="载入 JSON…", command=self._load_about).pack(side="left")
            ttk.Button(buttons, text="保存 JSON…", command=self._save_about).pack(
                side="left", padx=(8, 0))
            ttk.Label(
                buttons, foreground="gray",
                text="需要自定义小节可先保存再手工编辑 JSON，打包时未知小节原样保留。",
            ).pack(side="left", padx=(16, 0))

            row = 3
            for section in default_content().sections:
                ttk.Label(inner, text=f"◆ {section.title}", font=("", 10, "bold")).grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
                row += 1
                for field in section.fields:
                    ttk.Label(inner, text=field.label).grid(row=row, column=0, sticky="w", pady=1)
                    ttk.Entry(
                        inner, textvariable=self.fields[(section.title, field.label)],
                    ).grid(row=row, column=1, sticky="ew", pady=1, padx=(12, 0))
                    row += 1

        def _grid_entry(
            self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar,
        ) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(parent, textvariable=var).grid(
                row=row, column=1, sticky="ew", pady=2, padx=(12, 0))

        def _grid_picker(
            self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar,
            command: Callable[[], None],
        ) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
            box = ttk.Frame(parent)
            box.grid(row=row, column=1, sticky="ew", pady=2, padx=(12, 0))
            box.columnconfigure(0, weight=1)
            ttk.Entry(box, textvariable=var).grid(row=0, column=0, sticky="ew")
            ttk.Button(box, text="浏览…", width=6, command=command).grid(
                row=0, column=1, padx=(6, 0))

        # ---- 文件选择 ----

        def _pick_icon(self) -> None:
            chosen = filedialog.askopenfilename(
                title="选择应用图标",
                filetypes=[
                    ("图标文件", "*.ico *.png *.jpg *.jpeg *.svg"),
                    ("全部文件", "*.*"),
                ],
            )
            if chosen:
                self.icon.set(chosen)

        def _pick_upx_dir(self) -> None:
            chosen = filedialog.askdirectory(title="选择 UPX 所在目录")
            if chosen:
                self.upx_dir.set(chosen)

        # ---- 关于页内容 ----

        def _collect_content(self) -> AboutContent:
            sections = []
            for section in default_content().sections:
                sections.append(AboutSection(
                    title=section.title,
                    description=section.description,
                    fields=tuple(
                        AboutField(
                            field.label,
                            self.fields[(section.title, field.label)].get().strip(),
                        )
                        for field in section.fields
                    ),
                    actions=section.actions,
                ))
            return AboutContent(
                tagline=self.tagline.get().strip(),
                notice=self.notice.get().strip(),
                footer=self.footer_text.get("1.0", "end").rstrip("\n"),
                sections=tuple(sections),
            )

        def _apply_content(self, content: AboutContent) -> None:
            self.tagline.set(content.tagline)
            self.notice.set(content.notice)
            self.footer_text.delete("1.0", "end")
            self.footer_text.insert("1.0", content.footer)
            loaded = {
                (section.title, field.label): field.value
                for section in content.sections
                for field in section.fields
            }
            for key, var in self.fields.items():
                value = loaded.get(key, "")
                # 默认占位文案（「待填写 · …」）不算已填写，清空让运行时回退。
                var.set("" if value.startswith("待填写") else value)

        def _load_about(self) -> None:
            chosen = filedialog.askopenfilename(
                title="载入关于页内容",
                filetypes=[("JSON 文件", "*.json"), ("全部文件", "*.*")],
            )
            if not chosen:
                return
            try:
                content = AboutContent.load(Path(chosen))
            except (OSError, ValueError) as exc:
                messagebox.showerror("载入失败", f"{chosen}\n\n{exc}")
                return
            self._apply_content(content)

        def _save_about(self) -> None:
            chosen = filedialog.asksaveasfilename(
                title="保存关于页内容", defaultextension=".json",
                initialfile=ABOUT_FILENAME, filetypes=[("JSON 文件", "*.json")],
            )
            if not chosen:
                return
            self._write_about(Path(chosen))
            messagebox.showinfo("已保存", chosen)

        def _write_about(self, path: Path) -> None:
            path.write_text(
                json.dumps(self._collect_content().to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # ---- 设置记忆（跨次发布不用重填） ----

        def _settings_path(self) -> Path:
            from platformdirs import user_data_dir

            return Path(user_data_dir("LimboWave", appauthor=False)) / "packager-gui.json"

        def _load_settings(self) -> None:
            try:
                data = json.loads(self._settings_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            self.name.set(data.get("name", "LimboWave"))
            self.onefile.set(bool(data.get("onefile", False)))
            self.hide_console.set(bool(data.get("hide_console", True)))
            self.icon.set(data.get("icon", ""))
            self.use_upx.set(bool(data.get("use_upx", False)))
            self.upx_dir.set(data.get("upx_dir", ""))
            self.tagline.set(data.get("tagline", ""))
            self.notice.set(data.get("notice", ""))
            footer = data.get("footer")
            if footer is not None:
                self._pending_footer = str(footer)
            saved_fields = data.get("fields", {})
            for (title, label), var in self.fields.items():
                saved = saved_fields.get(f"{title}|{label}")
                if saved is not None and not (label == "当前版本" and not saved):
                    var.set(str(saved))

        def _save_settings(self) -> None:
            data = {
                "name": self.name.get(),
                "onefile": self.onefile.get(),
                "hide_console": self.hide_console.get(),
                "icon": self.icon.get(),
                "use_upx": self.use_upx.get(),
                "upx_dir": self.upx_dir.get(),
                "tagline": self.tagline.get(),
                "notice": self.notice.get(),
                "footer": self.footer_text.get("1.0", "end").rstrip("\n"),
                "fields": {
                    f"{title}|{label}": var.get() for (title, label), var in self.fields.items()
                },
            }
            try:
                path = self._settings_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass  # 记不住设置不影响打包

        # ---- 打包 ----

        def _collect_options(self) -> tuple[BuildOptions | None, str | None]:
            about = WORK_DIR / ABOUT_FILENAME
            try:
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                self._write_about(about)
            except OSError as exc:
                return None, f"写入 {ABOUT_FILENAME} 失败：{exc}"
            icon_text = self.icon.get().strip()
            icon = Path(icon_text).resolve() if icon_text else None
            if icon is not None and not icon.is_file():
                return None, f"图标文件不存在：{icon}"
            upx_text = self.upx_dir.get().strip()
            upx_dir = Path(upx_text).resolve() if upx_text else None
            if self.use_upx.get() and upx_dir is not None and not upx_dir.is_dir():
                return None, f"UPX 目录不存在：{upx_dir}"
            options = BuildOptions(
                name=self.name.get().strip() or "LimboWave",
                onefile=self.onefile.get(),
                hide_console=self.hide_console.get(),
                icon=icon,
                use_upx=self.use_upx.get(),
                upx_dir=upx_dir,
                about=about,
            )
            return options, None

        def _start_build(self) -> None:
            options, error = self._collect_options()
            if options is None:
                messagebox.showerror("无法开始", error or "未知错误")
                return
            self._save_settings()
            self._append_log("")
            self.build_button.config(state="disabled")
            self.cancel_button.config(state="normal")
            self.status.config(text="正在打包…")
            events = self.events
            work_dir = WORK_DIR

            def worker() -> None:
                try:
                    # on_spawn 只赋值进程句柄（跨线程安全），不碰任何 Tk 控件。
                    exe = run_build(
                        options, work_dir,
                        log=lambda line: events.put(("log", line)),
                        on_spawn=self._remember_process,
                    )
                except BuildError as exc:
                    events.put(("fail", str(exc)))
                else:
                    events.put(("done", str(exe)))

            threading.Thread(target=worker, daemon=True).start()
            self._poll_events()

        def _remember_process(self, process: subprocess.Popen[str]) -> None:
            self.process = process

        def _cancel_build(self) -> None:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                self.status.config(text="已请求终止…")

        def _poll_events(self) -> None:
            try:
                while True:
                    kind, payload = self.events.get_nowait()
                    if kind == "log":
                        self._append_log(payload)
                    elif kind == "done":
                        self._finish_build(f"打包完成：{payload}", info=True)
                    elif kind == "fail":
                        self._finish_build(f"打包失败：{payload}", info=False)
            except queue.Empty:
                pass
            if self.build_button["state"] == "disabled":
                self.root.after(120, self._poll_events)

        def _finish_build(self, message: str, *, info: bool) -> None:
            self.build_button.config(state="normal")
            self.cancel_button.config(state="disabled")
            self.status.config(text="就绪。" if info else "失败，详见日志。")
            self._save_settings()
            if info:
                messagebox.showinfo("LimboWave 打包器", message)
            else:
                messagebox.showerror("LimboWave 打包器", message)

        def _append_log(self, line: str) -> None:
            self.log_text.config(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

        def _on_close(self) -> None:
            self._save_settings()
            if self.process is not None and self.process.poll() is None:
                if not messagebox.askokcancel("退出", "打包正在进行，确定退出？"):
                    return
                self.process.terminate()
            self.root.destroy()

    root = tk.Tk()
    app = GuiApp(root)
    app._poll_events()  # 同文件内部启动事件轮询
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # 用系统 Python 直接跑本脚本时也能找到 src 布局下的 limbowave 包。
    if importlib.util.find_spec("limbowave") is None:
        sys.path.insert(0, str(SRC_DIR))
    if "--cli" in argv:
        return cli([item for item in argv if item != "--cli"])
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
