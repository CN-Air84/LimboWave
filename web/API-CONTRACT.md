# Public API boundary

All paths are under `/api/v1`, same origin. Internal app types are defined in `src/api/dto.ts`; the **wire** contract below is implemented by `src/api/adapter.ts`.

| Endpoint | Wire data |
|---|---|
| `GET pair/status` | `{status:"unpaired", csrf_token}` and bootstrap HttpOnly cookie; `{status:"pending", phrase}`; or `{status:"paired", device_id, csrf_token}` and auth cookie |
| `POST pair` | `{ticket}` or `{code}` (8 digits); bootstrap CSRF header required |
| `GET session` | `{server_epoch, device_id, csrf_token, password_required:bool, transport_secure:bool, permissions:{chat,tools}}`; 401 → pairing bootstrap |
| `GET password/challenge` | Paired devices only: `{challenge_id,public_key:PEM RSA,expires_in:120}`; one use per challenge |
| `POST password/verify` | CSRF required; `{challenge_id,encrypted_key,iv,ciphertext}` (standard Base64 except UTF-8 challenge ID), success `{ok:true}`; password failure 401, five failures revoke pairing |
| `GET state` | `{server_epoch, revision, seq, available, busy, conversation_id, branch_id, run_id, stream}`. `stream` is nullable, otherwise `{conversation_id,branch_id,run_id,message_id,text,thinking,status,tools?:Tool[],segments?:[{content,thinking}]}` |
| `GET models` | `[{id,name}]` (not a page wrapper) |
| `GET conversations?limit=50&cursor=…` | `{items:[{id,title,created_at,branches:[{id,title}]}],next_cursor}`; also accepts explicit `conversation_id/branch_id/updated_at/revision` |
| `GET conversations/{id}/messages?branch_id=…&limit=50&cursor=…` | `{items:[{id,role,content,thinking,status,run_id,tools?:Tool[],created_at,branch_id}],next_cursor}`; IDs scoped by requested conversation, branch-ancestor rows retained |
| `POST commands` | Common `{type,server_epoch,client_command_id,expected_revision}`. Revision is **global state revision**, not a made-up conversation revision. `new_session`: no extra fields; `send`: conversation_id/branch_id/text/model_id; `abort`: run_id; `select_model`: model_id only |
| `GET commands/{id}` | `{server_epoch,client_command_id,status,revision,run_id,conversation_id,branch_id,error}`. status pending/accepted/rejected/failed; failed is a definitive rejection in UI |
| `POST logout` | CSRF + cookie; `{ok:true}` |
| `GET events` | `Last-Event-ID: epoch:seq`. JSON envelope protocol_version/server_epoch/seq/kind/conversation_id/branch_id/run_id/message_id/payload. Sparse snapshot/resync frames accepted |

Runtime event names are normalized at the adapter: `assistant_delta`, `thinking_delta`, `assistant_start`, `assistant_end`, `user`, `tool`, `settled`, `run_failed`, `run_interrupted`, `branched`. Unknown names advance the cursor without rendering untrusted payloads. Malformed data/gaps trigger a fresh authoritative snapshot.

## Remaining explicit contract choices

- When no explicit `branch_id` is returned, UI selects the **last server-supplied branch** (no invented IDs). Backend should return `branch_id` for its preferred/current branch to remove ordering ambiguity; full branch selection is P1. History within a chosen branch includes ancestors as returned by the server.
- Current model ID is not returned by state/session. UI initially selects the first configured logical model; it sends that explicit ID with each send. Add a public selected_model_id if preserving desktop model selection on reload is required.
- Receipt 404 after a transport failure keeps the command unresolved; no automatic resend. Backend receipts must remain queryable throughout the valid session as specified in the plan.

## Pagination alignment confirmed

The implemented history endpoint returns an explicit `branch_id` with each conversation, and each message page is ordered oldest → newest; `next_cursor` selects older messages. The adapter prefers the explicit branch ID. History projects persisted `status`, `run_id`, and `tools`. `complete` maps to completed, `partial` maps to aborted (已停止生成), and `failed` remains failed; missing/unknown history status fails DTO validation rather than silently displaying success. Both history and state.stream restore `tools` through a default-empty array of whitelisted metadata. `Tool` is exactly `{tool_id,name,status:"running"|"completed"|"failed"}`; args/result/error (and any other server fields) are stripped. Snapshot tool metadata is authoritative and replaces stale incremental states, including an empty array. Cold reload and stream reconnect no longer depend on previous-page tool memory.

## Mandatory password gate

`password_required` means this paired device still needs verification. Pair cookies only preauthorize; state/models/events/conversations/history/commands return 403 until verified. Both HTTP and HTTPS require password verification. After `{ok:true}`, fetch session again; do not infer verification solely from the POST response. A wrong-password 401 is distinct from lost pairing: refresh session to determine whether the device remains paired.

RSA-OAEP SHA256 with MGF1 SHA256 wraps a random 32-byte AES key. AES-GCM uses a random 12-byte IV, UTF-8 password plaintext and UTF-8 `challenge_id` AAD. Ciphertext includes the trailing 16-byte authentication tag. Fresh challenge each attempt; no extra request ID or reusable verifier. Forge randomness must use crypto.getRandomValues and fail closed if absent.

HTTP is not protected by this envelope: chat remains plaintext and an active attacker can replace the webpage/public key. The frontend displays this warning and requires explicit acknowledgement; trusted HTTPS is recommended. Passwords never enter storage, logs or controller state and the input clears immediately on submit.
