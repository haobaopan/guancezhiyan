# 管策智研：GitHub + 学校虚拟机部署 V1

更新日期：2026-11-02。目标为现有 V2 邮箱密码版，入口是 `应用平台_v2.py`。
本文新增部署操作说明，不覆盖 `架构与部署_V2.md`，不改变业务代码。

## 当前状态与边界

- 已确认学校虚拟机地址：`10.27.80.201`，初始管理用户为 `root`。密码不写入本文件或 Git。
- 用户确认当前 Mac 已连接校园网或学校 VPN；当前探测 TCP 22 超时，仍需确认学校网络 ACL、虚拟机开机状态、SSH 端口和服务状态。
- 当前到该地址的路由经过 `en0`，网关为 `10.28.255.254`；这只是本机观察结果，不代表学校已允许跨网段 SSH。
- 本地已经初始化 Git 仓库，远端为 `https://github.com/haobaopan/guancezhiyan.git`，已验证远端当前为空。推送是否完成需比对远端 `main` 与本地提交；虚拟机尚未连接、尚未上线。
- 域名和受信任 HTTPS 证书尚未确认。先完成内网管理通路，再配置 HTTPS，不修改应用的 HTTPS 校验来绕过此项。
- 本地 43 项单元/HTTP/公共知识库回归通过；两套页面 JavaScript 语法检查通过。
- 使用独立临时数据启动真实入口：`/health`、`/`、`/style.css`、`/app.js` 正常；未登录 `/api/state` 返回 401，页面保留 CSP 和 nosniff。
- `docker compose -f deploy/compose.v3.yaml config --no-env-resolution --quiet` 通过。这是结构检查，不代表占位配置可上线。
- 本地尝试构建镜像，拉取 `python:3.13-slim` 时 Docker Hub 认证服务连接失败（`network is unreachable`）。容器构建、容器启动和服务器验收尚未完成。

## 方案与目录

沿用现有 Docker Compose + 宿主机 Nginx。它复用现有 Dockerfile，减少 Mac 与 VM 之间的运行环境差异。systemd 也是可选方案，但需额外维护宿主机 Python 与系统依赖；本次不切换方案。

| 内容 | 位置 | 是否进入 GitHub |
| --- | --- | --- |
| 应用源码、测试、迁移与配置模板 | Git 仓库 | 是 |
| 服务器真实配置与 pepper | `/opt/policy-app/.env.v3` | 否 |
| 公共知识库 | `/srv/policy/public/knowledge.sqlite3` | 否，单独传输 |
| 账户、项目、对话、上传资料 | `/srv/policy/data/` | 否 |
| 离线一致性备份 | `/srv/policy/backups/`，另需异机保存 | 否 |
| 学校接入资料、旧版本备份、日志 | 本地或受控服务器目录 | 否 |

新增 `deploy/compose.v3.yaml` 复用 `deploy/Dockerfile.v2`；独立容器项目名为 `policy-v3`。端口仍仅发布到 `127.0.0.1:8766`。持久目录与代码目录分离，缺少挂载源时直接失败，避免将缺失的数据库误建为空目录。`deploy/Dockerfile.v2.dockerignore` 仅允许所需源码进入构建上下文。

## 1. GitHub 仓库与本地代码

用户已创建仓库 `haobaopan/guancezhiyan`。本地 `origin` 指向 `https://github.com/haobaopan/guancezhiyan.git`，使用现有 Git 作者配置及系统凭据助手，不将认证凭证写入仓库。

本地已有 `.gitignore`，真实 `.env`、数据库、用户数据、学校资料和备份被排除；`.env.v2.example` 与 `.env.v3.example` 可以提交。首次推送前已检查候选文件中的凭证模式与本机 `.env` 密钥值；识别到的两个命中均为安全测试使用的固定假凭证，未发现其余命中。

首次推送后通过 `git ls-remote origin refs/heads/main` 与 `git rev-parse HEAD` 比对，确认 GitHub 上的代码与本地一致。仓库可见性在 GitHub 设置页确认，不能仅凭 Git 连接成功判断私有/公开状态。

## 2. 先打通学校 VM 的管理连接

```sh
ssh -o ConnectTimeout=8 root@10.27.80.201
```

在终端的密码提示中输入密码，不将密码附在命令里。首次连接时核对学校提供的主机指纹。

如果超时，请学校确认：VM 已开机；SSH 是否监听 TCP 22（如使用其他端口需明确告知）；学校防火墙是否允许当前校园网网段到 VM；VPN 是否仅支持网页代理或是否需要堡垒机。不能仅凭“VPN 已连接”判断 SSH 已可达。

连接成功后查看环境：

```sh
cat /etc/os-release
uname -m
ip -br addr
df -h
command -v docker
docker --version
docker compose version
```

初始账号登录后更换学校发放的初始密码，日常管理账户按学校规定设置。Docker 安装必须按检测到的系统选择官方步骤；未确认系统前不要执行 Ubuntu 专用安装脚本。

## 3. 让 VM 只读获取 GitHub 代码

私有仓库推荐使用该仓库专用的只读 Deploy key，避免把个人账户 token 写入服务器配置。若学校环境已有合适的 GitHub 认证，先复用并验证。

需要新 key 时，在 VM 生成专用 SSH key，不覆盖已有 key：

```sh
install -d -m 700 /root/.ssh
ssh-keygen -t ed25519 -f /root/.ssh/policy_github -C policy-vm-readonly
cat /root/.ssh/policy_github.pub
```

仅将 `.pub` 公钥添加到 GitHub 仓库 `Settings` → `Deploy keys`；不要勾选写权限，不上传私钥。首次 SSH 访问 GitHub 核对其官方公布的主机指纹。

确认 `/opt/policy-app` 不存在或为空，再克隆用户已创建的仓库：

```sh
GIT_SSH_COMMAND='ssh -i /root/.ssh/policy_github -o IdentitiesOnly=yes' \
  git clone git@github.com:haobaopan/guancezhiyan.git /opt/policy-app
```

## 4. 准备独立持久数据

在 VM 执行：

```sh
install -d -m 700 /srv/policy/data
chown 10001:10001 /srv/policy/data
install -d -m 755 /srv/policy/public
install -d -m 700 /srv/policy/backups
```

首次部署，在 Mac 单独传输公共知识库。若目标已有数据库，先确认用途并备份，不能直接覆盖：

```sh
scp '/Users/macbook/管策智研10.28版/knowledge.sqlite3' \
  root@10.27.80.201:/srv/policy/public/knowledge.sqlite3
```

在 VM 设置只读权限并比较源/目标 SHA-256：

```sh
chmod 644 /srv/policy/public/knowledge.sqlite3
sha256sum /srv/policy/public/knowledge.sqlite3
```

Mac 可执行 `shasum -a 256 knowledge.sqlite3`。约 300 MB 知识库不进入 Git 仓库。

## 5. 配置 HTTPS 与服务器参数

向学校确认：正式访问域名、域名解析、受信任证书/私钥安装位置、允许访问的校园网网段、是否由学校统一反向代理和终止 HTTPS。若学校统一提供代理，应根据实际链路检查客户端 IP 和转发头，不直接套用本机 Nginx 假设。

使用现有 `deploy/nginx.v2.conf` 作为模板，另建实际站点配置，替换 `server_name`、HTTP 跳转目标及证书路径。Nginx 代理目标为 `127.0.0.1:8766`，覆盖 `X-Real-IP`。检查 `nginx -t` 后再重载。

在 VM 的代码目录内，只在 `.env.v3` 不存在时复制模板：

```sh
cd /opt/policy-app
(umask 077; cp -n .env.v3.example .env.v3)
chmod 600 .env.v3
```

生成随机 pepper：`openssl rand -hex 32`，将值写入 `.env.v3` 的 `AUTH_CODE_PEPPER`；把 `APP_ORIGIN` 改为实际 `https://域名`。不保留占位符，不将用户模型 API key 配置到该文件。后续更新保留同一 pepper，不能每次部署重新生成。

域名/证书未准备好时，只能继续本机或 SSH 隧道内的验证；本次不为了上线将应用的外部监听配置改成 HTTP。部署时只开放学校确认来源到 HTTPS 入口，不开放应用端口 8766。

## 6. 构建与验收

确认 VM 能访问 GitHub、Docker 镜像仓库和系统包仓库，再执行：

```sh
cd /opt/policy-app
docker compose -f deploy/compose.v3.yaml config --quiet
docker compose -f deploy/compose.v3.yaml up -d --build --wait --wait-timeout 180
docker compose -f deploy/compose.v3.yaml ps
docker compose -f deploy/compose.v3.yaml logs --tail=100 policy
curl --fail http://127.0.0.1:8766/health
```

若镜像源不可达，先修复学校允许的网络通路或使用学校批准的镜像服务，不随意替换未知来源镜像。

从目标客户端访问 HTTPS 域名，确认：证书受信任、注册/登录/退出、刷新后的会话、公共知识库检索、项目与资料上传、导出、重启后数据保留。模型配置和真实供应商调用单独验证，涉及实际额度。未完成这些检查不能标记为上线成功。

## 7. 更新、备份与恢复

首次部署成功后记录当前 Git commit、实际访问地址、服务器配置路径和验收结果。

更新前停止应用并使用现有一致性备份工具。备份完成后可先恢复旧容器服务，再获取代码和构建，缩短备份窗口：

```sh
cd /opt/policy-app
docker compose -f deploy/compose.v3.yaml stop policy
python3 scripts/backup_v2.py backup --data /srv/policy/data --target /srv/policy/backups
docker compose -f deploy/compose.v3.yaml start policy
GIT_SSH_COMMAND='ssh -i /root/.ssh/policy_github -o IdentitiesOnly=yes' git pull --ff-only
docker compose -f deploy/compose.v3.yaml up -d --build --wait --wait-timeout 180
```

备份失败时先恢复原服务并排查，不继续更新。备份目录只保存应用数据，不含 `.env.v3`、公共知识库和 HTTPS 私钥，这些必须单独受控备份。将备份加密后保存到异机，并安排恢复演练。

恢复工具要求目标目录不存在；停服后先恢复至新目录并校验，不在线覆盖正在使用的数据库：

```sh
python3 scripts/backup_v2.py restore \
  --archive /srv/policy/backups/<BACKUP>.tar.gz \
  --target /srv/policy/restore-<VERSION>
```

若升级包含迁移，代码回退与数据版本必须匹配，不能只回退 Git commit 就声称恢复成功。
