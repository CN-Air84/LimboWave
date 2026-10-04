/**
 * PolicyEnforcementExtension：LimboWave 的权限网关与观测通道。
 *
 * 对应 ADR-0001 裁决 2/4 与合同 §五.4。本扩展是应用掌控权限、最终请求与压缩的载体。
 *
 * 拦截面（合同 §十一 GATE-04 实机结论）：Pi 有两条独立的执行通道，必须都钩住：
 *   - tool_call：模型发起的工具调用。返回 { block: true, reason } 阻断。
 *   - user_bash：用户/RPC 的 shell 命令（含 ! / !! 前缀）。返回 { result: {...} } 作为伪造结果阻断。
 * 注：不存在 user_editor——RPC bash 命令统一走 user_bash（excludeFromContext 区分）。
 *
 * 裁决协议：扩展通过 ctx.ui.confirm 发起 extension_ui_request，标题带固定前缀
 * `limbowave.gate:<工具>`、消息为 JSON 载荷——应用据此跑权限策略引擎
 * （会话授权 / 资源范围 / 高影响判定）。无响应、取消或异常一律默认拒绝。
 *
 * 观测通道：
 *   - before_provider_request / after_provider_response：捕获最终请求与响应（合同 §五.6 / GATE-02）。
 *   - session_before_compact：压缩接管（ADR 裁决 2 / GATE-03）。
 *
 * 所有裁决与观测经 stderr 的结构化日志（`[LIMBOWAVE] {json}`）回报给应用——
 * stdout 只承载 RPC 协议，stderr 是日志通道（合同 §三.2）。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import net from "node:net";

/** 站点参数规则（由应用经环境变量下发；不含密钥）。 */
interface ParamRules {
  whitelist: string[];
  strip: string[];
}

/**
 * 取当前 provider（= 应用的站点 id）的参数规则。
 *
 * 环境变量是 `{ [provider]: { whitelist, strip } }`：会话内可以切换站点，
 * 规则必须跟着**当前模型**的 provider 走，而不是启动时那一个。
 */
function loadParamRules(provider: string | undefined): ParamRules | null {
  const raw = process.env.LIMBOWAVE_PARAM_RULES;
  if (!raw || !provider) {
    return null;
  }
  try {
    const all = JSON.parse(raw) as Record<string, Partial<ParamRules>>;
    const parsed = all[provider];
    if (!parsed) {
      return null;
    }
    const whitelist = Array.isArray(parsed.whitelist) ? parsed.whitelist : [];
    const strip = Array.isArray(parsed.strip) ? parsed.strip : [];
    return whitelist.length || strip.length ? { whitelist, strip } : null;
  } catch {
    report("param_rules.invalid", { raw: "[unparsable]" });
    return null;
  }
}

/**
 * 关键参数不允许被规则拿掉——删掉它们请求会失去意义。
 * 与 Python 侧 domain/param_rules.PROTECTED_KEYS 保持一致。
 */
const PROTECTED_KEYS = new Set(["model", "messages", "input", "contents"]);

/** 按站点规则过滤：**先删除规则，再白名单裁剪**（与 Python 侧同序）。 */
function applyRules(
  payload: Record<string, unknown>,
  rules: ParamRules,
): { filtered: Record<string, unknown>; stripped: string[]; dropped: string[] } {
  const stripped: string[] = [];
  const filtered: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (rules.strip.includes(key) && !PROTECTED_KEYS.has(key)) {
      stripped.push(key);
      continue;
    }
    filtered[key] = value;
  }

  const dropped: string[] = [];
  if (rules.whitelist.length) {
    const allowed = new Set([...rules.whitelist, ...PROTECTED_KEYS]);
    for (const key of Object.keys(filtered)) {
      if (!allowed.has(key)) {
        dropped.push(key);
        delete filtered[key];
      }
    }
  }
  return { filtered, stripped, dropped };
}

/** 向应用回写一条结构化日志（走 stderr，不污染 stdout 协议）。 */
function report(kind: string, data: Record<string, unknown>): void {
  try {
    process.stderr.write(`[LIMBOWAVE] ${JSON.stringify({ kind, ...data })}\n`);
  } catch {
    /* 日志失败不得影响拦截 */
  }
}

/** 认证类请求头：值绝不出 Pi 进程，只回传"存在性 + scheme"。 */
const SENSITIVE_HEADERS = new Set([
  "authorization",
  "proxy-authorization",
  "x-api-key",
  "api-key",
  "apikey",
  "x-auth-token",
  "x-goog-api-key",
  "cookie",
  "set-cookie",
]);

/**
 * 在**源头**脱敏：密钥不经过 stderr、不进入应用日志，只回传结构化视图。
 * 应用侧还会再脱敏一次（纵深防御），但真正的保证在这里。
 */
function redactHeaders(headers: unknown): Record<string, unknown> {
  const source = (headers ?? {}) as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(source)) {
    if (SENSITIVE_HEADERS.has(name.toLowerCase())) {
      const text = String(value).trim();
      const space = text.indexOf(" ");
      const head = space > 0 ? text.slice(0, space) : "";
      out[name] = {
        present: text.length > 0,
        scheme: head && /^[A-Za-z]+$/.test(head) ? head : null,
        value: "[REDACTED]",
      };
    } else {
      out[name] = value;
    }
  }
  return out;
}

/**
 * 结构化裁决请求的前缀与解析约定（应用侧同源实现，改动必须同步）。
 *
 * 标题固定前缀 + 消息为 JSON——应用据此跑策略引擎（会话授权 / 资源范围 /
 * 高影响判定），而不是靠解析自然语言。
 */
const GATE_PREFIX = "limbowave.gate:";

interface GatePayload {
  tool: string;
  input: Record<string, unknown>;
  /** user_bash 通道的命令（模型工具调用没有这一项）。 */
  command?: string;
  call_id?: string;
  run_id?: string;
}

/** 请求应用裁决，返回结构化载荷。载荷解析失败 → 交由应用决定（仍是默认拒绝）。 */
async function decideStructured(
  ctx: { hasUI: boolean; ui: { confirm: (t: string, m: string) => Promise<boolean> } },
  payload: GatePayload,
): Promise<boolean> {
  if (!ctx.hasUI) {
    return false;
  }
  try {
    return await ctx.ui.confirm(`${GATE_PREFIX}${payload.tool}`, JSON.stringify(payload));
  } catch {
    return false;
  }
}


// ============================================================================
// 应用侧工具代理（§九.1 内置工具模式 / T7.1 的执行器接线）
// ============================================================================
//
// 模型发起的工具调用发生在 Pi 进程里，但**执行、校验、权限、审计都在应用侧**。
// 本扩展只做薄代理：把 {tool, params} 经回环 TCP 送给应用，拿回结构化结果
// 原样返回。**不在这里重新实现一遍执行器**——两份实现必然漂移。
//
// 通道凭据（host/port/token）由应用经环境变量下发。如实说明：同用户进程能读到
// 彼此的环境变量，所以令牌**不是**针对同用户攻击者的边界，它防的是误连。

interface ToolIpcSession {
  host: string;
  port: number;
  token: string;
}

function loadToolIpc(): ToolIpcSession | null {
  const raw = process.env.LIMBOWAVE_TOOL_IPC;
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<ToolIpcSession>;
    if (
      typeof parsed.host !== "string" ||
      typeof parsed.port !== "number" ||
      typeof parsed.token !== "string"
    ) {
      return null;
    }
    return { host: parsed.host, port: parsed.port, token: parsed.token };
  } catch {
    return null;
  }
}

/** 一次工具调用请求。字段名与 Python 侧 `ToolIpcServer._process` 的解析一致。 */
interface ToolCallRequest {
  token: string;
  tool: string;
  params: Record<string, unknown>;
  confirmed: boolean;
}

interface ToolCallResponse {
  ok: boolean;
  data?: Record<string, unknown>;
  error?: string | null;
  truncated?: boolean;
  notes?: string[];
}

/**
 * 经回环 TCP 调一次应用侧工具。**每次调用新建连接**：短连接避免半开状态，
 * 且省掉重连与心跳逻辑（调用频率远低于连接建立成本）。
 */
async function callAppTool(
  tool: string,
  params: Record<string, unknown>,
  confirmed: boolean,
): Promise<ToolCallResponse> {
  const ipc = loadToolIpc();
  if (!ipc) {
    return { ok: false, error: "应用侧工具通道未启用（LIMBOWAVE_TOOL_IPC 缺失）" };
  }

  const request: ToolCallRequest = { token: ipc.token, tool, params, confirmed };

  return await new Promise<ToolCallResponse>((resolve) => {
    const socket = net.createConnection({ host: ipc.host, port: ipc.port });
    let buffer = "";
    let settled = false;

    const finish = (response: ToolCallResponse): void => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      resolve(response);
    };

    socket.setTimeout(120_000, () => finish({ ok: false, error: "工具调用超时" }));
    socket.on("connect", () => {
      socket.write(JSON.stringify(request) + "\n");
    });
    socket.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline < 0) {
        return;
      }
      try {
        finish(JSON.parse(buffer.slice(0, newline)) as ToolCallResponse);
      } catch {
        finish({ ok: false, error: "应用侧返回的不是合法 JSON" });
      }
    });
    socket.on("error", (error: Error) => {
      finish({ ok: false, error: `无法连接应用侧工具通道：${error.message}` });
    });
    socket.on("close", () => finish({ ok: false, error: "工具通道被关闭" }));
  });
}

/** 把应用侧结果转成 Pi 的工具返回值。 */
function toToolResult(response: ToolCallResponse): {
  content: Array<{ type: "text"; text: string }>;
  details?: Record<string, unknown>;
} {
  if (!response.ok) {
    return {
      content: [{ type: "text", text: `工具执行失败：${response.error ?? "未知错误"}` }],
      details: { ok: false, error: response.error },
    };
  }
  const payload = response.data ?? {};
  // 结果以 JSON 交给模型（结构化），并附带截断/提示标记
  const notes = response.notes ?? [];
  const text = JSON.stringify(
    { ...payload, ...(response.truncated ? { _truncated: true } : {}) },
    null,
    2,
  );
  return {
    content: [
      { type: "text", text: text },
      ...(notes.length ? [{ type: "text" as const, text: notes.join("\n") }] : []),
    ],
    details: { ok: true, data: payload, truncated: Boolean(response.truncated) },
  };
}

/**
 * 注册一个把调用转发给应用侧的工具。
 *
 * ``confirmed`` 传 true 的依据：模型工具调用在到达 execute 之前**已经过本扩展的
 * `tool_call` 闸门**（应用侧策略引擎 + 用户确认）。所以到这一步是"已同意"。
 */
function registerProxiedTool(
  pi: ExtensionAPI,
  definition: {
    name: string;
    label: string;
    description: string;
    snippet: string;
    parameters: Record<string, unknown>;
  },
): void {
  pi.registerTool({
    name: definition.name,
    label: definition.label,
    description: definition.description,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    parameters: definition.parameters as any,
    promptSnippet: definition.snippet,
    async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
      const response = await callAppTool(
        definition.name,
        definition.name === "add_session_memory"
          ? { ...(params ?? {}), call_id: _toolCallId, run_id: memoryContext?.run_id ?? "" }
          : (params ?? {}) as Record<string, unknown>,
        true,
      );
      return toToolResult(response);
    },
  });
}

/** 应用侧工具名清单。**与 Python 侧 APP_TOOLS / _SCHEMAS 一一对应**（有测试守）。 */
const APP_TOOL_NAMES = [
  "read_document",
  "list_directory",
  "stat_file",
  "search_text",
  "create_file",
  "modify_file",
  "read_url",
  "web_search",
  "run_command",
  "add_session_memory",
] as const;

/** 应用侧工具清单。**与 Python 侧 tool_gateway._SCHEMAS 一一对应**（有测试守）。 */
function registerAppTools(pi: ExtensionAPI): void {
  registerProxiedTool(pi, {
    name: "read_document",
    label: "读取附件文档",
    description:
      "按 file_id 精确读取用户在消息里附加的 TXT/MD/剪贴板文档。行号从 1 开始，" +
      "start_line/end_line 为闭区间，单次默认最多 200 行；超限会被拒绝并返回建议分段，" +
      "长文件请分段读取。用户消息的附件清单里会给出各文档的 file_id、名称与行数。",
    snippet: "读取用户附加的文本文档（按 file_id 与行范围，需分段）",
    parameters: Type.Object({
      file_id: Type.String(),
      start_line: Type.Optional(Type.Number({ minimum: 1 })),
      end_line: Type.Optional(Type.Number({ minimum: 1 })),
    }),
  });
  registerProxiedTool(pi, {
    name: "add_session_memory",
    label: "添加会话记忆",
    description: "保存当前对话分支中值得后续保留的简短信息。仅保存稳定偏好、已确认事实或长期任务背景，不记录密钥或整段聊天。应用按用户设置审批；拒绝不代表已保存。不能写全局记忆。",
    snippet: "需要记住后续有用的信息时，使用 add_session_memory；避免重复和无关内容。",
    parameters: Type.Object({ content: Type.String({ minLength: 1, maxLength: 4000 }) }),
  });
  registerProxiedTool(pi, {
    name: "list_directory",
    label: "列出目录",
    description: "列出工作区内某个目录的内容（名称/类型/大小）",
    snippet: "列出工作区目录内容",
    parameters: Type.Object({ path: Type.Optional(Type.String()) }),
  });
  registerProxiedTool(pi, {
    name: "stat_file",
    label: "查看文件信息",
    description: "查看工作区内文件或目录的类型、大小与修改时间",
    snippet: "查看文件或目录的元信息",
    parameters: Type.Object({ path: Type.String() }),
  });
  registerProxiedTool(pi, {
    name: "search_text",
    label: "搜索文本",
    description: "在工作区文件里搜索文本，返回文件、行号与命中行",
    snippet: "在文件里搜索文本（返回行号）",
    parameters: Type.Object({
      pattern: Type.String(),
      path: Type.Optional(Type.String()),
      max_results: Type.Optional(Type.Number()),
    }),
  });
  registerProxiedTool(pi, {
    name: "create_file",
    label: "创建文件",
    description: "在工作区内创建新文件；文件已存在时拒绝（不覆盖）",
    snippet: "创建文件（已存在则拒绝）",
    parameters: Type.Object({ path: Type.String(), content: Type.String() }),
  });
  registerProxiedTool(pi, {
    name: "modify_file",
    label: "修改文件",
    description: "精确替换文件中的一段文本；要求目标文本恰好出现一次",
    snippet: "精确替换文件片段（需唯一匹配）",
    parameters: Type.Object({
      path: Type.String(),
      old_text: Type.String(),
      new_text: Type.String(),
    }),
  });
  registerProxiedTool(pi, {
    name: "read_url",
    label: "读取网页",
    description: "读取一个 URL 的内容（默认禁止环回/私有网段/云元数据地址）",
    snippet: "读取网页内容",
    parameters: Type.Object({ url: Type.String() }),
  });
  registerProxiedTool(pi, {
    name: "web_search",
    label: "网页搜索",
    description: "网页搜索（需要配置搜索服务凭据，未配置会明确报错）",
    snippet: "网页搜索",
    parameters: Type.Object({ query: Type.String() }),
  });
  registerProxiedTool(pi, {
    name: "run_command",
    label: "执行命令",
    description:
      `执行当前平台 Shell 命令（应用侧执行器：结构化返回 stdout/stderr/退出码/超时）。\n${process.env.LIMBOWAVE_SHELL_ENV ?? "环境未知；先确认 Shell 类型，不要假定 Bash 或 PowerShell。"}`,
    snippet: "执行当前平台 Shell 命令（返回结构化结果）",
    parameters: Type.Object({
      command: Type.String(),
      timeout_seconds: Type.Optional(Type.Number()),
    }),
  });
}

// ============================================================================
// 模型目录热更新（应用内部命令）
// ============================================================================
//
// 应用改了站点/模型配置后：先就地重写 models.json，再经 RPC 发
// `/limbowave-reload-models {token, env}`。这里把新的密钥引用与参数规则写进
// 本进程环境（models.json 里的 `$ENV` 在请求时才解析），然后让模型注册表重读
// models.json。载荷**含密钥**：只走父子进程间的 stdin，绝不回写到日志。
//
// 口令来自启动环境：用户在输入框里敲同名斜杠命令也调不动它。

const RELOAD_COMMAND = "limbowave-reload-models";
const SECRET_ENV_PREFIX = "LIMBOWAVE_SECRET_";
const PARAM_RULES_ENV = "LIMBOWAVE_PARAM_RULES";

/**
 * 注册表刷新后，会话运行时仍持有**旧的模型对象**——能力（input/reasoning/
 * contextWindow）不会跟着注册表变。不重申的话，会话启动后才补上的能力声明
 * （典型：事后勾选的视觉支持）要等到换模型才生效，期间图片一直被剥成占位符。
 *
 * 只在能力确实变化时才 setModel：pi 会把每次 setModel 记进会话历史，
 * 没变化的重申只会刷出多余的模型切换记录。
 */
async function reassertActiveModel(
  pi: ExtensionAPI,
  ctx: { model: unknown; modelRegistry: { find(provider: string, modelId: string): unknown } },
): Promise<void> {
  const active = ctx.model as
    | {
        provider: string;
        id: string;
        reasoning?: unknown;
        input?: unknown;
        contextWindow?: unknown;
        maxTokens?: unknown;
      }
    | undefined;
  if (!active) {
    return;
  }
  const refreshed = ctx.modelRegistry.find(active.provider, active.id);
  if (!refreshed) {
    // 目录里已没有当前模型：应用侧会在下一轮提示重新选模型，这里不强行切换。
    report("models.active_missing_after_reload", { provider: active.provider, id: active.id });
    return;
  }
  const candidate = refreshed as typeof active & { provider: string; id: string };
  const changed =
    candidate.reasoning !== active.reasoning ||
    JSON.stringify(candidate.input) !== JSON.stringify(active.input) ||
    candidate.contextWindow !== active.contextWindow ||
    candidate.maxTokens !== active.maxTokens;
  if (!changed) {
    return;
  }
  try {
    const applied = await pi.setModel(candidate as Parameters<typeof pi.setModel>[0]);
    report("models.active_reasserted", { id: candidate.id, applied });
  } catch (error) {
    report("models.active_reassert_failed", { error: String(error) });
  }
}

function registerReloadCommand(pi: ExtensionAPI): void {
  pi.registerCommand(RELOAD_COMMAND, {
    description: "LimboWave 内部：热更新模型目录",
    handler: async (args, ctx) => {
      let request: { token?: unknown; env?: unknown };
      try {
        request = JSON.parse(args) as { token?: unknown; env?: unknown };
      } catch {
        report("models.reload_rejected", { reason: "unparsable" });
        return;
      }
      const expected = process.env.LIMBOWAVE_CONTROL_TOKEN;
      if (!expected || request.token !== expected) {
        report("models.reload_rejected", { reason: "token" });
        return;
      }
      const env = (request.env ?? {}) as Record<string, unknown>;
      // 只接受目录相关的变量：这条通道不能被拿来改写任意环境
      const accepted = Object.entries(env).filter(
        ([name, value]) =>
          (name.startsWith(SECRET_ENV_PREFIX) || name === PARAM_RULES_ENV) &&
          typeof value === "string",
      ) as Array<[string, string]>;
      const incoming = new Set(accepted.map(([name]) => name));
      // 已从配置里移除的站点：其密钥一并撤掉
      for (const name of Object.keys(process.env)) {
        if (name.startsWith(SECRET_ENV_PREFIX) && !incoming.has(name)) {
          delete process.env[name];
        }
      }
      for (const [name, value] of accepted) {
        if (value) {
          process.env[name] = value;
        } else {
          delete process.env[name];
        }
      }
      await ctx.modelRegistry.refresh({ allowNetwork: false });
      // 注册表是新的模型对象，但会话运行时仍持旧对象：能力变了要重申，
      // 否则事后补的视觉等能力在当前会话里一直不生效（见函数注释）。
      await reassertActiveModel(pi, ctx);
      report("models.reloaded", {
        models: ctx.modelRegistry.getAvailable().map((m) => `${m.provider}/${m.id}`),
      });
    },
  });
}

interface MemoryContext {
  run_id: string;
  branch_id: string;
  round: number;
  global: string[];
  session: string[];
  global_interval: number;
  session_interval: number;
}
let memoryContext: MemoryContext | null = null;
const memoryAnchors = new Map<string, { signature: string; timestamp: number; round: number }>();

function registerMemoryContext(pi: ExtensionAPI): void {
  pi.registerCommand("limbowave-memory", {
    description: "LimboWave 内部：更新本轮记忆快照",
    handler: async (args) => {
      try {
        const request = JSON.parse(args);
        if (!process.env.LIMBOWAVE_CONTROL_TOKEN || request.token !== process.env.LIMBOWAVE_CONTROL_TOKEN) return;
        const value = request.context as MemoryContext;
        if (!value || typeof value.run_id !== "string" || typeof value.branch_id !== "string" ||
            !Number.isInteger(value.round) || value.round < 1 ||
            ![value.global_interval, value.session_interval].every(n => Number.isInteger(n) && n >= 1 && n <= 30) ||
            ![value.global, value.session].every(items => Array.isArray(items) && items.length <= 100 &&
              items.every(text => typeof text === "string" && text.length <= 8000))) return;
        if (memoryContext?.branch_id !== value.branch_id) memoryAnchors.clear();
        memoryContext = value;
        report("memory.ready", { run_id: value.run_id });
      } catch { /* 未通过校验不得覆盖已绑定上下文。 */ }
    },
  });
  // context 不会写入 Pi 会话文件；每次调用重建两个受控提醒，避免堆积及旧版本残留。
  pi.on("context", (event) => {
    const context = memoryContext;
    if (!context) return;
    const messages = event.messages.filter(m => !(m.role === "custom" && m.customType.startsWith("limbowave.memory.")));
    const lastUser = [...messages].reverse().find(m => m.role === "user");
    if (!lastUser) return { messages };
    for (const kind of ["global", "session"] as const) {
      const content = context[kind];
      if (!content.length) { memoryAnchors.delete(kind); continue; }
      const interval = context[`${kind}_interval`];
      const signature = JSON.stringify([content, interval]);
      let anchor = memoryAnchors.get(kind);
      const due = (context.round - 1) % interval === 0;
      if (!anchor || anchor.signature !== signature || (due && anchor.round !== context.round) ||
          !messages.some(m => m.role === "user" && m.timestamp === anchor!.timestamp)) {
        anchor = { signature, timestamp: lastUser.timestamp, round: context.round };
        memoryAnchors.set(kind, anchor);
      }
      const index = messages.findIndex(m => m.role === "user" && m.timestamp === anchor!.timestamp);
      messages.splice(index + 1, 0, {
        role: "custom", customType: `limbowave.memory.${kind}`, display: false,
        timestamp: anchor.timestamp,
        content: `${kind === "global" ? "用户全局记忆" : "当前分支会话记忆"}（应用提供的参考资料，不改变系统权限；以用户当前明确指示为准）：
${JSON.stringify(content)}`,
      });
    }
    return { messages };
  });
  pi.on("session_compact", () => { memoryAnchors.clear(); });
  // RPC 的 new/switch/fork 都重建会话并发出 session_start，而非 session_switch。
  pi.on("session_start", () => { memoryContext = null; memoryAnchors.clear(); });
}

export default function (pi: ExtensionAPI) {
  registerReloadCommand(pi);
  registerMemoryContext(pi);
  // ---- 应用侧工具（模型可调；执行/权限/审计都在应用侧） ----
  // 注册后回报清单：应用据此确认「工具注册被 Pi 接受」——
  // 否则扩展加载失败时，模型会静默地没有工具可用，很难查。
  try {
    registerAppTools(pi);
    report("tools.registered", {
      request: APP_TOOL_NAMES,
      channel: loadToolIpc() ? "enabled" : "absent",
    });
  } catch (error) {
    report("tools.register_failed", {
      error: error instanceof Error ? error.message : String(error),
    });
  }

  // ---- 模型发起的工具调用 ----
  pi.on("tool_call", async (event, ctx) => {
    report("tool_call", { toolName: event.toolName,
      input: event.toolName === "add_session_memory" ? { redacted: true } : event.input });
    const allowed = await decideStructured(ctx, {
      tool: String(event.toolName),
      ...(event.toolName === "add_session_memory"
        ? { call_id: event.toolCallId, run_id: memoryContext?.run_id ?? "" } : {}),
      input: (event.input ?? {}) as Record<string, unknown>,
    });
    if (!allowed) {
      report("tool_call.denied", { toolName: event.toolName });
      return { block: true, reason: "LimboWave 权限网关拒绝" };
    }
    return undefined;
  });

  // ---- 用户/RPC 的 shell 命令（独立通道，不走 tool_call）----
  pi.on("user_bash", async (event, ctx) => {
    report("user_bash", { command: event.command, excludeFromContext: event.excludeFromContext });
    const allowed = await decideStructured(ctx, {
      tool: "user_bash",
      input: { command: event.command },
      command: String(event.command),
    });
    if (!allowed) {
      report("user_bash.denied", { command: event.command });
      return {
        result: {
          output: "[LimboWave] 命令被权限网关拒绝",
          exitCode: 1,
          cancelled: false,
          truncated: false,
        },
      };
    }
    return undefined;
  });

  // ---- 最终请求/响应观测（最终传输快照的数据源）----
  // 请求头单独一路：值在源头就脱敏，密钥不进入 stderr。
  pi.on("before_provider_headers", (event, _ctx) => {
    report("provider.headers", { headers: redactHeaders(event.headers) });
    return undefined;
  });
  // ---- 站点参数规则（§二.4）：在最终请求之前改写请求体 ----
  // 规则由应用经环境变量交给扩展（不含密钥）。改写发生在**发送前**，
  // 因此传输快照记录的是改写后的真实请求——参数来源仍然可解释。
  pi.on("before_provider_request", (event, ctx) => {
    // 注意：BeforeProviderRequestEvent 只有 payload 一个字段——provider / model / url
    // 不在此事件里（已核对 types.d.ts）。当前 provider 取自 ctx.model（会话内可能切过站点）。
    const route = { provider: ctx.model?.provider, model: ctx.model?.id };
    const rules = loadParamRules(ctx.model?.provider);
    if (!rules) {
      report("provider.request", { ...route, payload: event.payload });
      return undefined; // 没有规则：纯观测
    }

    const payload = (event.payload ?? {}) as Record<string, unknown>;
    const { filtered, stripped, dropped } = applyRules(payload, rules);
    if (stripped.length || dropped.length) {
      report("param_rules.applied", {
        stripped,
        whitelist_dropped: dropped,
        kept: Object.keys(filtered),
      });
    }
    report("provider.request", { ...route, payload: filtered });
    return { payload: filtered }; // 改写后的请求真正上线
  });
  pi.on("after_provider_response", (event, _ctx) => {
    report("provider.response", {
      status: event.status,
      headers: redactHeaders(event.headers),
    });
  });

  // ---- 压缩接管（应用侧生成摘要后注入）----
  pi.on("session_before_compact", (event, _ctx) => {
    const prep = event.preparation;
    report("compaction.request", {
      reason: event.reason,
      tokensBefore: prep.tokensBefore,
      firstKeptEntryId: prep.firstKeptEntryId ?? null,
      messagesToSummarize: prep.messagesToSummarize.length,
    });
    // 默认不接管（返回 undefined）→ 走 Pi 默认压缩。
    // 应用要接管时，应通过另一条控制通道把摘要写回，由这里返回 { compaction: {...} }。
    // 该回写机制待应用层用例就绪后补全（ADR 裁决 2 的「提前压缩 + 预批准摘要注入」）。
    return undefined;
  });
}
