import { type ChatEvent } from "./dto";
import { adaptEvent } from "./adapter";
import { ApiError } from "./client";
// Incremental SSE parser: UTF-8, CRLF, multi-line data, comments and bounded frames.
export async function readEvents(
  response: Response,
  onEvent: (event: ChatEvent) => void,
  signal: AbortSignal,
) {
  if (!response.ok)
    throw new ApiError(response.status, "stream_failed", "事件连接不可用");
  if (
    !response.headers.get("content-type")?.includes("text/event-stream") ||
    !response.body
  )
    throw new Error("事件流格式不正确");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "",
    data: string[] = [],
    size = 0;
  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      if (buffer.length > 1024 * 1024) throw new Error("事件帧过大");
      let index: number;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).replace(/\r$/, "");
        buffer = buffer.slice(index + 1);
        if (!line) {
          if (data.length) onEvent(adaptEvent(JSON.parse(data.join("\n"))));
          data = [];
          size = 0;
        } else if (line.startsWith("data:")) {
          const value = line.slice(5).replace(/^ /, "");
          size += value.length;
          if (size > 1024 * 1024) throw new Error("事件帧过大");
          data.push(value);
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
export async function connectEvents(
  epoch: string,
  seq: number,
  signal: AbortSignal,
  onEvent: (event: ChatEvent) => void,
) {
  const response = await fetch("/api/v1/events", {
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    signal,
    headers: {
      Accept: "text/event-stream",
      "Last-Event-ID": `${epoch}:${seq}`,
    },
  });
  await readEvents(response, onEvent, signal);
}
