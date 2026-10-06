import { z } from "zod";
// Public wire contract only. Resolve backend naming changes here, never in components.
export const TargetSchema = z.object({
  conversation_id: z.string(),
  branch_id: z.string(),
  revision: z.number().int().nonnegative(),
});
export const ConversationSchema = TargetSchema.extend({
  title: z.string(),
  updated_at: z.string(),
  model_id: z.string().nullable(),
});
export const ToolSchema = z.object({
  tool_id: z.string(),
  name: z.string(),
  status: z.enum(["running", "waiting", "completed", "failed"]),
  summary: z.string().default(""),
});
export const MessageSchema = z.object({
  message_id: z.string(),
  conversation_id: z.string(),
  branch_id: z.string(),
  run_id: z.string().nullable(),
  role: z.enum(["user", "assistant", "system"]),
  content: z.string(),
  thinking: z.string().default(""),
  tools: z.array(ToolSchema).default([]),
  status: z.enum(["streaming", "completed", "aborted", "failed"]),
});
export const RunSchema = z.object({
  run_id: z.string(),
  conversation_id: z.string(),
  branch_id: z.string(),
  status: z.enum(["running", "completed", "aborted", "failed"]),
});
export const SessionSchema = z.discriminatedUnion("authenticated", [
  z.object({ authenticated: z.literal(false), csrf_token: z.string().min(1) }),
  z.object({
    authenticated: z.literal(true),
    csrf_token: z.string().min(1),
    server_epoch: z.string(),
    device_name: z.string(),
    password_required: z.boolean(),
    transport_secure: z.boolean(),
    capabilities: z.object({ chat: z.boolean(), remote_agent: z.boolean() }),
  }),
]);
export const StateSchema = z.object({
  server_epoch: z.string(),
  seq: z.number().int().nonnegative(),
  revision: z.number().int().nonnegative().default(0),
  active_run: RunSchema.nullable(),
  messages: z.array(MessageSchema),
});
export const PairSchema = z.discriminatedUnion("status", [
  z.object({ status: z.literal("paired") }),
  z.object({ status: z.literal("pending"), verification_phrase: z.string() }),
]);
export const ReceiptSchema = z.object({
  client_command_id: z.string(),
  status: z.enum(["pending", "accepted", "rejected"]),
  target: TargetSchema.optional(),
  run_id: z.string().nullable().optional(),
  message: z.string().optional(),
});
export const ModelSchema = z.object({
  model_id: z.string(),
  label: z.string(),
});
export const pageSchema = <T extends z.ZodType>(item: T) =>
  z.object({ items: z.array(item), next_cursor: z.string().nullable() });
export const EventSchema = z.object({
  protocol_version: z.literal(1),
  server_epoch: z.string(),
  seq: z.number().int().nonnegative(),
  kind: z.enum([
    "message.delta",
    "thinking.delta",
    "message.updated",
    "tool.updated",
    "run.updated",
    "conversations.changed",
    "resync_required",
    "snapshot",
    "noop",
  ]),
  conversation_id: z.string().nullable(),
  branch_id: z.string().nullable(),
  run_id: z.string().nullable(),
  message_id: z.string().nullable(),
  payload: z.unknown(),
});
export type Target = z.infer<typeof TargetSchema>;
export type Conversation = z.infer<typeof ConversationSchema>;
export type Message = z.infer<typeof MessageSchema>;
export type Snapshot = z.infer<typeof StateSchema>;
export type Session = z.infer<typeof SessionSchema>;
export type ChatEvent = z.infer<typeof EventSchema>;
export type Receipt = z.infer<typeof ReceiptSchema>;
export type Model = z.infer<typeof ModelSchema>;
export type Page<T> = { items: T[]; next_cursor: string | null };
type CommandBase = { client_command_id: string; server_epoch: string };
type CommandTarget = {
  conversation_id: string;
  branch_id: string;
  expected_revision: number;
};
export type Command = CommandBase &
  (
    | { kind: "new-session"; model_id: string }
    | (CommandTarget & { kind: "send"; text: string; model_id: string })
    | { kind: "abort"; run_id: string }
    | (CommandTarget & { kind: "select-model"; model_id: string })
  );

export const PasswordChallengeSchema = z.object({
  challenge_id: z.string().min(1),
  public_key: z.string().min(1),
  expires_in: z.literal(120),
});
export type PasswordChallenge = z.infer<typeof PasswordChallengeSchema>;
export type PasswordEnvelope = {
  challenge_id: string;
  encrypted_key: string;
  iv: string;
  ciphertext: string;
};
