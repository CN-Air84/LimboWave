# 应用配置与密钥（Task 2.1/2.2 配置，Task 1.2 加密边界）

本站点管理 UI 落地前的配置方式。**应用是权威数据源；Pi 的 `models.json` 是运行时派生产物。**

## 一、配置位置

| 路径 | 内容 | 是否含密钥 |
| --- | --- | --- |
| `<data_root>/config.json` | 站点端点、逻辑模型、默认模型 | **否**（只有引用） |
| `<data_root>/vault.json` | 资料库：主密钥的**密文包装**（Argon2id + ChaCha20-Poly1305） | 只有密文 |
| `<data_root>/vault/secrets.json` | 密钥**密文**（用资料库主密钥逐值加密） | 密文 |
| `<data_root>/limbowave.db` | 会话/消息/运行/快照（敏感字段逐值加密） | 敏感字段为密文 |
| `<data_root>/runtime/pi-home/` | Pi 的隔离运行环境，含**派生**的 `models.json` | 只有 `$ENV` 引用 |

Windows 上 `<data_root>` = `%LOCALAPPDATA%\LimboWave`。
资料库根目录已由 `.gitignore` 排除，绝不入库。

> 自 Task 1.2 的信封加密落地后不再存在明文 `master.key`。旧格式会在首次解锁时自动迁移：
> 旧密文换钥重加密后删除旧主密钥文件。加密边界的设计与限制见
> [`docs/architecture/adr-0002-vault-encryption.md`](../architecture/adr-0002-vault-encryption.md)。

## 二、`config.json`

```json
{
  "endpoints": [
    {
      "id": "relay-a",
      "name": "中转站 A",
      "base_url": "https://relay-a.example.com/v1",
      "api": "openai-completions",
      "credential_ref": "relay-a-key",
      "headers": { "X-Tenant": "personal" },
      "compat": { "supportsDeveloperRole": false }
    }
  ],
  "actual_models": [
    {
      "endpoint_id": "relay-a",
      "model_id": "deepseek-chat",
      "name": "",
      "default_thinking_level": "high",
      "available_thinking_levels": ["low", "medium", "high"],
      "supports_thinking": true,
      "supports_tools": true
    }
  ],
  "models": [
    {
      "id": "deepseek-chat",
      "name": "DeepSeek Chat",
      "bindings": [{ "endpoint_id": "relay-a", "model_id": "deepseek-chat", "auto_matched": true }],
      "default_binding": 0,
      "context_window": 128000,
      "max_tokens": 8192,
      "supports_images": false
    }
  ],
  "default_model_id": "deepseek-chat"
}
```

### 端点（`endpoints`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 端点唯一标识，同时用作派生 `models.json` 里的 provider 键 |
| `name` | 是 | 显示名 |
| `base_url` | 是 | 必须是 `http(s)` URL；末尾斜杠会被归一 |
| `api` | 是 | `openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai` |
| `credential_ref` | 否 | **密钥引用名**，不是密钥本身。本地端点（Ollama 等）可省略 |
| `headers` | 否 | 默认请求头，原样透传给 Pi |
| `compat` | 否 | Pi 的兼容开关，原样透传（见下） |

### 实际模型目录（`actual_models`）

站点上真实可调用的远端模型：来自「站点端点 → 实际模型」页的清单导入、能力探测或手动输入。
**导入实际模型不会创建逻辑模型**。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `endpoint_id` / `model_id` | 是 | 站点 + 该站点上的远端模型 ID；二者组合唯一 |
| `name` | 否 | 清单给出的显示名（与 ID 相同时留空） |
| `default_thinking_level` 等能力字段 | 否 | 由探测写入；站点-模型级能力的**唯一来源** |

### 逻辑模型（`models`）

逻辑模型是**用户手动建立**的分类：ID 手输，或引用某个实际模型的 ID。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 应用侧稳定标识（用户认知中的模型） |
| `name` | 是 | 显示名 |
| `bindings` | 否 | 绑定的实际模型 `{endpoint_id, model_id, auto_matched}`；可以为空（先建后绑，路由会如实报错） |
| `default_binding` | 否 | 默认绑定的下标，缺省 `0` |
| `context_window` / `max_tokens` | 否 | 缺省由 Pi 使用自身默认值 |
| `supports_images` | 否 | 为真时派生 `input: ["text", "image"]` |

绑定规则：

- **一个实际模型只能绑定一个逻辑模型**；同一逻辑模型在一个站点至多一条绑定。
- 自动匹配只用三条确定规则（忽略首尾空白、大小写、末尾 `-free`），接近程度：
  完全相同 > 仅大小写/空白不同 > 归一后相同；同一站点并列最接近时不替用户挑。
- 触发时机：新建逻辑模型、首次导入实际模型、在逻辑模型页点「按 ID 自动匹配」。
- 自动匹配**从不改动手动配对**（`auto_matched: false`），只会用更接近的候选替换自动建立的绑定。
- 旧版配置（能力字段写在绑定上）加载时自动迁入实际模型目录；再次保存后绑定里只剩引用。

配置在加载时会被校验：端点/模型 id 不得重复、绑定必须指向存在的端点、
一个实际模型不得被两个逻辑模型绑定、`default_binding` 不得越界、`default_model_id` 必须存在。
校验失败会直接报错，不会静默降级。

## 三、密钥

密钥只存于加密库。**不用命令行参数传密钥**（会进 shell 历史与进程列表），改为标准输入。
`secret` 系列命令需要先解锁资料库（同样经 stdin 读主密码）：

```powershell
python -m limbowave vault init                # 首次：设置主密码（交互输入，二次确认）
python -m limbowave secret set relay-a-key    # 先问主密码，再问密钥，均不回显
python -m limbowave secret list
python -m limbowave secret delete relay-a-key
python -m limbowave vault change-password     # 改主密码（只重新封装主密钥，不重写数据）
```

非交互场景（脚本/CI）按行喂 stdin：`secret set` 依次是「主密码、密钥」，其余命令首行为主密码：

```powershell
"master-password`nsk-xxxx" | python -m limbowave secret set relay-a-key
```

密钥的流向：

```
vault.json（主密码包裹的主密钥）→ 解锁
  → vault/secrets.json（密文）按需解密
  → 启动 Pi 时经一次性环境变量 LIMBOWAVE_SECRET_<引用名大写> 注入子进程
  → 派生 models.json 里只写 "$LIMBOWAVE_SECRET_<引用名大写>"
  → Pi 在请求时把值放进 Authorization 头
```

因此密钥**不会**出现在：`config.json`、Pi 的 `auth.json`、派生的 `models.json`、日志或异常文本。

### 已知限制（ADR-0002 如实记录）

- 应用层字段加密**不是全库加密**：表名、列名、外键、时间戳等元数据是明文的。
- 解锁期间主密钥存在于进程内存，Python 无法可靠擦除；`lock()` 只丢弃引用。
- FTS5 无法索引密文——Phase 9 的全文搜索将基于解密后的内存数据构建。
- Windows Hello 恢复封装（设计计划 §12.3）尚未接入；`vault.json` 的 `wraps`
  已是列表结构，为第二份系统保护包装预留。

## 四、路由语义

- 逻辑模型 → 端点的选择是**确定**的：取 `default_binding` 指向的绑定。
- 每次路由都带 `reason`（可解释）。
- **不自动跨站点重发**：端点失败就如实暴露错误，是否切换由用户决定。

## 五、优雅降级

以下情形应用以"无内核"模式启动（输入区禁用并提示），不崩溃：

- **启动时取消了解锁**：进入「未解锁」模式——内存仓库、无内核，本次会话不持久化；
- 没有 `config.json` 或其中没有逻辑模型；
- 路由失败（模型不存在、绑定或端点缺失）；
- 端点引用了密钥库里不存在的凭据；
- node 或 Pi 未安装。

这个取舍是有意的：**配置问题不是运行时崩溃**。

## 六、`compat` 兼容开关

中转站的协议实现常与官方有细微差异。`compat` 会原样透传给 Pi 的 `models.json`，
常用项：

- `supportsDeveloperRole` — 服务端不认 `developer` 角色时置 `false`
- `supportsReasoningEffort` — 服务端不认 `reasoning_effort` 时置 `false`
- `maxTokensField` — `max_completion_tokens` 或 `max_tokens`
- `thinkingFormat` — `reasoning_effort` / `deepseek` / `zai` / `qwen` / `openrouter` / `together` / `chat-template` 等

完整清单见 Pi Runtime 合同 §五.5。

## 七、验证配置是否生效

```powershell
python -m limbowave            # 启动应用；状态栏显示 "就绪 · <模型 id>" 即路由成功
```

或直接看图一行的派生结果：

```powershell
Get-Content "$env:LOCALAPPDATA\LimboWave\runtime\pi-home\.pi\agent\models.json"
```

该文件每次启动都会重建，**可以安全删除**——它不是权威数据。
