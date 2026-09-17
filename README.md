# Telegram Codex Bot

通过 Telegram 私聊管理这台服务器上的多个独立 Codex Agent（Codex thread）和多套 Codex 登录账号。Agent 与项目目录无关；Telegram 侧只保存名称、账号和 thread ID，后台统一从 `/data` 启动。

## 功能

- 创建、切换、分叉和停止多个 Codex Agent
- 同时连接多套 Codex 登录，并通过 Agent 名称切换账号
- 每个 Agent 保留独立上下文
- 发现并导入服务器已有 Codex threads
- 不同 Agent 可以并行运行
- Telegram 用户 ID 白名单
- 默认 `workspace-write` 沙盒和 `approvalPolicy=never`
- 未预期的提权、文件修改审批和 MCP 征询默认拒绝
- 原子化状态持久化和 systemd 常驻
- 纯 Python 标准库，无额外 pip 依赖

## Telegram 命令

服务启动时会自动为所有私聊注册 Telegram 原生快捷命令菜单，并将聊天菜单按钮设为命令列表。

```text
/agents
/accounts
/agent <名称>
/newagent <名称> [账号]
/forkagent <新名称>
/renameagent <旧名称> <新名称>
/deleteagent <名称>
/purgeagent <名称> confirm
/threads [账号]
/importagent <名称> <thread_id> [账号]
/status  # 当前 Agent、Codex 登录和额度/重置时间
/stop [名称]
/help
```

`/deleteagent` 只删除 Telegram 别名，不删除 Codex 历史；`/purgeagent` 才会永久删除 thread。

启动后会为每套登录自动准备一个可切换的 Agent。本机默认显示 `main @default`、`account4 @account4`、`agentopt @agentopt`；发送 `/agent account4` 即切换账号。首次向尚未启动的 Agent 发消息时才创建 thread。

## 部署

### 1. 创建 Bot 并取得用户 ID

在 Telegram 的 `@BotFather` 创建 Bot，然后先给 Bot 发一条消息。在服务器执行：

```bash
read -rsp "Bot Token: " TELEGRAM_TOKEN
echo
curl -sS "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getUpdates"
unset TELEGRAM_TOKEN
```

返回 JSON 中的 `message.from.id` 是 Telegram 数字用户 ID。

### 2. 安装 systemd 配置

```bash
cd /data/telegram-codex-bot
sudo ./scripts/install-systemd.sh
sudoedit /etc/telegram-codex-bot.env
```

至少填写：

```ini
TELEGRAM_BOT_TOKEN=BotFather生成的Token
TELEGRAM_ALLOWED_USER_IDS=你的数字用户ID
CODEX_ACCOUNTS=default=/home/ubuntu/.codex,account4=/home/ubuntu/.codex-account-4,agentopt=/home/ubuntu/.codex-agent-opt
CODEX_DEFAULT_ACCOUNT=default
```

不要把真实 Token 写入仓库或发送到聊天中。

### 3. 启动

```bash
sudo systemctl enable --now telegram-codex-bot
sudo systemctl status telegram-codex-bot
sudo journalctl -u telegram-codex-bot -f
```

systemd 服务以 `ubuntu` 用户运行，分别复用 `CODEX_ACCOUNTS` 中配置的现有登录。不要把同一个正在终端中执行任务的 thread 再导入机器人并同时操作。

## 手动运行

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_ALLOWED_USER_IDS='123456789'
export CODEX_BOT_STATE_FILE=/tmp/telegram-codex-state.json
cd /data/telegram-codex-bot
python3 -m telegram_codex_bot
```

## 验证

```bash
cd /data/telegram-codex-bot
python3 -m unittest discover -s tests -v
python3 -m compileall -q telegram_codex_bot tests
```

无需 Telegram Token 的 App Server 握手检查：

```bash
python3 scripts/check-app-server.py
```

## 安全说明

- 默认只能写 `/data`；不要轻易改成 `danger-full-access`。
- App Server 仅通过子进程 stdio 使用，没有监听公网端口。
- 仅私聊和白名单用户可以操作。
- Bot Token 的环境文件权限应保持 `0600`。
- `/purgeagent` 会永久删除 Codex thread，必须带 `confirm`。
