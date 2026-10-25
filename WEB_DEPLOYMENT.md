# LimbowaveOnServer 部署手册

> 更新日期：2026-10-13。本文依据当前仓库的实际入口、配置、认证和备份实现编写。
> 主线为 **Ubuntu 24.04 x86_64 + Python 源码部署 + Nginx HTTPS + systemd**。
> 命令示例不代表已经完成真实 VPS、Android 或 iOS 验收；正式上线前必须执行文末检查。

> 发布版本说明（2026-10-13）：本手册发布在主分支根目录，但独立服务端实现尚在 `codex/limbowave-on-server` 分支。
> 本文对应源码提交 [`ea13d07`](https://github.com/CN-Air84/LimboWave/commit/ea13d071c7c02744536cbbdccca43527994776fa)。
> 部署独立服务端请检出该提交或已包含它的后续版本；本次发布不将服务端代码合入主分支。

## 目录

1. [选择部署方式](#1-选择部署方式)
2. [部署前准备](#2-部署前准备)
3. [安装系统依赖与运行时](#3-安装系统依赖与运行时)
4. [安装应用并构建网页](#4-安装应用并构建网页)
5. [初始化资料库与模型](#5-初始化资料库与模型)
6. [接入公网 HTTPS 域名](#6-接入公网-https-域名)
7. [配置后台服务](#7-配置后台服务)
8. [配置 Nginx 与证书](#8-配置-nginx-与证书)
9. [首次解锁与设备配对](#9-首次解锁与设备配对)
10. [日常维护](#10-日常维护)
11. [备份与恢复](#11-备份与恢复)
12. [升级与回退](#12-升级与回退)
13. [局域网访问简版](#13-局域网访问简版)
14. [常见问题](#14-常见问题)
15. [上线验收清单](#15-上线验收清单)

## 1. 选择部署方式

LimbowaveOnServer 提供浏览器界面，但不是只有静态文件的网站。网页通过同源 API 使用 Python 后端，
真实模型请求还需要 Node.js 和 Pi Runtime。

| 方式 | 适用情况 | 必须保持运行 | 认证与重启行为 |
| --- | --- | --- | --- |
| 桌面局域网 Web | 手机访问自己电脑上的资料库 | 桌面应用，并保持资料库解锁 | 配对后还要核验资料库密码；服务重启后重新配对 |
| Linux 独立服务端 | 不依赖桌面，浏览器访问自己的服务器 | Linux 后台服务 | SSH 手动解锁后配对；设备授权持久化，但重启后资料库仍需手动解锁 |

只想在家中手机使用，直接看[第 13 节](#13-局域网访问简版)。希望部署在 VPS 上，按第 2–12 节操作。

**当前边界：**

- 单用户资料库，不提供多人注册、租户隔离或多人协作权限体系。
- 首版只有一个共享模型执行通道，不是每个浏览器一套独立 Agent。
- 服务端不注册主机终端、文件读写等工具；允许的应用内工具包括写入会话记忆。
  网页没有远程批准主机工具的权限。
- 服务启动时锁定，不从文件、环境变量或网页自动读取主密码解锁。
- 不以离线写入、完整桌面功能对等或完整增量同步为部署前提；不要把附件同步视为已支持。
- 不要单独把 `web/dist` 上传到静态托管平台，也不要把 Vite 开发服务器当生产后端。

### 必须先知道的两个实现缺口

1. `server run` 目前没有公网域名参数，也不会读取本文定义的域名环境变量。
   默认配置只接受 `127.0.0.1:8765`。直接把现成 Nginx 模板接到默认命令上，公网域名请求会被
   `invalid_host` 拒绝，HTTPS Cookie 的传输标识也不会自动正确配置。
   **第 6–7 节用独立的部署入口显式注入现有 `ServerWebConfig`，不关闭安全校验。**
2. 桌面 `secret set` 使用桌面默认数据目录，没有服务端 `--data-dir` 参数。
   **第 5 节的辅助脚本显式把凭据写入服务器资料库**，避免写到另一个目录。

这两段辅助脚本是本手册提供的部署适配示例，不是仓库已经自带的一键安装器。
它们只调用现有 Python 接口；升级时应重新核对接口，并在上线前实测。

## 2. 部署前准备

### 2.1 环境要求

| 项目 | 本手册采用的配置 |
| --- | --- |
| 操作系统 | Ubuntu 24.04，glibc，x86_64；其他发行版、ARM、Alpine/musl 需另外验收 |
| Python | 3.12 或 3.13；本手册使用系统 `/usr/bin/python3.12` |
| Node.js | 至少 22.19.0；统一采用经过审查的 Node 24.x 补丁版本 |
| Pi | **固定 `@earendil-works/pi-coding-agent@0.86.0`**，不用 `latest` 或版本范围 |
| 资源起点 | 1 vCPU、1–2 GiB 内存、至少 10 GiB 可用磁盘；不是性能保证 |
| 存储 | 本地块存储，SQLite 数据库和 WAL 不放在 NFS/共享网络盘 |
| 网络 | 域名、有效 HTTPS 证书、SSH 管理通道、访问所选模型端点的出站能力 |

小内存机器建议在单独构建机生成前端/发布产物；构建需求可能大于实际运行需求。

### 2.2 约定路径

```text
/opt/limbowave/app/                 应用源码及 .venv-linux
/opt/limbowave/bin/limbowave         本手册创建的命令包装器
/opt/limbowave/node/                Node.js
/opt/limbowave/pi/                  固定版本 Pi
/opt/limbowave/server_entry.py      HTTPS 服务启动适配入口
/opt/limbowave/set_server_secret.py 凭据录入辅助脚本
/etc/limbowave/server.env           公网域名配置，不放任何密码
/var/lib/limbowave/                 资料库、数据库、凭据和运行时数据
/var/backups/limbowave/             加密备份暂存位置
```

应用和运行时最终归 `root` 所有，服务账号只写 `/var/lib/limbowave`。
资料库中加密的是敏感字段，不是整个 SQLite 文件；目录权限和备份保护仍然必要。

### 2.3 设置部署参数

以下 Linux 命令使用 Bash，在有 `sudo` 权限的普通管理员账号下执行。
先替换三个占位值；`DOMAIN` 只写域名，不带协议、路径或尾部斜杠。
新 SSH 会话需要重新设置变量。不要把密码或 API Key 放进这些变量。

```bash
set -euo pipefail
DOMAIN='limbowave.example.com'       # 改成自己的域名
REPO_URL='<你的 LimboWave 仓库地址>'
DEPLOY_REF='ea13d071c7c02744536cbbdccca43527994776fa' # 本手册对应的服务端实现；升级前重新审核
```

让域名 A 记录指向 VPS。如果配置 AAAA，IPv6 也必须能够到达同一站点。
只按需放行实际 SSH 端口、80 和 443，**不要开放 8765**。
本手册的证书签发/续期采用 HTTP-01，需保持公网 80 可达；改用 DNS-01 时另行设计续期流程。
修改 SSH 或防火墙规则前，保留一条可用的 SSH 会话，避免锁死管理入口。

## 3. 安装系统依赖与运行时

### 3.1 系统软件与服务账号

下面的账号创建命令用于首次安装。已有同名账号时，先核对其身份，不要重复创建。

```bash
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl xz-utils git \
  python3.12 python3.12-venv \
  nginx certbot python3-certbot-nginx

sudo useradd --system --user-group \
  --home-dir /var/lib/limbowave --shell /usr/sbin/nologin limbowave
sudo install -d -m 0755 /opt/limbowave
sudo install -d -m 0700 -o limbowave -g limbowave /var/lib/limbowave
```

按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 为管理员安装 uv。
先审查安装来源/脚本，确保以下检查成功；运行服务不需要 uv。

```bash
uv --version
/usr/bin/python3.12 --version
```

本手册后面显式使用系统 Python，避免虚拟环境依赖管理员家目录中的 Python，
被 `ProtectHome=true` 拦截。不要通过关闭 `ProtectHome` 来解决这个问题。

### 3.2 Node.js 与固定版本 Pi

应用包不包含 Node/Pi。以下示例解析官方 Node 24.x 分支提供的 Linux x64 归档并核对 SHA256。
部署前审查所解析版本的官方发布和安全信息；记录实际版本/归档/校验值，后续重建使用记录值，
不要把这个动态分支地址当作永久锁定版本。其他架构不能套用 `linux-x64`。

```bash
NODE_WORKDIR=$(mktemp -d)
cd "$NODE_WORKDIR"
curl -fsSLO https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt
NODE_ARCHIVE=$(awk '$2 ~ /^node-v24[.][0-9]+[.][0-9]+-linux-x64[.]tar[.]xz$/ {print $2}' SHASUMS256.txt)
test -n "$NODE_ARCHIVE"
NODE_VERSION=$(printf '%s' "$NODE_ARCHIVE" | sed 's/^node-//; s/-linux-x64.tar.xz$//')
curl -fsSLO "https://nodejs.org/dist/$NODE_VERSION/$NODE_ARCHIVE"
grep "  $NODE_ARCHIVE$" SHASUMS256.txt | sha256sum --check --strict -

sudo install -d -m 0755 /opt/limbowave/node /opt/limbowave/pi
sudo tar -xJf "$NODE_ARCHIVE" -C /opt/limbowave/node --strip-components=1
sudo env PATH=/opt/limbowave/node/bin:/usr/bin:/bin \
  /opt/limbowave/node/bin/npm install --global --prefix /opt/limbowave/pi \
  --ignore-scripts @earendil-works/pi-coding-agent@0.86.0
cd -
```

以服务账号、与后台服务相同的 HOME/PATH 检查，不能只验证管理员的交互终端：

```bash
sudo -u limbowave env -i HOME=/var/lib/limbowave \
  PATH=/opt/limbowave/node/bin:/opt/limbowave/pi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  node -e "const [m,n]=process.versions.node.split('.').map(Number); if(m<22||(m===22&&n<19))process.exit(1); require('node:assert').strictEqual(require('/opt/limbowave/pi/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version,'0.86.0')"
sudo -u limbowave env -i HOME=/var/lib/limbowave \
  PATH=/opt/limbowave/node/bin:/opt/limbowave/pi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  pi --version
```

不能依赖 nvm 或管理员家目录里的 Pi；服务账号没有登录 Shell，也不应修改运行时安装目录。

## 4. 安装应用并构建网页

### 4.1 获取审核过的源码

```bash
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" /opt/limbowave/app
git clone "$REPO_URL" /opt/limbowave/app
cd /opt/limbowave/app
git checkout --detach "$DEPLOY_REF"
git rev-parse HEAD
```

记录完整提交 SHA。不要把开发机的 `.venv`、运行时数据、主密码、API Key 或 `.env` 一起上传。
下列构建在普通管理员身份下执行；不要以服务账号下载依赖。

### 4.2 先构建前端，再安装 Python 项目

```bash
export PATH=/opt/limbowave/node/bin:/opt/limbowave/pi/bin:$PATH
cd /opt/limbowave/app/web
npm ci
npm run typecheck
npm run build -- --emptyOutDir

test -s /opt/limbowave/app/src/limbowave/web/static/index.html
find /opt/limbowave/app/src/limbowave/web/static/assets -name '*.js' -print

cd /opt/limbowave/app
UV_PROJECT_ENVIRONMENT=/opt/limbowave/app/.venv-linux \
  uv sync --locked --no-dev --python /usr/bin/python3.12 --no-python-downloads
```

- 默认生产输出是 `/opt/limbowave/app/src/limbowave/web/static`，Python 后端直接提供这些文件。
- `--emptyOutDir` 只清理 Vite 配置指向的生产静态目录，避免旧哈希资产残留。
  不要并行运行前端构建、打包或正在提供这些文件的生产服务。
- 构建 `--outDir dist` 是前端测试用法，不会把资产放到源码部署所需的位置。
- `uv.lock` 和 `web/package-lock.json` 都必须保留；安装失败时检查锁文件，别临时删除或升级依赖绕过。
- 安装项目之前先生成静态文件，因为 wheel 配置也显式包含该目录。

### 4.3 创建统一管理命令

项目没有自动安装名为 `limbowave` 的 Python console script。
下面显式创建包装器，供原有 systemd 模板和管理命令使用：

```bash
sudo install -d -m 0755 /opt/limbowave/bin
sudo tee /opt/limbowave/bin/limbowave >/dev/null <<'SH'
#!/bin/sh
exec /opt/limbowave/app/.venv-linux/bin/python -m limbowave "$@"
SH
sudo chmod 0755 /opt/limbowave/bin/limbowave

sudo -u limbowave /opt/limbowave/bin/limbowave server --help
```

本手册只部署后端/网页，不运行桌面 GUI。当前 Python 依赖清单仍包含 PySide6，
不表示服务器需要显示器或图形登录会话。

**已有 Linux 打包产物时：** 可参考[打包说明](docs/development/packaging.md)，目录式产物必须连同 `_internal`
一起分发，不能只复制单个可执行文件；Windows `.exe` 不能当 Linux 产物使用。
本文第 6 节的适配入口依赖源码虚拟环境，不能不加调整地换成冻结包路径。

## 5. 初始化资料库与模型

### 5.1 创建服务器资料库

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server init --data-dir /var/lib/limbowave
```

交互输入主密码；它用于保护服务器资料库，请单独妥善保存。
命令拒绝覆盖已有 `vault.json`。不要把密码写在命令行、脚本、环境文件或 Shell 历史里。

### 5.2 配置端点与逻辑模型

编辑 `/var/lib/limbowave/config.json`，最小示例如下。
把 `REPLACE_WITH_PROVIDER_BASE_URL` 替换为所选服务商的兼容 API 地址，
把两处 `REPLACE_WITH_PROVIDER_MODEL_ID` 替换为同一个真实模型 ID。
本示例假设对方支持 `openai-completions`，其他协议按[配置说明](docs/development/configuration.md)调整。

```json
{
  "endpoints": [
    {
      "id": "server-main",
      "name": "服务器模型端点",
      "base_url": "https://REPLACE_WITH_PROVIDER_BASE_URL/v1",
      "api": "openai-completions",
      "credential_ref": "server_api_key",
      "rpm": 5
    }
  ],
  "actual_models": [
    {
      "endpoint_id": "server-main",
      "model_id": "REPLACE_WITH_PROVIDER_MODEL_ID",
      "supports_thinking": false,
      "supports_tools": false
    }
  ],
  "models": [
    {
      "id": "web-chat",
      "name": "网页聊天",
      "bindings": [
        {
          "endpoint_id": "server-main",
          "model_id": "REPLACE_WITH_PROVIDER_MODEL_ID"
        }
      ],
      "supports_images": false
    }
  ],
  "default_model_id": "web-chat"
}
```

这里只登记文本聊天；按模型真实能力配置思考、工具和上下文参数，不要猜测能力。
保存后检查权限与 JSON 语法：

```bash
sudo chown limbowave:limbowave /var/lib/limbowave/config.json
sudo chmod 0600 /var/lib/limbowave/config.json
sudo -u limbowave /opt/limbowave/app/.venv-linux/bin/python \
  -m json.tool /var/lib/limbowave/config.json >/dev/null
```

配置文件仅保存密钥引用，**不放 API Key 本体**。Pi 的 `models.json` 是运行时派生结果，
不能把它当作权威配置手工维护。

### 5.3 交互录入模型 API Key

为避免桌面 CLI 的默认目录与服务端目录不一致，安装这个辅助脚本：

```bash
sudo tee /opt/limbowave/set_server_secret.py >/dev/null <<'PY'
import argparse
import getpass
from pathlib import Path

from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import Vault


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ref")
    args = parser.parse_args()
    root = Path("/var/lib/limbowave")
    vault = Vault(root / "vault.json")
    key = vault.unlock(getpass.getpass("服务器资料库主密码："))
    try:
        secret = getpass.getpass("模型 API Key：")
        if not secret or secret != getpass.getpass("再次输入 API Key："):
            raise SystemExit("密钥为空或两次输入不一致，未保存")
        directory = root / "vault"
        directory.mkdir(mode=0o700, exist_ok=True)
        SecretStore(key, directory / "secrets.json").set(args.ref, secret)
        print("已保存加密凭据；未显示密钥")
    finally:
        vault.lock()


if __name__ == "__main__":
    main()
PY
sudo chmod 0644 /opt/limbowave/set_server_secret.py
sudo -u limbowave /opt/limbowave/app/.venv-linux/bin/python \
  /opt/limbowave/set_server_secret.py server_api_key
```

在可交互的 SSH 终端输入密码和 Key，不通过管道喂入明文。
引用名必须与 `credential_ref` 一致；服务已启动时，修改配置或凭据后重新锁定/解锁以重建运行时。

## 6. 接入公网 HTTPS 域名

原有默认 `server run` 只使用 loopback 地址作为合法 Host/Origin。
以下适配入口将 HTTPS 公网 origin 交给已经实现的 `ServerWebConfig`，同时仍只监听本机：

```bash
sudo tee /opt/limbowave/server_entry.py >/dev/null <<'PY'
import asyncio
import os
from pathlib import Path

from limbowave.bootstrap import AppPaths
from limbowave.server.lifecycle import ServerLifecycle
from limbowave.server.web_config import ServerWebConfig


def main():
    root = Path("/var/lib/limbowave")
    config = ServerWebConfig(
        public_origin=os.environ["LIMBOWAVE_PUBLIC_ORIGIN"],
        bind_host="127.0.0.1",
        bind_port=8765,
    ).web_config()
    lifecycle = ServerLifecycle(
        AppPaths(root, root / "logs"),
        web_config=config,
    )
    asyncio.run(lifecycle.run_forever())


if __name__ == "__main__":
    main()
PY

sudo install -d -m 0755 /etc/limbowave
printf 'LIMBOWAVE_PUBLIC_ORIGIN=https://%s\n' "$DOMAIN" \
  | sudo tee /etc/limbowave/server.env >/dev/null
sudo chmod 0600 /etc/limbowave/server.env
sudo chown -R root:root /opt/limbowave
sudo chmod -R a+rX,go-w /opt/limbowave
```

注意：

- `LIMBOWAVE_PUBLIC_ORIGIN` 是**上述适配入口读取**的变量，不是原有 CLI 已支持的设置。
- 公网 origin 必须是唯一、规范的 `https://域名`，不能有路径、尾部斜杠、查询参数或用户名密码。
- 不部署到 `/chat/` 子路径；当前前端和 API 使用根路径。
- Python 入口保留 `if __name__ == "__main__"`，避免后台存储子进程导入入口时重复启动服务。
- 不绑定 `0.0.0.0`，不把后端端口暴露公网，不通过放宽 Host/Origin/Cookie 校验绕过部署错误。

## 7. 配置后台服务

复制仓库的服务模板，保留原有独立账号、运行时 PATH 和文件系统保护：

```bash
sudo cp /opt/limbowave/app/deploy/limbowave.service /etc/systemd/system/limbowave.service
sudo install -d -m 0755 /etc/systemd/system/limbowave.service.d
sudo tee /etc/systemd/system/limbowave.service.d/web.conf >/dev/null <<'INI'
[Service]
EnvironmentFile=/etc/limbowave/server.env
UMask=0077
ExecStart=
ExecStart=/opt/limbowave/app/.venv-linux/bin/python /opt/limbowave/server_entry.py
INI

sudo systemctl daemon-reload
sudo systemctl enable --now limbowave
sudo systemctl status limbowave --no-pager
sudo journalctl -u limbowave -n 80 --no-pager
```

这个 drop-in 替换模板里的启动命令，但保留 Node/Pi 的启动前检查，以及
`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=strict`、`ProtectHome=true` 和数据目录写入范围。

检查状态与监听：

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server status --data-dir /var/lib/limbowave
sudo ss -ltnp | grep ':8765'
curl --fail --silent --show-error \
  -H "Host: $DOMAIN" http://127.0.0.1:8765/healthz
```

预期为 `locked`，监听 `127.0.0.1:8765`，健康检查返回 `{"ok":true,"locked":true}`。
公网 origin 配置后，本地探测也要使用正确 Host；无该请求头的 `curl` 返回 `invalid_host` 是预期保护。
**健康检查通过仅表示服务存活，不代表模型可用或资料库已解锁。**

## 8. 配置 Nginx 与证书

### 8.1 先配置 HTTP 站点，申请证书

证书不存在时，不要先启用引用证书文件的 HTTPS 模板，否则 `nginx -t` 会失败。
下面使用 Ubuntu 的 `sites-available/sites-enabled` 布局；适用于新站点，已有同名配置时先备份并核对。

```bash
sudo tee /etc/nginx/sites-available/limbowave >/dev/null <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location / { return 404; }
}
EOF
sudo ln -s /etc/nginx/sites-available/limbowave /etc/nginx/sites-enabled/limbowave
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --nginx -d "$DOMAIN"
```

按提示填写证书管理邮箱并完成签发。若失败，先检查 DNS、IPv6 和公网 80 可达性。
不要换成浏览器忽略证书错误。

### 8.2 启用项目 HTTPS 代理模板

```bash
sed "s/limbowave\.example\.com/$DOMAIN/g" \
  /opt/limbowave/app/deploy/limbowave-nginx.conf \
  | sudo tee /etc/nginx/sites-available/limbowave >/dev/null
sudoedit /etc/nginx/sites-available/limbowave
```

在编辑器中核对：

1. `server_name`、证书目录和 `server.env` 中的域名完全一致。
2. 保留 `proxy_pass http://127.0.0.1:8765`，所有页面/API 都走同一个应用。
3. 保留模板里的 `Host $host`、`X-Forwarded-Host $host` 和 `X-Forwarded-Proto https`。
   代理应覆盖这些头，不能照搬浏览器提供的转发值。
4. 在 HTTPS `server` 内补充 `proxy_set_header Forwarded "";`，去除外部传入的标准转发头；
   后端明确拒绝带 `Forwarded` 的请求。
5. 保留 `client_max_body_size 64k`、`proxy_buffering off` 和 `proxy_read_timeout 1h`。
   后者是两次读取之间的空闲超时，不是“每次生成可以运行一小时”的业务保证。
6. 模板的 HSTS 含 `includeSubDomains; preload`。只有全部相关子域都长期支持 HTTPS，且明确选择
   preload 策略时才保留；普通首次部署可改成 `max-age=31536000`，不要误影响其他子域。
7. 不加 API 缓存、HTML/JS 跨站注入或会改变流式响应的代理功能。
   前置 CDN/其他代理时，需要另外验证同源认证、SSE 和安全头，本文不直接认证该组合。

完成后：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail --silent --show-error "https://$DOMAIN/healthz"
curl --head "https://$DOMAIN/"
sudo certbot renew --dry-run
```

浏览器应没有证书警告，网页应加载成功。不要用 `curl -k` 掩盖 TLS 配置问题。
核对实际证书续期定时任务，以及续期成功后 Nginx 重载流程；仅签发一次证书不等于续期已经配置好。

## 9. 首次解锁与设备配对

### 9.1 SSH 交互解锁

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server unlock --data-dir /var/lib/limbowave
sudo -u limbowave /opt/limbowave/bin/limbowave server status --data-dir /var/lib/limbowave
```

交互输入主密码，预期输出 `ok`、随后 `unlocked`。
控制命令通过 `/var/lib/limbowave/server.sock` 联系运行中的服务，必须使用同一账号和同一数据目录。
网页不能替代这个解锁步骤。

### 9.2 生成配对邀请

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server pair --data-dir /var/lib/limbowave
```

输出包含短期票据、备用码及 `url`。使用形如 `https://自己的域名/#ticket=...` 的 `url`
在可信浏览器上完成配对；配对窗口约两分钟，过期后重新生成。
不要把邀请发到群聊、公开截图或诊断报告。票据放在 URL fragment，不改成查询参数。

当前服务端 CLI 没有“批准备用码请求”的命令，**本文采用 ticket URL 配对**；
不要把桌面端的短语核对/批准步骤原样套到 headless 服务端。

**认证差异：** 当前独立服务端没有安装桌面 LAN 的 `PasswordGate`，配对完成后不再让浏览器
二次输入资料库主密码。服务器通过 SSH 手动解锁，配对邀请本身就是重要访问凭据。
不要声称这条路径提供了“网页配对后再输主密码”的双重认证，也不要为了配对把主密码发给浏览器。
配对设备可访问该资料库中的会话，只授权自己信任的设备。

### 9.3 完成真实模型验收

在浏览器新建或选择会话，选择已配置的逻辑模型，发送一条短提示，例如“只回复：部署测试成功”。
确认收到**真实模型回复**，且内容逐步到达；刷新后历史仍然存在。
`pi --version`、构建成功、`/healthz`、解锁成功或模拟内核测试都不能替代这个步骤。

同时检查浏览器的会话 Cookie 包含 `Secure`、`HttpOnly` 和 `SameSite=Strict`，
断线/刷新不会自动重复发送旧消息，其他浏览器没有配对时不能读取会话。
不要把 Cookie 或 API Key 复制到公开日志里来测试。

## 10. 日常维护

常用命令都在 SSH 中执行：

```bash
# 查询状态 / 主动锁定
sudo -u limbowave /opt/limbowave/bin/limbowave server status --data-dir /var/lib/limbowave
sudo -u limbowave /opt/limbowave/bin/limbowave server lock --data-dir /var/lib/limbowave

# 查看授权设备 / 撤销某个设备（替换设备 ID）
sudo -u limbowave /opt/limbowave/bin/limbowave server devices --data-dir /var/lib/limbowave
sudo -u limbowave /opt/limbowave/bin/limbowave server revoke '<device_id>' --data-dir /var/lib/limbowave

# 查看日志 / 重启 / 停止
sudo journalctl -u limbowave -n 100 --no-pager
sudo systemctl restart limbowave
sudo systemctl stop limbowave

# 检查磁盘容量
df -h /var/lib/limbowave
sudo du -sh /var/lib/limbowave
```

- 进程、机器或服务重启后，资料库重新锁定，需要再次运行 `server unlock`；不要配置自动解锁。
- 服务端设备授权保存于 SQLite，默认绝对有效期为 30 天，上限 8 个授权设备；锁定/重启不会
  自动永久撤销它们。Cookie 有效且授权未过期时，解锁后可重连；失效时重新配对。
  这与桌面 LAN 的“本次服务有效”不同。
- 设备丢失、共享或不再可信时主动撤销；仅关闭网页不等于撤销授权。
- 多设备共享执行通道，忙碌/版本冲突时先刷新状态；不自动重发含糊失败的发送命令。
- 默认 API 请求体上限 64 KiB，活动请求上限 32、SSE 上限 8、每分钟请求预算 240。
  这些是服务端资源保护，不是模型服务商的 RPM 配额。
- 全量同步默认最多 10,000 实体，存储字段预算和编码响应上限均为 8 MiB；超限返回
  `413 snapshot_too_large`，并发快照返回 `503 snapshot_busy`。历史分页不等于全量同步分页。
- 变更日志保留最近 50,000 序号窗口；旧游标可能收到 `409 resync_required`。
  清理日志不会保证 SQLite 文件马上缩小，仍要监控磁盘。
- 不把密码、API Key、配对链接、Cookie 或完整敏感对话粘贴到工单和公开日志。

## 11. 备份与恢复

### 11.1 创建加密备份

使用现有备份脚本；不要直接复制正在运行的 SQLite 主文件或 WAL。
建议避开生成中的会话，先主动锁定服务，获得稳定的维护窗口：

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server lock --data-dir /var/lib/limbowave
sudo install -d -m 0700 -o limbowave -g limbowave /var/backups/limbowave
BACKUP_FILE="/var/backups/limbowave/limbowave-$(date +%Y%m%d-%H%M%S).limbowave-backup"
sudo -u limbowave /opt/limbowave/app/.venv-linux/bin/python \
  /opt/limbowave/app/scripts/server_backup.py \
  --data-dir /var/lib/limbowave --output "$BACKUP_FILE"
```

已经处于 `locked` 时无需重复执行 `server lock`。备份脚本交互读取资料库主密码和
**独立备份密码**，后者还需要确认一次；它不会自动解锁后台服务。
备份包含数据库、配置、加密凭据、附件 blob 和可移植恢复密钥包装。

将加密归档复制到服务器之外，并把备份密码单独保管。单独保存在同一 VPS 不算完整灾备。
独立备份密码能够恢复资料库密钥，本身就是敏感凭据；忘记资料库密码和备份密码没有恢复绕过。

不要直接把这个交互脚本塞进无终端的定时任务；自动备份需要另外设计经过审查的秘密管理方式。
维护结束后按第 9 节手动解锁。

### 11.2 先恢复到空的临时目录

下面替换实际备份路径，恢复目标必须是新建的空目录。确认目标位置与容量后执行：

```bash
BACKUP_FILE='/var/backups/limbowave/<实际备份文件>.limbowave-backup'
RESTORE_DIR="/var/lib/limbowave-restore-$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0700 -o limbowave -g limbowave "$RESTORE_DIR"
sudo -u limbowave /opt/limbowave/app/.venv-linux/bin/python \
  /opt/limbowave/app/scripts/server_backup.py \
  --data-dir /var/lib/limbowave --source "$BACKUP_FILE" --destination "$RESTORE_DIR"
```

输入独立备份密码。必须看到完整性通过且退出状态为 0；脚本先验证归档与 schema 兼容性，
再落盘，不会替你覆盖正在使用的资料库。

**重要：恢复后的 `vault.json` 用独立备份密码包装密钥，因此第一次解锁恢复资料库时，
输入的是备份密码，不是旧主密码。** 后续需要改密码时，使用实际支持的资料库密码管理流程，
不要手改 `vault.json` 或原始数据库密文。

### 11.3 停服后切换资料库

确认恢复内容、完整性、应用版本和权限后，再执行以下停机切换。
这会恢复到备份时点；备份之后的新消息不会自动合并。旧目录保留用于回退，不删除。

```bash
sudo systemctl stop limbowave
OLD_DATA_DIR="/var/lib/limbowave-before-restore-$(date +%Y%m%d-%H%M%S)"
test -d "$RESTORE_DIR"
sudo mv /var/lib/limbowave "$OLD_DATA_DIR"
sudo mv "$RESTORE_DIR" /var/lib/limbowave
sudo chown -R limbowave:limbowave /var/lib/limbowave
sudo chmod 0700 /var/lib/limbowave
sudo systemctl start limbowave
sudo -u limbowave /opt/limbowave/bin/limbowave server unlock --data-dir /var/lib/limbowave
```

恢复后在浏览器检查历史、模型配置和真实模型调用。设备授权表也随数据库进入备份；
换机器或怀疑原设备不可信时，列出并撤销旧设备，再重新配对。
恢复演练成功后才把备份视为可用。

## 12. 升级与回退

不要在提供服务的静态目录中直接在线构建。推荐维护窗口或独立构建目录，流程如下：

1. 记录当前提交、Node/Pi 版本、域名配置、systemd drop-in 与 Nginx 配置。
2. 主动锁定，按第 11 节创建并检查独立密码备份，然后停止服务。
3. 在管理员拥有的独立目录获取新提交，按第 4 节构建静态文件并同步锁定依赖。
4. 在测试环境核对第 5–7 节辅助脚本使用的 Python 接口，以及服务账号的读权限。
5. 部署到固定应用路径时重新创建/同步虚拟环境，或构建于其最终绝对路径；
   **不要盲目移动虚拟环境**，其中可能包含绝对路径。保留旧源码和发布记录。
6. 确认 `/opt/limbowave` 最终归 root 所有且服务账号只读，再启动服务。
7. 手动 SSH 解锁，重新验收网页、流式响应、真实模型回复、历史和设备撤销。

回退代码之前先判断数据库是否已经迁移。旧程序不一定能读取新 schema；
需要时按第 11 节恢复升级前备份，再启动匹配的旧版本。
不要只换旧二进制后强行打开已经升级的数据，也不要把主密码留在自动升级脚本里。

## 13. 局域网访问简版

这条路径不需要 VPS/systemd/Nginx；它使用正在运行的桌面应用。

1. 先在电脑完成模型配置与真实模型测试，解锁资料库。
2. 打开 **设置 → 局域网访问**，选择与手机互通的实际私有 IPv4，默认端口 8765。
   手机不能使用 `127.0.0.1`，也不能用 `0.0.0.0` 作为访问地址。
3. 推荐配置 PEM 证书及私钥；证书 SAN 必须包含 **IPAddress 类型的实际局域网 IP**，
   手机必须信任签发链。只信任根 CA，不把 CA 私钥或服务器私钥复制到手机。
4. 开启后以面板显示的实际地址访问，生成短期邀请并扫码/配对。
   配对后手机还须核验当前资料库密码，HTTPS 也不省略这一步。
5. 如明确选择无证书 HTTP，必须在桌面勾选风险确认，手机侧也需确认风险。
   这并不消除聊天明文、主动篡改和中间人风险，只用于明确接受风险的可信网络。
6. 主机运行、资料库解锁、网络互通缺一不可；休眠、退出、关闭服务或撤销后连接不可继续。
   服务重启后重新配对/核验，不能套用服务端的持久化授权行为。

源码启动时也要先构建生产静态文件；发布包需要包含同一批构建资产。
只为可信私网按需放行选定端口，不关闭整个防火墙、不映射路由器公网端口、不绕过证书警告。
详细的 IP SAN、手机信任和真机验收要求见[局域网 Web 说明](docs/development/lan-web.md)。

## 14. 常见问题

| 现象 | 排查与处理 |
| --- | --- |
| 服务无法启动 | 看 `journalctl`；确认系统 Python、Node/Pi 可由服务账号执行，`vault.json`、目录权限、域名配置正确；不关闭 systemd 保护绕过 |
| 控制面不可用 | 确认服务正在运行、使用 `limbowave` 账号与同一个 `--data-dir`，检查 `server.sock`；Windows 不能替代 Linux Unix socket 路径 |
| `400 invalid_host` | 比对 `server.env`、Nginx Host 和访问域名；确认 systemd 真正使用第 6 节适配入口；本地探测携带正确 Host |
| `400 invalid_proxy_headers` | 确认代理覆盖 HTTPS/Host 转发头，去除 `Forwarded`，不要透传不可信头 |
| `403 invalid_origin` / CSRF 错误 | 只从同一个规范 HTTPS origin 使用页面和 API；不要改成跨域静态站，不关 Origin/CSRF 校验 |
| `502 Bad Gateway` | 后端未启动、监听端口不一致、解锁切换期间短暂不可达；看服务日志与 loopback 健康检查 |
| 健康但一直锁定 | 启动/重启后的正常状态，SSH 交互执行 `server unlock` |
| 解锁成功但没有模型回复 | 可能仍为无内核降级；检查同一数据目录的配置、引用名、真实模型 ID、Pi PATH、网络与 `kernel.unavailable`；不能只看 `/healthz` |
| 页面白屏 / JS 404 | 检查生产静态目录、`index.html` 引用的哈希资产、发布位置和 MIME/CSP；不要启动 Vite 掩盖缺包 |
| 网页回复一次性出现 | 检查 Nginx/CDN 缓冲和流式传输；保留 `proxy_buffering off`，用真实模型验收 |
| `401` 或配对失败 | 邀请可能过期/已用，设备被撤销，或 Cookie 不可用；按实际模式重新配对，不公开分享邀请 |
| `409` / 忙碌 / 回执未知 | 先刷新状态、核对原操作回执，保留草稿；不要把不明确的结果当作“发送失败”自动重发 |
| `413` | 普通请求检查 64 KiB 上限；全量同步检查实体/字节预算；不随手同时扩大所有限制 |
| 证书签发或续期失败 | 核对 A/AAAA、80 可达性、ACME challenge、证书路径和重载机制；不改用忽略验证 |
| LAN 手机连接失败 | 实际 IP、主机休眠、AP/访客网络隔离、防火墙、VPN、证书 IP SAN 与手机信任；先查网络和证书，不映射公网端口 |

## 15. 上线验收清单

部署完成后逐项检查，记录提交 SHA、运行时版本、设备/浏览器和实际结果：

- [ ] 安装使用锁文件；Pi 精确版本为 0.86.0；Node/Pi 在服务身份下均可启动。
- [ ] Python 不依赖被 `ProtectHome` 隐藏的管理员家目录；应用/运行时归 root，资料库目录受限。
- [ ] 后端只监听 `127.0.0.1:8765`；公网未开放 8765，网页/API 同源。
- [ ] HTTPS 证书受浏览器信任；HTTP 只用于跳转/验证；续期 dry-run 与重载流程已核对。
- [ ] 系统启动后是 locked；网页无法远程解锁，也无自动解锁秘密配置。
- [ ] SSH 解锁与 ticket 配对有效，未配对浏览器不能读取会话；Cookie 安全属性正确。
- [ ] 收到真实模型的流式回复，刷新后历史仍在；不是仅构建、健康检查或 mock 内核通过。
- [ ] 刷新、断线、重连不自动重复发送；多设备冲突可恢复。
- [ ] 撤销设备立即阻止继续读取，锁定后无法继续使用；主机工具不向网页开放。
- [ ] 服务重启后需要 SSH 解锁；设备授权行为与所选模式一致。
- [ ] 备份已离机保存，独立备份密码单独保管；空目录恢复、备份密码解锁与回退演练成功。
- [ ] 已验证目标 Android/iOS/桌面浏览器，包括输入法、后台恢复、证书信任和 SSE。

尚未在真实服务器或真机执行的项目保持未勾选，不能用单元测试结果代替上线验收。

## 相关资料与维护依据

仓库内：

- [服务端开发基线](https://github.com/CN-Air84/LimboWave/blob/ea13d071c7c02744536cbbdccca43527994776fa/docs/development/server.md)
- [原服务端部署说明](https://github.com/CN-Air84/LimboWave/blob/ea13d071c7c02744536cbbdccca43527994776fa/docs/development/server-deployment.md)
- [配置与密钥](docs/development/configuration.md)
- [Linux 支持范围](docs/development/linux.md)
- [Pi Runtime 合同](docs/architecture/pi-runtime-contract.md)
- [局域网 Web 与打包验收](docs/development/lan-web.md)
- [Web 前端说明](web/README.md)
- [systemd 模板](https://github.com/CN-Air84/LimboWave/blob/ea13d071c7c02744536cbbdccca43527994776fa/deploy/limbowave.service) / [Nginx 模板](https://github.com/CN-Air84/LimboWave/blob/ea13d071c7c02744536cbbdccca43527994776fa/deploy/limbowave-nginx.conf)

外部命令核对参考（以官方文档和所部署版本为准）：

- [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)
- [uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)
- [Node.js 官方下载](https://nodejs.org/en/download)
- [Nginx 代理模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [Certbot 官方安装与 Nginx 操作说明](https://certbot.eff.org/instructions)
