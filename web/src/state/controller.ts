import { secureCommandId } from "../api/random";
import { ApiClient, ApiError } from "../api/client";
import { connectEvents } from "../api/events";
import type {
  Command,
  Conversation,
  Message,
  Model,
  Receipt,
  Session,
  Target,
} from "../api/dto";
import {
  applyEvent,
  emptyChat,
  fromSnapshot,
  inTarget,
  mergeMessages,
  type ChatState,
} from "./chat";
type AuthSession = Extract<Session, { authenticated: true }>;
export type ViewState = {
  phase: "loading" | "pairing" | "password" | "ready";
  session: AuthSession | null;
  chat: ChatState;
  conversations: Conversation[];
  conversationCursor: string | null;
  models: Model[];
  target: Target | null;
  history: Message[];
  historyCursor: string | null;
  drafts: Record<string, string>;
  model: string;
  error: string;
  connection: "connecting" | "online" | "offline";
  busy: boolean;
  loadingHistory: boolean;
  pending: Command | null;
  phrase: string;
};
export const draftKey = (target: Target | null) =>
  target ? `${target.conversation_id}:${target.branch_id}` : "new";
export class ChatController {
  private value: ViewState = {
    phase: "loading",
    session: null,
    chat: emptyChat,
    conversations: [],
    conversationCursor: null,
    models: [],
    target: null,
    history: [],
    historyCursor: null,
    drafts: {},
    model: "",
    error: "",
    connection: "connecting",
    busy: false,
    loadingHistory: false,
    pending: null,
    phrase: "",
  };
  private listeners = new Set<() => void>();
  private stream?: AbortController;
  private retry?: ReturnType<typeof setTimeout>;
  private flush?: ReturnType<typeof setTimeout>;
  private pairTimer?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private selection = 0;
  private stopped = false;
  private retryCount = 0;
  constructor(
    public api = new ApiClient(),
    private events = connectEvents,
  ) {}
  getSnapshot = () => this.value;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit() {
    for (const listener of this.listeners) listener();
  }
  private patch(patch: Partial<ViewState>) {
    this.value = { ...this.value, ...patch };
    this.emit();
  }
  setDraft(text: string) {
    this.patch({
      drafts: { ...this.value.drafts, [draftKey(this.value.target)]: text },
    });
  }
  dismissError() {
    this.patch({ error: "" });
  }
  private cancelStream() {
    this.stream?.abort();
    clearTimeout(this.retry);
    clearTimeout(this.flush);
    this.flush = undefined;
  }
  stop() {
    this.stopped = true;
    this.generation++;
    this.selection++;
    this.cancelStream();
    clearTimeout(this.pairTimer);
  }
  private fail(error: unknown) {
    if (error instanceof ApiError && error.status === 401) {
      this.generation++;
      this.selection++;
      this.cancelStream();
      this.api.clear();
      this.patch({
        phase: "pairing",
        session: null,
        chat: emptyChat,
        history: [],
        conversations: [],
        models: [],
        target: null,
        pending: null,
        error: "连接授权已失效。请在电脑端重新配对；草稿仅保留在本页内存中。",
      });
    } else
      this.patch({
        error:
          error instanceof Error ? error.message : "连接出现问题，请稍后重试",
      });
  }
  async initialize(ticket?: string | null) {
    this.stopped = false;
    try {
      const session = await this.api.session();
      if (this.stopped) return;
      if (!session.authenticated) {
        this.patch({ phase: "pairing", connection: "offline" });
        if (ticket) await this.pair({ ticket });
      } else {
        if (session.password_required) this.passwordGate(session);
        else {
          this.patch({ session, phase: "loading" });
          await this.sync();
        }
      }
    } catch (error) {
      this.patch({ phase: "pairing" });
      this.fail(error);
    }
  }
  private passwordGate(session: AuthSession) {
    this.generation++;
    this.selection++;
    this.cancelStream();
    this.patch({
      phase: "password",
      session,
      connection: "offline",
      chat: emptyChat,
      history: [],
      conversations: [],
      models: [],
      target: null,
      pending: null,
      phrase: "",
    });
  }
  async verifyPassword(password: string, acceptHttpRisk = false) {
    if (this.value.phase !== "password" || this.value.busy || !password) return;
    if (!this.value.session?.transport_secure && !acceptHttpRisk) {
      this.patch({ error: "请先明确确认 HTTP 连接风险，或改用 HTTPS。" });
      return;
    }
    const generation = this.generation;
    this.patch({ busy: true, error: "" });
    try {
      // Keep the crypto bundle off pairing/startup; fetch it alongside the
      // challenge only after the user explicitly requests password verification.
      const [challenge, { encryptPassword }] = await Promise.all([
        this.api.passwordChallenge(),
        import("../api/password"),
      ]);
      if (this.stopped || generation !== this.generation) return;
      const envelope = encryptPassword(password, challenge);
      password = "";
      await this.api.verifyPassword(envelope);
      if (this.stopped || generation !== this.generation) return;
      await this.initialize();
    } catch (error) {
      if (!this.stopped && generation === this.generation) {
        if (error instanceof ApiError && error.status === 401) {
          // A wrong password is 401 too; only a revoked cookie returns to pairing.
          try {
            const session = await this.api.session();
            if (this.stopped || generation !== this.generation) return;
            if (session.authenticated && session.password_required) {
              this.passwordGate(session);
              this.patch({
                error: "资料库密码不正确或挑战已失效，请重新输入。",
              });
            } else this.fail(error);
          } catch {
            this.fail(error);
          }
        } else
          this.patch({
            error:
              "密码核验失败，请重新输入；若持续失败，请重新配对或改用 HTTPS。",
          });
      }
    } finally {
      password = "";
      this.patch({ busy: false });
    }
  }
  async pair(input: { ticket: string } | { code: string }) {
    this.stopped = false;
    this.patch({ busy: true, error: "" });
    try {
      await this.api.pairStatus();
      const result = await this.api.pair(input);
      if (result.status === "paired") await this.initialize();
      else if (result.status === "pending") {
        this.patch({ phrase: result.verification_phrase });
        this.pollPair();
      }
    } catch (error) {
      this.fail(error);
    } finally {
      this.patch({ busy: false });
    }
  }
  private pollPair() {
    clearTimeout(this.pairTimer);
    this.pairTimer = setTimeout(async () => {
      try {
        const result = await this.api.pairStatus();
        if (this.stopped) return;
        if (result.status === "paired") {
          this.patch({ phrase: "" });
          await this.initialize();
        } else this.pollPair();
      } catch (error) {
        this.patch({ phrase: "" });
        this.fail(error);
      }
    }, 2000);
  }
  async sync() {
    if (
      this.stopped ||
      this.value.phase === "password" ||
      this.value.phase === "pairing"
    )
      return;
    const generation = ++this.generation;
    const selection = ++this.selection;
    this.cancelStream();
    this.patch({ connection: "connecting" });
    try {
      const session = this.value.session ?? (await this.api.session());
      if (generation !== this.generation || this.stopped) return;
      if (!session.authenticated)
        throw new ApiError(401, "unauthorized", "请重新配对");
      if (session.password_required) {
        this.passwordGate(session);
        return;
      }
      const [snapshot, conversations, models] = await Promise.all([
        this.api.state(),
        this.api.conversations(),
        this.api.models(),
      ]);
      if (generation !== this.generation || this.stopped) return;
      if (snapshot.server_epoch !== session.server_epoch) {
        this.patch({ session: null });
        void this.sync();
        return;
      }
      const changed =
        !!this.value.chat.server_epoch &&
        snapshot.server_epoch !== this.value.chat.server_epoch;
      let target = this.value.target;
      const fresh = conversations.items.find((c) => inTarget(c, target));
      if (fresh) target = fresh;
      if (!target && snapshot.active_run)
        target =
          conversations.items.find((c) =>
            inTarget(c, { ...snapshot.active_run!, revision: 0 }),
          ) ?? null;
      const history = target
        ? await this.api.messages(target.conversation_id, target.branch_id)
        : { items: [], next_cursor: null };
      if (generation !== this.generation || this.stopped) return;
      if (selection !== this.selection) {
        void this.sync();
        return;
      }
      // Preserve known terminal status during in-flight finalization, never across epochs.
      const observed = changed
        ? []
        : [...this.value.history, ...this.value.chat.messages];
      const restoreTools = (message: Message): Message => {
        const previous = observed.find(
          (m) =>
            m.message_id === message.message_id &&
            m.conversation_id === message.conversation_id &&
            m.branch_id === message.branch_id,
        );
        if (!previous) return message;
        return {
          ...message,
          tools: message.tools,
          status:
            message.status !== "streaming" &&
            ["failed", "aborted"].includes(previous.status)
              ? previous.status
              : message.status,
        };
      };
      const restored = fromSnapshot(snapshot);
      restored.messages = restored.messages.map(restoreTools);
      this.patch({
        phase: "ready",
        session,
        chat: restored,
        conversations: conversations.items,
        conversationCursor: conversations.next_cursor,
        models: models.items,
        target,
        history: history.items.map(restoreTools),
        historyCursor: history.next_cursor,
        loadingHistory: false,
        model:
          fresh?.model_id ??
          (this.value.model || models.items[0]?.model_id || ""),
        pending: changed ? null : this.value.pending,
        ...(changed
          ? {
              error:
                "服务已重启。草稿已保留，旧命令不会重发；请核对历史后再操作。",
            }
          : {}),
      });
      this.openStream(generation);
    } catch (error) {
      if (generation !== this.generation || this.stopped) return;
      this.fail(error);
      if (this.value.session) this.reconnect();
    }
  }
  private reconnect() {
    this.patch({ connection: "offline" });
    clearTimeout(this.retry);
    this.retry = setTimeout(
      () => void this.sync(),
      Math.min(15000, 1000 * 2 ** Math.min(this.retryCount++, 4)),
    );
  }
  private openStream(generation: number) {
    const controller = new AbortController();
    this.stream = controller;
    this.patch({ connection: "online" });
    void this.events(
      this.value.chat.server_epoch,
      this.value.chat.seq,
      controller.signal,
      (event) => {
        if (generation !== this.generation || controller.signal.aborted) return;
        try {
          const chat = applyEvent(this.value.chat, event);
          if (chat.resync) {
            void this.sync();
            return;
          }
          this.retryCount = 0;
          this.value = { ...this.value, chat };
          if (!this.flush)
            this.flush = setTimeout(() => {
              this.flush = undefined;
              this.emit();
            }, 40);
          if (
            event.kind === "conversations.changed" ||
            (event.kind === "run.updated" && !chat.active_run)
          )
            void this.sync();
        } catch {
          void this.sync();
        }
      },
    )
      .then(() => {
        if (!controller.signal.aborted && generation === this.generation)
          this.reconnect();
      })
      .catch((error) => {
        if (!controller.signal.aborted && generation === this.generation) {
          this.fail(error);
          if (this.value.session) this.reconnect();
        }
      });
  }
  async select(target: Conversation) {
    if (this.value.phase !== "ready") return;
    const selection = ++this.selection;
    this.patch({
      target,
      history: [],
      historyCursor: null,
      model: target.model_id ?? this.value.models[0]?.model_id ?? "",
      loadingHistory: true,
    });
    try {
      const page = await this.api.messages(
        target.conversation_id,
        target.branch_id,
      );
      if (selection === this.selection)
        this.patch({ history: page.items, historyCursor: page.next_cursor });
    } catch (error) {
      if (selection === this.selection) this.fail(error);
    } finally {
      if (selection === this.selection) this.patch({ loadingHistory: false });
    }
  }
  async moreHistory() {
    if (this.value.phase !== "ready") return;
    const { target, historyCursor, loadingHistory } = this.value;
    if (!target || !historyCursor || loadingHistory) return;
    const selection = this.selection;
    this.patch({ loadingHistory: true });
    try {
      const page = await this.api.messages(
        target.conversation_id,
        target.branch_id,
        historyCursor,
      );
      if (selection === this.selection)
        this.patch({
          history: mergeMessages(page.items, this.value.history),
          historyCursor: page.next_cursor,
        });
    } catch (error) {
      this.fail(error);
    } finally {
      if (selection === this.selection) this.patch({ loadingHistory: false });
    }
  }
  async moreConversations() {
    if (this.value.phase !== "ready") return;
    const cursor = this.value.conversationCursor;
    if (!cursor) return;
    const generation = this.generation;
    try {
      const page = await this.api.conversations(cursor);
      if (generation === this.generation)
        this.patch({
          conversations: [
            ...new Map(
              [...this.value.conversations, ...page.items].map((c) => [
                c.conversation_id,
                c,
              ]),
            ).values(),
          ],
          conversationCursor: page.next_cursor,
        });
    } catch (error) {
      this.fail(error);
    }
  }
  private targetFields() {
    const t = this.value.target;
    if (!t) throw new Error("请先新建会话");
    return {
      conversation_id: t.conversation_id,
      branch_id: t.branch_id,
      expected_revision: t.revision,
    };
  }
  private base() {
    return {
      client_command_id: secureCommandId(),
      server_epoch: this.value.chat.server_epoch,
    };
  }
  async newSession() {
    if (!this.value.model) return;
    await this.execute({
      ...this.base(),
      kind: "new-session",
      model_id: this.value.model,
    });
  }
  async send() {
    const text = this.value.drafts[draftKey(this.value.target)] ?? "";
    if (!text.trim() || !this.value.target || !this.value.model) return;
    await this.execute({
      ...this.base(),
      ...this.targetFields(),
      kind: "send",
      text,
      model_id: this.value.model,
    });
  }
  async abort() {
    const run = this.value.chat.active_run;
    if (run)
      await this.execute({ ...this.base(), kind: "abort", run_id: run.run_id });
  }
  async selectModel(model: string) {
    if (!this.value.target) {
      this.patch({ model });
      return;
    }
    await this.execute({
      ...this.base(),
      ...this.targetFields(),
      kind: "select-model",
      model_id: model,
    });
  }
  private async accept(command: Command, receipt: Receipt) {
    if (
      !this.value.session ||
      command.server_epoch !== this.value.chat.server_epoch
    )
      return;
    if (receipt.client_command_id !== command.client_command_id)
      throw new Error("命令回执不匹配");
    if (
      command.kind === "send" &&
      receipt.target &&
      !inTarget(receipt.target, {
        ...command,
        revision: command.expected_revision,
      })
    )
      throw new Error("发送回执目标不匹配");
    if (receipt.status === "pending") {
      this.patch({
        pending: command,
        error: "命令仍在确认中。请查询结果，不要重复发送。",
      });
      return;
    }
    this.patch({ pending: null });
    if (receipt.status === "rejected") {
      this.patch({ error: receipt.message ?? "命令未接受，草稿已保留" });
      await this.sync();
      return;
    }
    if (
      command.kind === "new-session" &&
      receipt.target &&
      !this.value.target
    ) {
      const text = this.value.drafts.new;
      if (text)
        this.patch({
          drafts: {
            ...this.value.drafts,
            new: "",
            [draftKey(receipt.target)]: text,
          },
        });
    }
    if (command.kind === "send") {
      const key = draftKey({ ...command, revision: command.expected_revision });
      if (this.value.drafts[key] === command.text)
        this.patch({ drafts: { ...this.value.drafts, [key]: "" } });
    }
    if (
      receipt.target &&
      (command.kind === "new-session" ||
        inTarget(
          command.kind === "send"
            ? command
            : { conversation_id: null, branch_id: null },
          this.value.target,
        ))
    )
      this.patch({ target: receipt.target });
    if (command.kind === "select-model")
      this.patch({ model: command.model_id });
    if (command.kind === "abort")
      this.patch({
        chat: {
          ...this.value.chat,
          messages: this.value.chat.messages.map((message) =>
            message.role === "assistant" && message.run_id === command.run_id
              ? { ...message, status: "aborted" as const }
              : message,
          ),
        },
      });
    await this.sync();
  }
  private async execute(command: Command) {
    if (
      this.value.phase !== "ready" ||
      this.value.busy ||
      this.value.pending ||
      this.value.connection !== "online" ||
      !this.value.session?.capabilities.chat
    )
      return;
    if (this.value.chat.active_run && command.kind !== "abort") {
      this.patch({ error: "另一端正在生成。草稿已保留，请稍后再试。" });
      return;
    }
    this.patch({ busy: true, error: "", pending: command });
    try {
      await this.accept(
        command,
        await this.api.command(command, this.value.chat.revision),
      );
    } catch (error) {
      if (error instanceof ApiError && error.status < 500) {
        this.patch({ pending: null });
        this.fail(error);
        if (error.status === 403) this.patch({ session: null });
        if (error.status === 409 || error.status === 403) await this.sync();
      } else {
        // The POST may have succeeded. Read its receipt once, never replay it.
        try {
          const session = await this.api.session();
          if (
            !session.authenticated ||
            session.server_epoch !== command.server_epoch
          ) {
            await this.sync();
            return;
          }
          if (session.password_required) {
            this.passwordGate(session);
            return;
          }
          await this.accept(
            command,
            await this.api.receipt(command.client_command_id),
          );
        } catch (queryError) {
          this.fail(queryError);
          this.patch({
            error:
              "发送结果尚未确认，草稿已保留。请查询回执或重新连接；不会自动重发。",
          });
        }
      }
    } finally {
      this.patch({ busy: false });
    }
  }
  async checkPending() {
    if (this.value.phase !== "ready") return;
    const command = this.value.pending;
    if (!command || this.value.busy) return;
    this.patch({ busy: true });
    try {
      const session = await this.api.session();
      if (
        !session.authenticated ||
        session.server_epoch !== command.server_epoch
      ) {
        await this.sync();
        return;
      }
      await this.accept(
        command,
        await this.api.receipt(command.client_command_id),
      );
    } catch (error) {
      this.fail(error);
    } finally {
      this.patch({ busy: false });
    }
  }
  async logout() {
    try {
      await this.api.logout();
      this.stop();
      this.api.clear();
      this.patch({
        session: null,
        phase: "pairing",
        chat: emptyChat,
        drafts: {},
        history: [],
        conversations: [],
        models: [],
        target: null,
        pending: null,
        error: "",
        connection: "offline",
      });
    } catch (error) {
      this.fail(error);
    }
  }
}
