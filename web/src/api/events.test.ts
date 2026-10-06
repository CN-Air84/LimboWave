import { it, expect } from "vitest";
import { readEvents } from "./events";
it("parses arbitrarily split UTF8, CRLF, heartbeat and multi-line data", async () => {
  const wire =
    ': heartbeat\r\ndata: {"kind":"assistant_delta",\r\ndata: "server_epoch":"e","seq":1,"payload":{"delta":"你好"}}\r\n\r\n';
  const bytes = new TextEncoder().encode(wire);
  const events: unknown[] = [];
  const body = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    },
  });
  await readEvents(
    new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    (e) => events.push(e),
    new AbortController().signal,
  );
  expect(events).toHaveLength(1);
  expect(events[0]).toMatchObject({
    seq: 1,
    kind: "message.delta",
    payload: { text: "你好" },
  });
});
