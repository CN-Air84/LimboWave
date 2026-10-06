import { z } from "zod";
import { type Command, type Message, type ChatEvent, StateSchema } from "./dto";
// Wire DTOs from web/schemas.py, dto.py and RuntimeFacade. All unknown keys stripped.
// Snapshot/history tool DTO is deliberately narrower than incremental UI tool state.
const ToolSchema = z
  .object({
    tool_id: z.string(),
    name: z.string(),
    status: z.enum(["running", "completed", "failed"]),
  })
  .transform((tool) => ({ ...tool, summary: "" }));
const nullableId = z.string().nullable().default(null);
export const WireSession = z
  .object({
    server_epoch: z.string(),
    csrf_token: z.string(),
    device_id: z.string(),
    password_required: z.boolean(),
    transport_secure: z.boolean(),
    permissions: z.object({ chat: z.boolean(), tools: z.boolean() }),
  })
  .transform((s) => ({
    authenticated: true as const,
    server_epoch: s.server_epoch,
    csrf_token: s.csrf_token,
    device_name: s.device_id,
    password_required: s.password_required,
    transport_secure: s.transport_secure,
    capabilities: {
      chat: s.permissions.chat,
      remote_agent: s.permissions.tools,
    },
  }));
export const WirePair = z
  .discriminatedUnion("status", [
    z.object({ status: z.literal("unpaired"), csrf_token: z.string() }),
    z.object({
      status: z.literal("paired"),
      csrf_token: z.string().optional(),
    }),
    z.object({ status: z.literal("pending"), phrase: z.string() }),
  ])
  .transform((p) =>
    p.status === "pending"
      ? { status: p.status, verification_phrase: p.phrase }
      : p,
  );
export const WireModels = z
  .array(z.object({ id: z.string(), name: z.string() }))
  .transform((items) => ({
    items: items.map((m) => ({ model_id: m.id, label: m.name })),
  }));
export const WireConversation = z
  .object({
    id: z.string().optional(),
    conversation_id: z.string().optional(),
    branch_id: z.string().optional(),
    branches: z
      .array(z.object({ id: z.string(), title: z.string() }))
      .default([]),
    revision: z.number().int().optional(),
    title: z.string(),
    created_at: z.string().optional(),
    updated_at: z.string().optional(),
  })
  .transform((c) => ({
    conversation_id: c.conversation_id ?? c.id ?? "",
    branch_id: c.branch_id ?? c.branches.at(-1)?.id ?? "",
    revision: c.revision ?? 0,
    title: c.title,
    updated_at: c.updated_at ?? c.created_at ?? "",
    model_id: null,
  }))
  .refine((c) => !!c.conversation_id && !!c.branch_id, "会话或分支 ID 缺失");
const WireMessage = z.object({
  id: z.string().optional(),
  message_id: z.string().optional(),
  conversation_id: z.string().optional(),
  branch_id: z.string().optional(),
  run_id: nullableId,
  role: z.enum(["user", "assistant", "system", "tool"]),
  text: z.string().optional(),
  content: z.string().optional(),
  thinking: z.string().nullable().optional(),
  status: z.enum(["complete", "partial", "failed", "completed", "aborted"]),
  tools: ToolSchema.array().default([]),
});
export function historySchema(conversation_id: string, branch_id: string) {
  return z
    .object({ items: z.array(WireMessage), next_cursor: z.string().nullable() })
    .transform((page) => ({
      ...page,
      items: page.items.map((m): Message => ({
        message_id: m.message_id ?? m.id ?? "",
        conversation_id: m.conversation_id ?? conversation_id,
        branch_id: m.branch_id ?? branch_id,
        run_id: m.run_id,
        role: m.role === "tool" ? "system" : m.role,
        content: m.content ?? m.text ?? "",
        thinking: m.thinking ?? "",
        tools: m.tools,
        status:
          m.status === "failed"
            ? "failed"
            : ["aborted", "partial"].includes(m.status ?? "")
              ? "aborted"
              : "completed",
      })),
    }))
    .refine((page) => page.items.every((m) => !!m.message_id), "消息 ID 缺失");
}
const WireStream = z.object({
  conversation_id: nullableId,
  branch_id: nullableId,
  run_id: nullableId,
  message_id: nullableId,
  text: z.string().default(""),
  thinking: z.string().default(""),
  status: z.string().default("running"),
  tools: ToolSchema.array().default([]),
  segments: z
    .array(
      z.object({
        content: z.string(),
        thinking: z.string().nullable().default(""),
      }),
    )
    .default([]),
});
export const WireState = z
  .object({
    server_epoch: z.string(),
    revision: z.number().int().nonnegative(),
    seq: z.number().int().nonnegative(),
    available: z.boolean(),
    busy: z.boolean(),
    conversation_id: nullableId,
    branch_id: nullableId,
    run_id: nullableId,
    stream: WireStream.nullable(),
  })
  .transform((s) => {
    const stream = s.stream;
    const messages: Message[] =
      stream?.message_id && stream.conversation_id && stream.branch_id
        ? [
            {
              message_id: stream.message_id,
              conversation_id: stream.conversation_id,
              branch_id: stream.branch_id,
              run_id: stream.run_id,
              role: "assistant",
              content:
                stream.segments
                  .map((part) => part.content)
                  .filter(Boolean)
                  .map((text) => text + "\n\n")
                  .join("") + stream.text,
              thinking:
                stream.segments
                  .map((part) => part.thinking)
                  .filter(Boolean)
                  .map((text) => text + "\n\n")
                  .join("") + stream.thinking,
              tools: stream.tools,
              status: [
                "partial",
                "aborted",
                "interrupted",
                "run_interrupted",
              ].includes(stream.status)
                ? "aborted"
                : ["failed", "run_failed"].includes(stream.status)
                  ? "failed"
                  : s.busy
                    ? "streaming"
                    : "completed",
            },
          ]
        : [];
    return {
      server_epoch: s.server_epoch,
      revision: s.revision,
      seq: s.seq,
      messages,
      active_run:
        s.busy && s.run_id && s.conversation_id && s.branch_id
          ? {
              run_id: s.run_id,
              conversation_id: s.conversation_id,
              branch_id: s.branch_id,
              status: "running" as const,
            }
          : null,
    };
  });
export const WireReceipt = z
  .object({
    client_command_id: z.string(),
    server_epoch: z.string(),
    status: z.enum(["pending", "accepted", "rejected", "failed"]),
    conversation_id: nullableId,
    branch_id: nullableId,
    revision: z.number().int().nullable().optional(),
    run_id: nullableId,
    error: z.object({ code: z.string() }).nullable().optional(),
  })
  .transform((r) => ({
    client_command_id: r.client_command_id,
    status: r.status === "failed" ? ("rejected" as const) : r.status,
    run_id: r.run_id,
    ...(r.conversation_id && r.branch_id && r.revision != null
      ? {
          target: {
            conversation_id: r.conversation_id,
            branch_id: r.branch_id,
            revision: r.revision,
          },
        }
      : {}),
    message: r.error ? "命令未接受，草稿已保留。请刷新状态后重试。" : undefined,
  }));
export function wireCommand(command: Command, revision: number) {
  const base = {
    type: command.kind.replaceAll("-", "_"),
    client_command_id: command.client_command_id,
    server_epoch: command.server_epoch,
    expected_revision: revision,
  };
  switch (command.kind) {
    case "new-session":
      return base;
    case "abort":
      return { ...base, run_id: command.run_id };
    case "select-model":
      return { ...base, model_id: command.model_id };
    case "send":
      return {
        ...base,
        conversation_id: command.conversation_id,
        branch_id: command.branch_id,
        text: command.text,
        model_id: command.model_id,
      };
  }
}
const WireEvent = z.object({
  protocol_version: z.literal(1).default(1),
  server_epoch: z.string(),
  seq: z.number().int().nonnegative(),
  kind: z.string(),
  conversation_id: nullableId,
  branch_id: nullableId,
  run_id: nullableId,
  message_id: nullableId,
  payload: z.record(z.string(), z.unknown()).default({}),
});
export function adaptEvent(value: unknown): ChatEvent {
  const e = WireEvent.parse(value);
  const base = { ...e, kind: "conversations.changed" as ChatEvent["kind"] };
  if (e.kind === "snapshot")
    return {
      ...base,
      kind: "snapshot",
      payload: StateSchema.parse(WireState.parse(e.payload)),
    };
  if (e.kind === "resync_required" || e.kind === "runtime_unavailable")
    return { ...base, kind: "resync_required" };
  if (["assistant_delta", "thinking_delta"].includes(e.kind))
    return {
      ...base,
      kind: e.kind === "thinking_delta" ? "thinking.delta" : "message.delta",
      payload: {
        text: z.string().parse(e.payload.delta ?? e.payload.text ?? ""),
      },
    };
  if (["settled", "run_failed", "run_interrupted"].includes(e.kind))
    return {
      ...base,
      kind: "run.updated",
      payload: {
        run_id: e.run_id,
        conversation_id: e.conversation_id,
        branch_id: e.branch_id,
        status:
          e.kind === "settled"
            ? "completed"
            : e.kind === "run_failed"
              ? "failed"
              : "aborted",
      },
    };
  if (e.kind === "tool") {
    const p = z
      .object({
        name: z.string(),
        phase: z.string(),
        tool_call_id: z.string(),
        is_error: z.boolean().optional(),
      })
      .parse(e.payload);
    return {
      ...base,
      kind: "tool.updated",
      payload: {
        tool_id: p.tool_call_id,
        name: p.name,
        status: p.is_error
          ? "failed"
          : ["end", "completed", "finish"].includes(p.phase)
            ? "completed"
            : ["waiting", "approval", "pending"].includes(p.phase)
              ? "waiting"
              : "running",
        summary: "",
      },
    };
  }
  if (e.kind === "assistant_start" || e.kind === "assistant_end")
    return { ...base, kind: "resync_required" };
  if (e.kind === "user")
    return {
      ...base,
      kind: "message.updated",
      payload: {
        message_id: e.message_id,
        conversation_id: e.conversation_id,
        branch_id: e.branch_id,
        run_id: e.run_id,
        role: e.kind === "user" ? "user" : "assistant",
        content: z.string().parse(e.payload.text ?? ""),
        thinking: z.string().parse(e.payload.thinking ?? ""),
        tools: [],
        status: "completed",
      },
    };
  // Unknown protocol kinds advance the cursor without inventing a user-visible event.
  return {
    ...base,
    kind: ["branched", "conversations_changed", "model_changed"].includes(
      e.kind,
    )
      ? "conversations.changed"
      : "noop",
  };
}
