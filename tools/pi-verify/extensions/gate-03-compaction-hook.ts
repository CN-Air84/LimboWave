/**
 * P0-GATE-03 探针扩展：接管压缩。
 *
 * 模式由环境变量控制（探针启动 Pi 前注入）：
 * - GATE03_MODE=observe：只记录，不接管（用于确认钩子被触发与 preparation 内容）。
 * - GATE03_MODE=cancel：返回 {cancel:true}，取消压缩。
 * - GATE03_MODE=custom：返回自定义 {compaction:{summary, firstKeptEntryId, tokensBefore}}。
 *
 * 每次触发都把 event.reason 与 preparation 概要写入 GATE03_LOG（JSONL）。
 */

import { appendFileSync } from "node:fs";

export default function (pi) {
  const logPath = process.env.GATE03_LOG;
  const mode = process.env.GATE03_MODE || "observe";

  pi.on("session_before_compact", (event, ctx) => {
    const prep = event.preparation || {};
    if (logPath) {
      appendFileSync(
        logPath,
        JSON.stringify({
          reason: event.reason,
          tokensBefore: prep.tokensBefore,
          firstKeptEntryId: prep.firstKeptEntryId ?? null,
          messagesToSummarize: Array.isArray(prep.messagesToSummarize)
            ? prep.messagesToSummarize.length
            : null,
          turnPrefixMessages: Array.isArray(prep.turnPrefixMessages)
            ? prep.turnPrefixMessages.length
            : null,
          hasPreviousSummary: !!prep.previousSummary,
          mode,
        }) + "\n",
        "utf8",
      );
    }

    if (mode === "cancel") {
      return { cancel: true };
    }
    if (mode === "custom") {
      return {
        compaction: {
          summary: "CUSTOM-SUMMARY-MARKER：这是应用注入的自定义压缩摘要。",
          firstKeptEntryId: prep.firstKeptEntryId,
          tokensBefore: prep.tokensBefore,
        },
      };
    }
    return undefined;
  });
}
