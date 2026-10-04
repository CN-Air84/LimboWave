"""数据库迁移系统。

设计约束（设计计划 Task 1.1）：

- 迁移是**有序且不可变**的：每条迁移只声明"从哪个版本到哪个版本"，一旦发布不再修改。
- 迁移在**单个事务**内执行：失败则整体回滚，不留半截 schema。
- 版本号存在 ``schema_version`` 表里，不依赖文件名或时间戳。
- 迁移列表在代码里，不在磁盘上——避免"迁移文件丢失导致状态不可知"。

**加密边界（ADR-0002）**：敏感字段（消息正文、标题）以密文存储；
非敏感元数据（id、时间戳、角色、状态）明文。因此 DDL 里正文字段用 TEXT 存 base64 密文。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

# 当前 schema 版本。新增迁移时递增。
CURRENT_VERSION = 10


@dataclass(frozen=True, slots=True)
class Migration:
    """一条迁移：把库从 ``from_version`` 升到 ``from_version + 1``。"""

    from_version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _migration_001(conn: sqlite3.Connection) -> None:
    """初始 schema：会话、分支、消息、运行、双快照、Runtime 镜像。

    字段命名与领域模型的对应见各表注释。**敏感字段存密文**（ADR-0002）：
    带 ``_enc`` 后缀的列保存 base64(nonce+ciphertext)。
    """
    conn.executescript(
        """
        -- 会话。title_enc 为密文。
        CREATE TABLE conversations (
            id                       TEXT PRIMARY KEY,
            title_enc                TEXT NOT NULL,
            created_at               TEXT NOT NULL,
            default_logical_model_id TEXT
        );

        -- 分支。Phase 1B 每会话一条主线，但结构支持分叉。
        CREATE TABLE branches (
            id                      TEXT PRIMARY KEY,
            conversation_id         TEXT NOT NULL
                                    REFERENCES conversations(id) ON DELETE CASCADE,
            created_at              TEXT NOT NULL,
            parent_branch_id        TEXT REFERENCES branches(id) ON DELETE SET NULL,
            forked_from_message_id  TEXT
        );
        CREATE INDEX idx_branches_conversation ON branches(conversation_id);

        -- 用户可见消息。content_enc 与 thinking_enc 为密文。
        -- pi_entry_id 只作溯源，**不是主键**（Phase 1B 约束）。
        CREATE TABLE messages (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL
                            REFERENCES conversations(id) ON DELETE CASCADE,
            branch_id       TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content_enc     TEXT NOT NULL,
            thinking_enc    TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL
                            CHECK (status IN ('complete', 'partial', 'failed')),
            created_at      TEXT NOT NULL,
            run_id          TEXT,
            pi_entry_id     TEXT
        );
        CREATE INDEX idx_messages_branch ON messages(branch_id, created_at);
        CREATE INDEX idx_messages_run ON messages(run_id);

        -- 运行记录。error_enc 为密文（错误文本可能含密钥片段）。
        CREATE TABLE runs (
            id                    TEXT PRIMARY KEY,
            conversation_id       TEXT NOT NULL
                                  REFERENCES conversations(id) ON DELETE CASCADE,
            branch_id             TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
            status                TEXT NOT NULL
                                  CHECK (status IN ('running', 'completed', 'aborted',
                                                    'failed', 'interrupted')),
            created_at            TEXT NOT NULL,
            finished_at           TEXT,
            user_message_id       TEXT NOT NULL REFERENCES messages(id),
            assistant_message_id  TEXT REFERENCES messages(id),
            stop_reason           TEXT,
            error_enc             TEXT
        );
        CREATE INDEX idx_runs_conversation ON runs(conversation_id, created_at);

        -- 请求意图快照（应用准备发送什么）。每轮一条。
        CREATE TABLE request_intents (
            id                TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            conversation_id   TEXT NOT NULL,
            branch_id         TEXT NOT NULL,
            logical_model_id  TEXT NOT NULL,
            endpoint_id       TEXT NOT NULL,
            routing_reason    TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            prompt_layers     TEXT NOT NULL DEFAULT '{}',
            message_ids       TEXT NOT NULL DEFAULT '[]',
            attachment_ids    TEXT NOT NULL DEFAULT '[]',
            app_params        TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX idx_intents_run ON request_intents(run_id);

        -- 传输快照（provider 实际收到什么）。一轮可有多条（工具调用往返）。
        -- body_enc / headers_enc 为密文：请求体与头都可能含敏感内容。
        CREATE TABLE transport_snapshots (
            id                TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            created_at        TEXT NOT NULL,
            sequence          INTEGER NOT NULL,
            attempt           INTEGER NOT NULL DEFAULT 1,
            provider          TEXT,
            model_id          TEXT,
            url               TEXT,
            headers_enc       TEXT NOT NULL DEFAULT '{}',
            body_enc          TEXT NOT NULL DEFAULT '{}',
            param_diff        TEXT NOT NULL DEFAULT '{}',
            response_status   INTEGER,
            stop_reason       TEXT,
            error_class       TEXT
        );
        CREATE UNIQUE INDEX idx_transport_run_seq ON transport_snapshots(run_id, sequence);

        -- Runtime 条目镜像。**有自己的主键**，entry_id 只作溯源。
        -- payload_enc 为密文（Pi 条目可能含工具参数中的凭据）。
        CREATE TABLE runtime_mirrors (
            id                   TEXT PRIMARY KEY,
            conversation_id      TEXT NOT NULL
                                 REFERENCES conversations(id) ON DELETE CASCADE,
            entry_id             TEXT NOT NULL,
            entry_type           TEXT NOT NULL,
            captured_at          TEXT NOT NULL,
            run_id               TEXT REFERENCES runs(id) ON DELETE SET NULL,
            parent_entry_id      TEXT,
            message_id           TEXT REFERENCES messages(id) ON DELETE SET NULL,
            runtime_instance_id  TEXT,
            payload_enc          TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX idx_mirrors_conversation_entry
            ON runtime_mirrors(conversation_id, entry_id);
        CREATE INDEX idx_mirrors_run ON runtime_mirrors(run_id);
        """
    )


def _migration_002(conn: sqlite3.Connection) -> None:
    """Phase 4：文件文档登记卡 + 图片附件登记卡。

    内容本体都不在表里——剪贴板文档与图片的字节在加密 blob 仓，
    这里只有元数据与 blob 引用。display_name_enc 为密文（用户可命名的内容）。
    """
    conn.executescript(
        """
        -- 文件文档登记卡（TXT/MD/剪贴板）。行偏移与内容哈希支撑精确读取与变化检测。
        CREATE TABLE file_documents (
            id               TEXT PRIMARY KEY,
            kind             TEXT NOT NULL CHECK (kind IN ('text', 'markdown', 'clipboard')),
            display_name_enc TEXT NOT NULL,
            path             TEXT,
            blob_id          TEXT,
            encoding         TEXT NOT NULL DEFAULT 'utf-8',
            line_count       INTEGER NOT NULL DEFAULT 0,
            size_bytes       INTEGER NOT NULL DEFAULT 0,
            content_hash     TEXT NOT NULL DEFAULT '',
            mtime_ns         INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL
        );

        -- 图片附件登记卡。原图在加密 blob 仓，这里只有元数据。
        CREATE TABLE image_attachments (
            id            TEXT PRIMARY KEY,
            format        TEXT NOT NULL CHECK (format IN ('jpeg', 'png')),
            blob_id       TEXT NOT NULL,
            width         INTEGER NOT NULL DEFAULT 0,
            height        INTEGER NOT NULL DEFAULT 0,
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            content_hash  TEXT NOT NULL DEFAULT '',
            source_path   TEXT,
            created_at    TEXT NOT NULL
        );
        """
    )


def _migration_003(conn: sqlite3.Connection) -> None:
    """Phase 5：压缩版本表 + 消息白名单标记。

    压缩版本是「拟发送上下文」的一层，原始消息永久保留——版本表只存
    摘要与元数据，不碰消息本体。摘要与错误文本加密（可能含会话内容）。
    """
    conn.executescript(
        """
        -- 消息白名单标记（§7.3）：压缩时保留原文
        ALTER TABLE messages ADD COLUMN is_whitelisted INTEGER NOT NULL DEFAULT 0;

        -- 压缩版本。同一分支同一时刻只有一个启用版本（部分唯一索引保证）。
        CREATE TABLE compression_versions (
            id                     TEXT PRIMARY KEY,
            conversation_id        TEXT NOT NULL
                                   REFERENCES conversations(id) ON DELETE CASCADE,
            branch_id              TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
            created_at             TEXT NOT NULL,
            status                 TEXT NOT NULL
                                   CHECK (status IN ('draft', 'previewed', 'accepted',
                                                     'rejected', 'failed')),
            input_message_ids      TEXT NOT NULL DEFAULT '[]',
            tokens_before          INTEGER NOT NULL DEFAULT 0,
            tokens_after           INTEGER NOT NULL DEFAULT 0,
            compression_model_id   TEXT NOT NULL DEFAULT '',
            compression_endpoint_id TEXT NOT NULL DEFAULT '',
            prompt_version         INTEGER NOT NULL DEFAULT 1,
            generated_summary_enc  TEXT NOT NULL DEFAULT '',
            edited_summary_enc     TEXT,
            whitelist_message_ids  TEXT NOT NULL DEFAULT '[]',
            error_enc              TEXT,
            is_active              INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_compression_branch ON compression_versions(branch_id, created_at);
        -- 同一分支至多一个启用版本
        CREATE UNIQUE INDEX idx_compression_one_active
            ON compression_versions(branch_id) WHERE is_active = 1;
        """
    )


def _migration_004(conn: sqlite3.Connection) -> None:
    """Phase 6：会话级权限授权 + 权限审计。

    **授权边界是敏感信息**（允许读哪些目录等于泄露目录结构）→ 加密存储。
    审计记录保留决策链：工具、参数、匹配规则、结果、是否用户确认、实际范围。
    """
    conn.executescript(
        """
        -- 会话级持久授权（§11.1）。资源边界加密。
        CREATE TABLE permission_grants (
            id                TEXT PRIMARY KEY,
            conversation_id   TEXT NOT NULL
                              REFERENCES conversations(id) ON DELETE CASCADE,
            capability        TEXT NOT NULL
                              CHECK (capability IN ('file_read', 'file_write',
                                                    'terminal', 'network')),
            created_at        TEXT NOT NULL,
            allowed_paths_enc     TEXT NOT NULL DEFAULT '',
            allowed_domains_enc   TEXT NOT NULL DEFAULT '',
            allowed_cwd_enc       TEXT NOT NULL DEFAULT '',
            command_classes_enc   TEXT NOT NULL DEFAULT '',
            note_enc              TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_grants_conversation ON permission_grants(conversation_id);

        -- 权限审计（§11.3）。每次决策一条。
        CREATE TABLE permission_audit (
            id                TEXT PRIMARY KEY,
            conversation_id   TEXT NOT NULL
                              REFERENCES conversations(id) ON DELETE CASCADE,
            created_at        TEXT NOT NULL,
            tool_name         TEXT NOT NULL,
            capability        TEXT NOT NULL,
            params_enc        TEXT NOT NULL DEFAULT '',
            matched_rule      TEXT NOT NULL DEFAULT '',
            decision          TEXT NOT NULL
                              CHECK (decision IN ('allow', 'confirm', 'deny')),
            risk              TEXT NOT NULL DEFAULT 'normal',
            user_confirmed    INTEGER NOT NULL DEFAULT 0,
            actual_paths_enc  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_audit_conversation ON permission_audit(conversation_id, created_at);
        """
    )


def _migration_005(conn: sqlite3.Connection) -> None:
    """Phase 9：工具步骤审计（随消息落库，加密）。

    设计计划 §三.2 要求「隐藏工具步骤只影响展示，不删除审计数据」——
    因此工具步骤必须持久化，不能只存在于界面内存里。
    """
    conn.executescript(
        """
        -- 工具步骤列表（JSON 密文）。空数组表示这条消息没有工具调用。
        ALTER TABLE messages ADD COLUMN tool_steps_enc TEXT NOT NULL DEFAULT '[]';
        """
    )


def _migration_006(conn: sqlite3.Connection) -> None:
    """§十三.1：传输快照补「原始响应」与「流式事件」。

    两者都可能含工具参数里的凭据或模型回显的敏感串，因此与 body/headers 同样
    按密文列存（加密边界内），脱敏在应用侧落库前完成。
    """
    conn.executescript(
        """
        -- 解析后的响应对象（content 块 / usage / stopReason / errorMessage）
        ALTER TABLE transport_snapshots ADD COLUMN response_body_enc TEXT NOT NULL DEFAULT '{}';
        -- 流式事件磁带（紧凑序列 + 计数 + 截断量）
        ALTER TABLE transport_snapshots ADD COLUMN stream_tape_enc TEXT NOT NULL DEFAULT '{}';
        """
    )


def _migration_007(conn: sqlite3.Connection) -> None:
    """输入区权限档位按会话持久化；旧会话安全回落为「仅聊天」。"""
    conn.execute(
        "ALTER TABLE conversations ADD COLUMN permission_preset "
        "TEXT NOT NULL DEFAULT 'chat_only'"
    )


def _migration_008(conn: sqlite3.Connection) -> None:
    """分支支持独立命名；标题与会话标题一样按敏感文本加密。"""
    conn.execute("ALTER TABLE branches ADD COLUMN title_enc TEXT")


def _migration_009(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE memory_documents (
            id TEXT PRIMARY KEY,
            payload BLOB NOT NULL,
            conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
            branch_id TEXT REFERENCES branches(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX memory_scope ON memory_documents(conversation_id, branch_id)")


def _migration_010(conn: sqlite3.Connection) -> None:
    """手动重试的展示关联；旧记录不猜测来源，保持原样。"""
    conn.execute("ALTER TABLE runs ADD COLUMN retry_of_message_id TEXT")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        from_version=0,
        description="初始 schema：会话/分支/消息/运行/双快照/Runtime 镜像",
        apply=_migration_001,
    ),
    Migration(
        from_version=1,
        description="Phase 4：文件文档与图片附件登记卡",
        apply=_migration_002,
    ),
    Migration(
        from_version=2,
        description="Phase 5：压缩版本表 + 消息白名单标记",
        apply=_migration_003,
    ),
    Migration(
        from_version=3,
        description="Phase 6：权限授权与权限审计",
        apply=_migration_004,
    ),
    Migration(
        from_version=4,
        description="Phase 9：工具步骤审计随消息落库",
        apply=_migration_005,
    ),
    Migration(
        from_version=5,
        description="§十三.1：传输快照补原始响应与流式事件",
        apply=_migration_006,
    ),
    Migration(
        from_version=6,
        description="输入区权限档位按会话持久化",
        apply=_migration_007,
    ),
    Migration(
        from_version=7,
        description="分支支持独立加密标题",
        apply=_migration_008,
    ),
    Migration(from_version=8, description="分层记忆与分支快照", apply=_migration_009),
    Migration(from_version=9, description="手动重试来源关联", apply=_migration_010),
)


class MigrationError(Exception):
    """迁移失败。"""


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema 版本。空库返回 0。"""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def migrate(conn: sqlite3.Connection, *, target: int = CURRENT_VERSION) -> int:
    """把库升到 ``target`` 版本。返回最终版本。

    每条迁移在**独立事务**内执行：任一失败即回滚该条，库停在上一个一致版本。
    """
    version = current_version(conn)
    if version > target:
        raise MigrationError(f"库版本 {version} 高于目标 {target}，拒绝降级")

    for migration in sorted(MIGRATIONS, key=lambda m: m.from_version):
        if migration.from_version < version:
            continue
        if migration.from_version >= target:
            break
        try:
            with conn:
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                    (migration.from_version + 1,),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"迁移 {migration.from_version}→{migration.from_version + 1} "
                f"({migration.description}) 失败：{exc}"
            ) from exc
        version = migration.from_version + 1
    return version
