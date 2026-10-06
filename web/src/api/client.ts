import { z } from "zod";
import {
  WireSession,
  WirePair,
  WireState,
  WireModels,
  WireConversation,
  WireReceipt,
  historySchema,
  wireCommand,
} from "./adapter";
import {
  PasswordChallengeSchema,
  type PasswordEnvelope,
  pageSchema,
  type Command,
  type Conversation,
  type Page,
  type Receipt,
} from "./dto";
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId = "",
    public retryable = false,
  ) {
    super(message);
  }
}
export class ApiClient {
  private csrf = "";
  constructor(private transport: typeof fetch = (...args) => fetch(...args)) {}
  clear() {
    this.csrf = "";
  }
  async request<T>(
    path: string,
    schema: z.ZodType<T>,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    if (body !== undefined && !this.csrf)
      throw new ApiError(403, "csrf_missing", "请重新初始化安全会话");
    const response = await this.transport(`/api/v1${path}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: signal ?? AbortSignal.timeout(15000),
      headers: {
        Accept: "application/json",
        ...(body === undefined
          ? {}
          : { "Content-Type": "application/json", "X-CSRF-Token": this.csrf }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = z
        .object({
          code: z.string(),
          message: z.string(),
          request_id: z.string(),
          retryable: z.boolean(),
        })
        .safeParse(await response.json().catch(() => null));
      throw new ApiError(
        response.status,
        data.success ? data.data.code : "http_error",
        data.success ? data.data.message : `请求失败 (${response.status})`,
        data.success ? data.data.request_id : "",
        data.success && data.data.retryable,
      );
    }
    return schema.parse(await response.json());
  }
  private sessionRequest?: Promise<import("./dto").Session>;
  session(): Promise<import("./dto").Session> {
    this.sessionRequest ??= this.loadSession().finally(() => {
      this.sessionRequest = undefined;
    });
    return this.sessionRequest;
  }
  private async loadSession(): Promise<import("./dto").Session> {
    try {
      const session = await this.request("/session", WireSession);
      this.csrf = session.csrf_token;
      return session;
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error;
      const bootstrap = await this.pairStatus();
      if (bootstrap.status === "unpaired")
        return { authenticated: false, csrf_token: bootstrap.csrf_token };
      throw error;
    }
  }
  pair(input: { ticket: string } | { code: string }) {
    return this.request("/pair", WirePair, input);
  }
  async pairStatus() {
    const result = await this.request("/pair/status", WirePair);
    if ("csrf_token" in result && result.csrf_token)
      this.csrf = result.csrf_token;
    return result;
  }
  passwordChallenge() {
    return this.request("/password/challenge", PasswordChallengeSchema);
  }
  verifyPassword(envelope: PasswordEnvelope) {
    return this.request(
      "/password/verify",
      z.object({ ok: z.literal(true) }),
      envelope,
    );
  }
  logout() {
    return this.request("/logout", z.unknown(), {});
  }
  state() {
    return this.request("/state", WireState);
  }
  models() {
    return this.request("/models", WireModels);
  }
  conversations(cursor?: string): Promise<Page<Conversation>> {
    return this.request(
      `/conversations?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      pageSchema(WireConversation),
    );
  }
  messages(id: string, branch: string, cursor?: string) {
    return this.request(
      `/conversations/${encodeURIComponent(id)}/messages?branch_id=${encodeURIComponent(branch)}&limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      historySchema(id, branch),
    );
  }
  command(command: Command, revision: number): Promise<Receipt> {
    return this.request(
      "/commands",
      WireReceipt,
      wireCommand(command, revision),
    );
  }
  receipt(id: string): Promise<Receipt> {
    return this.request(`/commands/${encodeURIComponent(id)}`, WireReceipt);
  }
}
// Called once, before any asynchronous operation or render. No storage, logs, or query params.
export function consumePairingFragment(
  location: Pick<Location, "hash" | "pathname" | "search">,
  history: Pick<History, "replaceState">,
): string | null {
  const fragment = location.hash.slice(1);
  if (location.hash)
    history.replaceState(null, "", location.pathname + location.search);
  return new URLSearchParams(fragment).get("ticket");
}
