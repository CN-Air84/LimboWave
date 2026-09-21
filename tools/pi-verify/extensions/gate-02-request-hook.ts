/**
 * P0-GATE-02 探针扩展：观察并可改写最终 provider 请求。
 *
 * 行为由环境变量控制（探针在启动 Pi 前注入）：
 * - GATE02_LOG：若非空，把每次 before_provider_request 的完整 payload 追加写入该文件（JSONL）。
 * - GATE02_REWRITE：若为 "1"，把 max_completion_tokens 改写为 2048（用于验证改写真正生效——
 *   mock 侧若收到 2048 而非默认 4096，说明改写成功）。
 *
 * 与官方 provider-payload.ts 同一模式：挂 before_provider_request，读 event.payload，
 * 返回一个新对象即替换请求。
 */

import { appendFileSync } from "node:fs";

export default function (pi) {
  const logPath = process.env.GATE02_LOG;
  const rewrite = process.env.GATE02_REWRITE === "1";

  pi.on("before_provider_request", (event, ctx) => {
    if (logPath) {
      appendFileSync(logPath, JSON.stringify(event.payload) + "\n", "utf8");
    }
    if (rewrite) {
      return { ...event.payload, max_completion_tokens: 2048 };
    }
    return undefined;
  });

  pi.on("after_provider_response", (event, ctx) => {
    if (logPath) {
      appendFileSync(
        logPath,
        "[after_provider_response] status=" + event.status + " headers=" + JSON.stringify(event.headers) + "\n",
        "utf8",
      );
    }
  });
}
