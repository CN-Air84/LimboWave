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
 * 裁决协议：扩展通过 ctx.ui.select/confirm 发起 extension_ui_request，
 * 应用在 RPC stdin 回 extension_ui_response。无响应或取消一律默认拒绝。
 *
 * 观测通道：
 *   - before_provider_request / after_provider_response：捕获最终请求与响应（合同 §五.6 / GATE-02）。
 *   - session_before_compact：压缩接管（ADR 裁决 2 / GATE-03）。
 *
 * 所有裁决与观测经 stderr 的结构化日志（`[LIMBOWAVE] {json}`）回报给应用——
 * stdout 只承载 RPC 协议，stderr 是日志通道（合同 §三.2）。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** 向应用回写一条结构化日志（走 stderr，不污染 stdout 协议）。 */
function report(kind: string, data: Record<string, unknown>): void {
  try {
    process.stderr.write(`[LIMBOWAVE] ${JSON.stringify({ kind, ...data })}\n`);
  } catch {
    /* 日志失败不得影响拦截 */
  }
}

/** 请求应用裁决。取消/超时/异常一律默认拒绝（deny）。 */
async function decide(
  ctx: { hasUI: boolean; ui: { confirm: (t: string, m: string) => Promise<boolean> } },
  title: string,
  detail: string,
): Promise<boolean> {
  if (!ctx.hasUI) {
    // RPC 模式下 hasUI 为 true（extension_ui 子协议可用）。这里是无 UI 的兜底。
    return false;
  }
  try {
    return await ctx.ui.confirm(title, detail);
  } catch {
    return false; // 应用断连/异常 → 默认拒绝
  }
}

export default function (pi: ExtensionAPI) {
  // ---- 模型发起的工具调用 ----
  pi.on("tool_call", async (event, ctx) => {
    report("tool_call", { toolName: event.toolName, input: event.input });
    const allowed = await decide(
      ctx,
      `允许执行工具 ${event.toolName}？`,
      JSON.stringify(event.input),
    );
    if (!allowed) {
      report("tool_call.denied", { toolName: event.toolName });
      return { block: true, reason: "LimboWave 权限网关拒绝" };
    }
    return undefined;
  });

  // ---- 用户/RPC 的 shell 命令（独立通道，不走 tool_call）----
  pi.on("user_bash", async (event, ctx) => {
    report("user_bash", { command: event.command, excludeFromContext: event.excludeFromContext });
    const allowed = await decide(ctx, "允许执行该 shell 命令？", event.command);
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
  pi.on("before_provider_request", (event, _ctx) => {
    report("provider.request", { payload: event.payload });
    return undefined; // 观测不改写；改写由应用显式控制时另行实现
  });
  pi.on("after_provider_response", (event, _ctx) => {
    report("provider.response", { status: event.status, headers: event.headers });
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
