"""站点参数规则（§二.4 的「参数白名单与删除规则」）。

中转站的协议实现常与官方有差异：有的**不认**某些字段（传了报 400），
有的**必须**收到某个字段（不传就用不上功能）。这两件事分别是：

- **删除规则**（``strip``）：无论谁加的，发送前一律去掉。
  用于「这个站点不认 ``reasoning_effort``」这类情况。
- **参数白名单**（``whitelist``）：只允许这些字段上线，其余全部裁掉。
  空 = 不限制（默认）。用于「这个站点只接受最小集合」的严格场景。

**执行位置**：在 Pi 的 ``before_provider_request`` 钩子里改写请求体
（GATE-02 已实机验证该钩子能捕获并改写 payload）。改写在**最终请求之前**，
因此传输快照记录的是**改写后**的真实请求——参数来源依然可解释
（差异会体现在 ``param_diff.only_in_intent`` 里）。

**红线**：本模块是纯函数，不碰网络、不碰密钥。规则只增删参数**键**，
不修改值——值的来源必须可追溯（§四.4）。
"""

from __future__ import annotations

from typing import Any

# 无论站点怎么配，这些键都不允许被删除/裁剪：
# 它们决定"发给谁"和"发什么内容"，删掉会让请求失去意义甚至变危险。
PROTECTED_KEYS = frozenset({"model", "messages", "input", "contents"})


class ParamRuleError(ValueError):
    """规则配置本身有问题。

    继承 ``ValueError``：配置校验错误要和其它配置错误一样被统一捕获
    （SettingsService 与 UI 的错误路径都按 ``ValueError`` 处理）。
    """


def validate_rules(*, whitelist: tuple[str, ...], strip: tuple[str, ...]) -> None:
    """校验规则自洽。构造配置时就该拦住，而不是等发送时才炸。"""
    overlap = set(whitelist) & set(strip)
    if overlap:
        raise ParamRuleError(
            f"参数 {', '.join(sorted(overlap))} 同时出现在白名单与删除规则里，语义矛盾"
        )
    protected_stripped = PROTECTED_KEYS & set(strip)
    if protected_stripped:
        raise ParamRuleError(f"不允许删除关键参数：{', '.join(sorted(protected_stripped))}")
    if whitelist:
        missing_protected = PROTECTED_KEYS - set(whitelist)
        if missing_protected:
            raise ParamRuleError(
                "白名单必须包含关键参数："
                f"{', '.join(sorted(missing_protected))}（否则请求会失去意义）"
            )


def apply_param_rules(
    params: dict[str, Any],
    *,
    whitelist: tuple[str, ...] = (),
    strip: tuple[str, ...] = (),
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """按站点规则过滤参数。返回 ``(过滤后的参数, 被删除的键, 被白名单裁掉的键)``。

    顺序固定：**先按删除规则去掉，再按白名单裁剪**。两个清单分开返回，
    这样日志与界面能说清「每个参数是被哪条规则拿掉的」。
    """
    stripped = tuple(k for k in strip if k in params and k not in PROTECTED_KEYS)
    remaining = {k: v for k, v in params.items() if k not in stripped}

    dropped: tuple[str, ...] = ()
    if whitelist:
        allowed = set(whitelist) | PROTECTED_KEYS
        dropped = tuple(sorted(k for k in remaining if k not in allowed))
        remaining = {k: v for k, v in remaining.items() if k in allowed}

    return remaining, stripped, dropped


def rules_active(*, whitelist: tuple[str, ...], strip: tuple[str, ...]) -> bool:
    """是否有规则需要执行（两者都空 = 不改写请求）。"""
    return bool(whitelist or strip)
