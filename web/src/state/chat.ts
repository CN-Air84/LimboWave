import { z } from "zod";
import {
  StateSchema,
  MessageSchema,
  RunSchema,
  ToolSchema,
  type ChatEvent,
  type Message,
  type Snapshot,
  type Target,
} from "../api/dto";
export type ChatState = Snapshot & { terminalRuns: string[]; resync: boolean };
export const emptyChat: ChatState = {
  server_epoch: "",
  seq: 0,
  revision: 0,
  active_run: null,
  messages: [],
  terminalRuns: [],
  resync: false,
};
export function fromSnapshot(snapshot: Snapshot): ChatState {
  return {
    ...snapshot,
    terminalRuns: [
      ...new Set(
        snapshot.messages
          .filter(
            (m) =>
              m.role === "assistant" &&
              m.status !== "streaming" &&
              m.run_id &&
              m.run_id !== snapshot.active_run?.run_id,
          )
          .map((m) => m.run_id!),
      ),
    ],
    resync: false,
  };
}
export function mergeMessages(history: Message[], live: Message[]): Message[] {
  const byId = new Map(history.map((m) => [m.message_id, m]));
  for (const message of live) {
    const persisted = byId.get(message.message_id);
    // A snapshot captured just before finalization can still say settled/complete.
    // A subsequently read persisted partial/failed result must not be downgraded.
    const preserveTerminal =
      persisted &&
      persisted.run_id === message.run_id &&
      persisted.conversation_id === message.conversation_id &&
      persisted.branch_id === message.branch_id &&
      message.status === "completed" &&
      ["aborted", "failed"].includes(persisted.status);
    byId.set(
      message.message_id,
      preserveTerminal ? { ...message, status: persisted.status } : message,
    );
  }
  return [...byId.values()];
}
export function inTarget(
  message: { conversation_id: string | null; branch_id: string | null },
  target: Target | null,
) {
  return (
    !!target &&
    message.conversation_id === target.conversation_id &&
    message.branch_id === target.branch_id
  );
}
export function applyEvent(state: ChatState, event: ChatEvent): ChatState {
  if (event.kind === "snapshot") {
    const snapshot = StateSchema.parse(event.payload);
    return snapshot.server_epoch === state.server_epoch &&
      snapshot.seq >= state.seq
      ? fromSnapshot(snapshot)
      : { ...state, resync: true };
  }
  if (
    event.server_epoch !== state.server_epoch ||
    event.kind === "resync_required"
  )
    return { ...state, resync: true };
  if (event.seq <= state.seq) return state;
  if (event.seq !== state.seq + 1) return { ...state, resync: true };
  const next = { ...state, seq: event.seq };
  if (event.kind === "conversations.changed" || event.kind === "noop")
    return next;
  if (!event.conversation_id || !event.branch_id)
    return { ...next, resync: true };
  if (event.kind === "run.updated") {
    const run = RunSchema.parse(event.payload);
    if (
      run.run_id !== event.run_id ||
      run.conversation_id !== event.conversation_id ||
      run.branch_id !== event.branch_id
    )
      return { ...next, resync: true };
    if (state.terminalRuns.includes(run.run_id)) return next;
    const terminal = run.status !== "running";
    return {
      ...next,
      active_run: terminal
        ? state.active_run?.run_id === run.run_id
          ? null
          : state.active_run
        : run,
      terminalRuns: terminal
        ? [...state.terminalRuns, run.run_id]
        : state.terminalRuns,
      messages: state.messages.map((m) =>
        m.role === "assistant" && m.run_id === run.run_id && terminal
          ? { ...m, status: run.status as Message["status"] }
          : m,
      ),
    };
  }
  if (!event.message_id || !event.run_id) return { ...next, resync: true };
  if (state.terminalRuns.includes(event.run_id)) return next;
  let message = state.messages.find((m) => m.message_id === event.message_id);
  if (
    message &&
    (message.conversation_id !== event.conversation_id ||
      message.branch_id !== event.branch_id ||
      message.run_id !== event.run_id)
  )
    return { ...next, resync: true };
  if (message && message.status !== "streaming") return next;
  message ??= {
    message_id: event.message_id,
    conversation_id: event.conversation_id,
    branch_id: event.branch_id,
    run_id: event.run_id,
    role: "assistant",
    content: "",
    thinking: "",
    tools: [],
    status: "streaming",
  };
  if (event.kind === "message.updated") {
    const updated = MessageSchema.parse(event.payload);
    if (
      updated.message_id !== event.message_id ||
      updated.run_id !== event.run_id ||
      updated.conversation_id !== event.conversation_id ||
      updated.branch_id !== event.branch_id
    )
      return { ...next, resync: true };
    message = updated;
  } else if (event.kind === "tool.updated") {
    const tool = ToolSchema.parse(event.payload);
    message = {
      ...message,
      tools: [...message.tools.filter((t) => t.tool_id !== tool.tool_id), tool],
    };
  } else {
    const { text } = z.object({ text: z.string() }).parse(event.payload);
    const key = event.kind === "thinking.delta" ? "thinking" : "content";
    message = { ...message, [key]: message[key] + text };
  }
  return { ...next, messages: mergeMessages(state.messages, [message]) };
}
