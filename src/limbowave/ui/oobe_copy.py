"""首次使用引导的文案。

对着演示改这个文件即可。带花括号的句子会换上当前服务商：
``{name}`` 是显示名，``{url}`` 是调用地址，``{greeting}`` 按本地钟点换成问候。
每家服务商自己的「去哪复制」写在 :class:`ProviderOption.console_hint`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScreenCopy:
    """一屏上稳定出现的句子。空字符串表示这一屏没有该控件。"""

    rail: str
    eyebrow: str
    title: str
    body: str
    primary: str
    secondary: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """服务商卡片。``detail`` 是卡片副文案，``console_hint`` 是「去哪复制」。"""

    id: str
    name: str
    detail: str
    base_url: str
    console_hint: str
    custom: bool = False
    spoken: str = ""
    more: bool = False


WINDOW_TITLE = "LimboWave · 首次使用"

WELCOME_STEPS = (
    "先选择服务商，",
    "然后填入密钥，",
    "再选择模型。",
)
WELCOME_NOTE = "模型绑定、备用站点这些，可以等用过之后再去设置里看。"

DISPLAY_NAME_LABEL = "显示名"
DISPLAY_NAME_PLACEHOLDER = "留空使用调用地址的主机名"
ADDRESS_LABEL = "API地址"
KEY_LABEL = "API 密钥"
REVEAL_SHOW = "显示"
REVEAL_HIDE = "隐藏"
KEY_PLACEHOLDER = "粘贴到这里"
URL_PLACEHOLDER = "https://"
CONSOLE_HEADING = "要从哪里复制："
MORE_PROVIDERS = "更多服务商"

NEED_PROVIDER = "先选一家。"
NEED_KEY = "嗯……还没有填写密钥呢。说起来，确认可行的话，也可以空着不填哦。"
NEED_URL = "还差调用地址没有填呢。"
BAD_URL = "看起来有些问题欸……地址要以 http:// 或 https:// 开头。"
URL_MISSING = "（尚未填写）"

# 演示壳上的字，不是用户正式版会看到的产品文案。
RAIL_TITLE = "文案对照"
RAIL_NOTE = "点一项，右边换成用户会看到的那一屏。这一栏正式版没有。"
RAIL_FOOT = "模型清单和能力配置均为演示，不联网也不保存。还没选过服务商时用 DeepSeek 占位。"
RAIL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "主路径",
        (
            "intro",
            "welcome",
            "provider",
            "key",
            "key_custom",
            "checking",
            "models",
            "success",
            "done",
        ),
    ),
    ("没连上", ("error_key", "error_network", "error_timeout")),
    ("先不设置", ("browse", "later")),
)

SAMPLE_PROVIDER_ID = "deepseek"

PROVIDERS: tuple[ProviderOption, ...] = (
    ProviderOption(
        id="deepseek",
        name="DeepSeek",
        detail="深度求索 · api.deepseek.com",
        base_url="https://api.deepseek.com",
        console_hint="打开 platform.deepseek.com，在 API keys 里新建一把。",
    ),
    ProviderOption(
        id="qwen",
        name="通义千问",
        detail="阿里云 · dashscope.aliyuncs.com",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        console_hint="打开阿里云百炼控制台，新建一把 API Key。",
    ),
    ProviderOption(
        id="kimi",
        name="Kimi",
        detail="月之暗面 · api.moonshot.cn",
        base_url="https://api.moonshot.cn/v1",
        console_hint="打开 platform.moonshot.cn，在 API Key 管理里新建一把。",
    ),
    ProviderOption(
        id="glm",
        name="GLM",
        detail="智谱 · open.bigmodel.cn",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        console_hint="打开 open.bigmodel.cn，在 API 密钥里新建一把。",
    ),
    ProviderOption(
        id="minimax",
        name="MiniMax",
        detail="api.minimaxi.com",
        base_url="https://api.minimaxi.com/v1",
        console_hint="打开 MiniMax 开放平台，在接口密钥里新建一把。",
    ),
    ProviderOption(
        id="openai",
        name="OpenAI",
        detail="api.openai.com",
        base_url="https://api.openai.com/v1",
        console_hint="打开 platform.openai.com，在 API keys 里新建一把。",
    ),
    ProviderOption(
        id="claude",
        name="Claude",
        detail="Anthropic · api.anthropic.com",
        base_url="https://api.anthropic.com",
        console_hint="打开 console.anthropic.com，在 API keys 里新建一把。",
    ),
    ProviderOption(
        id="gemini",
        name="Gemini",
        detail="Google · generativelanguage.googleapis.com",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        console_hint="打开 aistudio.google.com，创建一把 API key。",
    ),
    ProviderOption(
        id="custom",
        name="自定义",
        detail="自己填调用地址",
        base_url="",
        console_hint="创建一把API Key，然后从文档里找出服务商的API调用地址。",
        custom=True,
        spoken="这家服务商",
    ),
    ProviderOption(
        id="kimi-intl",
        name="Kimi 海外",
        detail="海外 · api.moonshot.ai",
        base_url="https://api.moonshot.ai/v1",
        console_hint="打开 platform.kimi.ai，在 API keys 里新建一把。",
        spoken="Kimi",
        more=True,
    ),
    ProviderOption(
        id="glm-intl",
        name="GLM 海外",
        detail="智谱 · api.z.ai",
        base_url="https://api.z.ai/api/paas/v4",
        console_hint="打开 z.ai 开放平台，在 API 密钥里新建一把。",
        spoken="GLM",
        more=True,
    ),
    ProviderOption(
        id="minimax-intl",
        name="MiniMax 海外",
        detail="海外 · api.minimax.io",
        base_url="https://api.minimax.io/v1",
        console_hint="打开 platform.minimax.io，在接口密钥里新建一把。",
        spoken="MiniMax",
        more=True,
    ),
    ProviderOption(
        id="mimo",
        name="MiMo",
        detail="小米 · api.xiaomimimo.com",
        base_url="https://api.xiaomimimo.com/v1",
        console_hint="打开 mimo.mi.com，在 API Key 里新建一把。",
        more=True,
    ),
    ProviderOption(
        id="stepfun",
        name="阶跃星辰",
        detail="api.stepfun.com",
        base_url="https://api.stepfun.com/v1",
        console_hint="打开 platform.stepfun.com，在 API keys 里新建一把。",
        more=True,
    ),
    ProviderOption(
        id="sensenova",
        name="日日新",
        detail="商汤 · token.sensenova.cn",
        base_url="https://token.sensenova.cn/v1",
        console_hint="打开 platform.sensenova.cn，在 API Key 管理里新建一把。",
        more=True,
    ),
)

# {greeting} 按本地时间换成早上好 / 中午好 / 下午好 / 晚上好 / 夜深了，不要写死。
SCREENS: dict[str, ScreenCopy] = {
    "intro": ScreenCopy(
        rail="产品介绍",
        eyebrow="",
        title="{greeting}\n欢迎回到LimboWave。",
        body="放下心来，倾诉心中所想；让芯连心，倾听灵魂的声音。\n认出纯洁的你、记住真正的你——仅此而已。\nLimboWave/灵波，共鸣，共振。",
        primary="继续",
    ),
    "welcome": ScreenCopy(
        rail="欢迎",
        eyebrow="第一次使用",
        title="先来接入一个服务商~",
        body="选择或填写API接口，填入密钥，然后检查连接。\n都做完就可以开始聊天啦。",
        primary="开始设置",
        secondary="先看看界面",
    ),
    "provider": ScreenCopy(
        rail="选择服务商",
        eyebrow="1 / 3 · 选择服务商",
        title="选一个API接口~",
        body="调用地址先填上，然后去填密钥就好啦。",
        primary="下一步",
        secondary="上一步",
    ),
    "key": ScreenCopy(
        rail="填入密钥",
        eyebrow="2 / 3 · 填入密钥",
        title="Ctrl~V！把密钥贴进来~",
        body=(
            "到 {name} 的控制台复制一把密钥，然后整段贴到下面。\n记得检查一下有没有复制完整哦。"
            "密钥只会留在这台电脑的加密资料库里，\n不会进入聊天和日志，也不会被静默分享或上传。"
        ),
        primary="保存并配置模型",
        secondary="上一步",
    ),
    "key_custom": ScreenCopy(
        rail="自定义地址",
        eyebrow="2 / 3 · 填入密钥",
        title="填上地址和密钥",
        body=(
            "可以从服务商的文档里查找调用地址。\n"
            "调用地址以 http:// 或 https:// 开头，常见以“/v1”结尾——\n"
            "当然也有例外，比如智谱Bigmodel的/v4。\n"
            "密钥只留在这台电脑上，\n不会进入聊天和日志，也不会被静默分享或上传。\n"
        ),
        primary="保存并配置模型",
        secondary="上一步",
    ),
    "checking": ScreenCopy(
        rail="检查连接",
        eyebrow="3 / 3 · 检查连接",
        title="稍等——正在检查~",
        body="正在用这把密钥访问 {url}。确认能连上即可，还不会发送聊天。",
        primary="正在检查…",
        secondary="取消",
        hint="官方API的话通常只需要几秒钟，中转站的话或许就要等一小会啦。",
    ),
    "models": ScreenCopy(
        rail="配置实际模型",
        eyebrow="3 / 3 · 配置模型",
        title="选好模型，就可以开始了",
        body=(
            "这里和「设置 → 站点端点 → 实际模型」是同一页。\n"
            "添加模型后选择默认聊天模型；能力可手动设置，也可检测。"
        ),
        primary="使用所选模型",
        secondary="修改端点",
    ),
    "success": ScreenCopy(
        rail="连上了",
        eyebrow="开始聊天",
        title="可以开始聊天啦!",
        body=(
            "现在就可以用{name}了。你选好的模型已设为默认。\n"
            "想换模型，或再加一家做备用的话，可以到设置里调。"
        ),
        primary="开始聊天",
        secondary="调整模型",
    ),
    "done": ScreenCopy(
        rail="开始聊天",
        eyebrow="开始聊天",
        title="演示结束~来发一条消息吧！",
        body="演示到此。正式版会收起这一页，直接进入对话。",
        primary="再看一遍",
    ),
    "error_key": ScreenCopy(
        rail="密钥没通过",
        eyebrow="3 / 3 · 检查连接",
        title="啊哦……密钥有些不对劲。",
        body="看起来{name}并没有接受这把密钥呢。\n看一下是否完整，或者到控制台确认它还有效。",
        primary="返回修改",
        secondary="换一家",
    ),
    "error_network": ScreenCopy(
        rail="地址没通",
        eyebrow="3 / 3 · 检查连接",
        title="啊哦……是地址填错了吗？",
        body="尝试访问 {url} 时遇到了些问题。\n先看网络，再确认这家服务商用的是不是这个地址。",
        primary="重试",
        secondary="换一家",
    ),
    "error_timeout": ScreenCopy(
        rail="超时",
        eyebrow="3 / 3 · 检查连接",
        title="啊哦……超时了……",
        body="{name}看起来并没有在时间内回应呢。\n要再试一次吗？\n反复如此的话，或许可以稍后再来——也许换一家会更稳妥些。",
        primary="重试",
        secondary="换一家",
    ),
    "browse": ScreenCopy(
        rail="先看看界面",
        eyebrow="先不设置",
        title="要先看看界面吗？",
        body="还暂时不能发送消息呢。\n什么时候准备好了的话，从左下角「设置」进来就可以啦。",
        primary="现在就设置",
        secondary="进入界面",
    ),
    "later": ScreenCopy(
        rail="进入界面",
        eyebrow="先不设置",
        title="嗯，开始使用吧！",
        body="演示到此。正式版会进入主界面，输入框要等连接完成才能发送。",
        primary="返回欢迎页",
    ),
}


class _Fields(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def greeting_for_hour(hour: int) -> str:
    """本地钟点对应的问候。5 点前和 23 点后算夜深。"""
    if hour >= 23 or hour < 5:
        return "夜深了……"
    if hour < 11:
        return "早上好！"
    if hour < 13:
        return "中午好！"
    if hour < 18:
        return "下午好~"
    return "晚上好，"


def mask_middle(secret: str) -> str:
    """留头尾，中段换成星号。空字符串保持为空。"""
    text = secret.strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "****"
    if len(text) <= 8:
        return f"{text[0]}****{text[-1]}"
    return f"{text[:4]}****{text[-4:]}"


def provider_by_id(provider_id: str) -> ProviderOption:
    for option in PROVIDERS:
        if option.id == provider_id:
            return option
    raise KeyError(provider_id)


def present(
    screen_id: str,
    provider: ProviderOption,
    *,
    url: str = "",
    display_name: str = "",
    hour: int | None = None,
) -> ScreenCopy:
    """把当前服务商和本地时间填进这一屏的句子。"""
    raw = SCREENS[screen_id]
    fields = _Fields(
        {
            "name": display_name.strip() or provider.spoken or provider.name,
            "url": url or provider.base_url or URL_MISSING,
            "console": provider.console_hint,
            "greeting": greeting_for_hour(datetime.now().hour if hour is None else hour),
        }
    )
    return ScreenCopy(
        rail=raw.rail,
        eyebrow=_fill(raw.eyebrow, fields),
        title=_fill(raw.title, fields),
        body=_fill(raw.body, fields),
        primary=_fill(raw.primary, fields),
        secondary=_fill(raw.secondary, fields),
        hint=_fill(raw.hint, fields),
    )


def _fill(template: str, fields: _Fields) -> str:
    return template.format_map(fields)
