/**
 * P0-GATE-04 探针扩展：拦截所有 tool_call。
 *
 * 模式由环境变量控制（探针在启动 Pi 前注入）：
 * - GATE04_MODE=allow-all：一律放行（undefined），用于确认工具本身能执行。
 * - GATE04_MODE=block-all：一律阻断 {block:true, reason}，用于验证副作用被阻止。
 * - GATE04_MODE=no-response：永不 resolve（探针随后杀进程），用于验证"无响应即不执行"。
 *
 * 每次拦截都把 toolName 与参数写入 GATE04_LOG（JSONL），供探针核对。
 */

import { appendFileSync } from "node:fs";

export default function (pi) {
  const logPath = process.env.GATE04_LOG;
  const mode = process.env.GATE04_MODE || "block-all";

  function record(phase, event) {
    if (!logPath) return;
    appendFileSync(
      logPath,
      JSON.stringify({
        phase,
        toolName: event.toolName,
        input: event.input,
      }) + "\n",
      "utf8",
    );
  }

  pi.on("tool_call", (event, ctx) => {
    record("tool_call", event);
    if (mode === "allow-all") return undefined;
    if (mode === "no-response") {
      // 永不 resolve：模拟应用断连。探针稍后强杀进程。
      return new Promise(() => {});
    }
    return { block: true, reason: "GATE04 blocked by extension" };
  });

  pi.on("tool_result", (event, ctx) => {
    record("tool_result", event);
    return undefined;
  });
}
