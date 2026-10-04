"""关于页内容：集中维护默认占位文案，并支持打包时注入的 ``about.json`` 覆盖。

数据流：打包器（``scripts/packager.py``）把用户填写的关于页内容写成 ``about.json``
随 PyInstaller 资源打进产物；应用启动时按「环境变量 → PyInstaller 解包目录 →
exe 同目录」的顺序发现并加载它。没有文件或文件损坏时回退到这里的默认占位文案——
开发态与未注入的构建看到的都是占位页，坏文件永远不会让关于页崩溃。

开发态预览注入效果：设置环境变量 ``LIMBOWAVE_ABOUT`` 指向一个 about.json 即可。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ABOUT_ENV_VAR = "LIMBOWAVE_ABOUT"
ABOUT_FILENAME = "about.json"

DEFAULT_TAGLINE = "待填写 · 在这里写下产品的一句话介绍。"
DEFAULT_NOTICE = "内容筹备中 · 以下为信息占位，正式文案与相关入口将在后续补充。"
DEFAULT_FOOTER = "感谢相遇。这里还会有更多关于灵波的故事。\n待填写 · 页脚寄语与补充说明"


@dataclass(frozen=True)
class AboutField:
    """一行「名称 → 内容」。value 为空表示未填写，渲染时回退占位文案。"""

    label: str
    value: str = ""


@dataclass(frozen=True)
class AboutSection:
    title: str
    description: str
    fields: tuple[AboutField, ...] = ()
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AboutContent:
    tagline: str = ""
    notice: str = ""
    footer: str = ""
    sections: tuple[AboutSection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "tagline": self.tagline,
            "notice": self.notice,
            "footer": self.footer,
            "sections": [
                {
                    "title": section.title,
                    "description": section.description,
                    "fields": [[field.label, field.value] for field in section.fields],
                    "actions": list(section.actions),
                }
                for section in self.sections
            ],
        }

    @classmethod
    def from_dict(cls, data: object) -> AboutContent:
        """宽松解析：结构不对的条目直接跳过。关于页宁可缺一句也不能崩。"""
        if not isinstance(data, dict):
            return cls()
        sections: list[AboutSection] = []
        for raw in data.get("sections", ()):
            if not isinstance(raw, dict):
                continue
            title = raw.get("title")
            if not isinstance(title, str) or not title:
                continue
            description = raw.get("description")
            actions = tuple(
                action
                for action in (raw.get("actions") or ())
                if isinstance(action, str) and action
            )
            sections.append(
                AboutSection(
                    title=title,
                    description=description if isinstance(description, str) else "",
                    fields=tuple(
                        AboutField(label, value)
                        for label, value in _field_pairs(raw.get("fields"))
                    ),
                    actions=actions,
                )
            )
        return cls(
            tagline=_text(data.get("tagline")),
            notice=_text(data.get("notice")),
            footer=_text(data.get("footer")),
            sections=tuple(sections),
        )

    @classmethod
    def load(cls, path: Path) -> AboutContent:
        """严格加载：文件缺失或 JSON 损坏时抛异常（打包器要向用户报错）。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} 的顶层必须是 JSON 对象")
        return cls.from_dict(data)

    def merged(self, fallback: AboutContent) -> AboutContent:
        """以 self 优先、fallback 补空：同题小节按字段名逐项合并。

        - self 里值为空的字段回退到 fallback 同名占位；
        - fallback 独有的小节/字段整体补到后面；
        - self 新增的小节原样保留。
        """
        sections = list(self.sections)
        index_by_title = {section.title: i for i, section in enumerate(sections)}
        for base in fallback.sections:
            if base.title not in index_by_title:
                index_by_title[base.title] = len(sections)
                sections.append(base)
                continue
            index = index_by_title[base.title]
            user = sections[index]
            fields = list(user.fields)
            index_by_label = {field.label: i for i, field in enumerate(fields)}
            for base_field in base.fields:
                if base_field.label not in index_by_label:
                    index_by_label[base_field.label] = len(fields)
                    fields.append(base_field)
                elif not fields[index_by_label[base_field.label]].value:
                    fields[index_by_label[base_field.label]] = base_field
            sections[index] = AboutSection(
                title=user.title,
                description=user.description or base.description,
                fields=tuple(fields),
                actions=user.actions or base.actions,
            )
        return AboutContent(
            tagline=self.tagline or fallback.tagline,
            notice=self.notice or fallback.notice,
            footer=self.footer or fallback.footer,
            sections=tuple(sections),
        )


# 后续正式文案在这里替换；链接和更新功能接入前，不把占位符做成可点击入口。
DEFAULT_SECTIONS = (
    AboutSection("版本与更新", "版本记录、发布信息与更新入口。", (
        AboutField("当前版本", "待填写 · 应用版本号"),
        AboutField("构建信息", "待填写 · 构建编号、提交标识与构建时间"),
        AboutField("发布日期", "待填写 · 当前版本的发布日期"),
        AboutField("发布渠道", "待填写 · 稳定版、预览版等发布渠道"),
        AboutField("更新摘要", "待填写 · 本次更新亮点与完整更新记录"),
    ), ("检查更新", "查看更新日志")),
    AboutSection("资源与社区", "文档、源代码与交流空间。", (
        AboutField("项目主页", "待填写 · 官方网站或项目介绍页链接"),
        AboutField("代码仓库", "待填写 · 源代码仓库地址"),
        AboutField("使用文档", "待填写 · 入门教程与完整文档链接"),
        AboutField("常见问题", "待填写 · 常见问题与排查指南链接"),
        AboutField("社区交流", "待填写 · 讨论区、社区群或社交账号"),
    ), ("打开项目主页", "浏览代码仓库", "阅读使用文档")),
    AboutSection("隐私与数据", "预留政策说明；此处不代表正式的数据处理承诺。", (
        AboutField("隐私政策", "待填写 · 正式隐私政策的内容、版本与链接"),
        AboutField("本地数据", "待填写 · 数据保存位置、保存范围与保留周期"),
        AboutField("网络请求", "待填写 · 联网场景、第三方服务与数据流向说明"),
        AboutField("诊断信息", "待填写 · 日志内容、敏感信息处理与分享注意事项"),
        AboutField("数据管理", "待填写 · 数据导出、备份、删除与恢复说明"),
    )),
    AboutSection("法律与许可", "许可和条款确定后，在这里补充正式文本。", (
        AboutField("软件许可", "待填写 · 本项目许可证名称与完整文本"),
        AboutField("版权声明", "待填写 · 版权年份、权利人及保留权利说明"),
        AboutField("使用条款", "待填写 · 使用范围、限制与服务条款链接"),
        AboutField("免责声明", "待填写 · 适用的责任边界与风险提示"),
    )),
)


def default_content() -> AboutContent:
    """默认占位页内容；形态由 tests/ui/test_about_page.py 锁定。"""
    return AboutContent(
        tagline=DEFAULT_TAGLINE,
        notice=DEFAULT_NOTICE,
        footer=DEFAULT_FOOTER,
        sections=DEFAULT_SECTIONS,
    )


def discover_about_file() -> Path | None:
    """按约定位置查找注入的 about.json，找不到返回 None。"""
    env = os.environ.get(ABOUT_ENV_VAR)
    if env:
        path = Path(env)
        if path.is_file():
            return path
    # PyInstaller 运行时解包目录：onefile 每次解包的临时目录；onedir 为 _internal。
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        path = Path(meipass) / ABOUT_FILENAME
        if path.is_file():
            return path
    # onedir 产物也允许把 about.json 放在 exe 旁边手动覆盖。
    path = Path(sys.executable).resolve().parent / ABOUT_FILENAME
    if path.is_file():
        return path
    return None


def load_embedded() -> AboutContent:
    """应用启动时用：找不到或解析失败都回退空内容，绝不抛异常。"""
    path = discover_about_file()
    if path is None:
        return AboutContent()
    try:
        return AboutContent.load(path)
    except (OSError, ValueError):
        return AboutContent()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _field_pairs(raw: object) -> list[tuple[str, str]]:
    """字段列表兼容两种写法：["名称", "内容"] 或 {"label": ..., "value": ...}。"""
    pairs: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return pairs
    for item in raw:
        if isinstance(item, list) and len(item) == 2:
            label, value = item
            if isinstance(label, str) and label:
                pairs.append((label, value if isinstance(value, str) else ""))
        elif isinstance(item, dict):
            label = item.get("label")
            value = item.get("value")
            if isinstance(label, str) and label:
                pairs.append((label, value if isinstance(value, str) else ""))
    return pairs
