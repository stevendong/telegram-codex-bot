# Telegram Codex Bot

通过 Telegram 私聊管理这台服务器上的多个独立 Codex Agent（Codex thread）和多套 Codex 登录账号。Agent 与项目目录无关；Telegram 侧只保存名称、账号和 thread ID，后台统一从 `/data` 启动。

## 功能

- 创建、切换、分叉和停止多个 Codex Agent
- 同时连接多套 Codex 登录，并通过 Agent 名称切换账号
- 自动以登录邮箱的前 10 个字符命名每套账号的主 Agent
- 按 Agent 查看并切换账号实际可用的 Codex 模型
- 查看、选择并使用账号可用的 reset 重置卡（二次确认）
- 每个 Agent 保留独立上下文
- Agent 回答使用 Telegram Rich Markdown 渲染标题、列表、代码、表格和公式
- 任务状态卡持续显示运行时长、当前阶段、事件数和计划完成度
- 状态卡提供可点击的中断按钮，同时支持 `/stop [名称]`
- 发现并导入服务器已有 Codex threads
- 不同 Agent 可以并行运行
- Telegram 用户 ID 白名单
- 等同 `codex --yolo`：`danger-full-access` 沙盒模式和 `approvalPolicy=never`
- systemd 不额外限制主机文件系统、网络或 `sudo`，可操作整台服务器
- 未预期的提权、文件修改审批和 MCP 征询默认拒绝
- 原子化状态持久化和 systemd 常驻
- 纯 Python 标准库，无额外 pip 依赖

## Telegram 命令

服务启动时会自动为所有私聊注册 Telegram 原生快捷命令菜单，并将聊天菜单按钮设为命令列表。

```text
/agents
/accounts
/models
/model <模型ID|default>
/agent <名称>
/newagent <名称> [账号]
/forkagent <新名称>
/renameagent <旧名称> <新名称>
/deleteagent <名称>
/purgeagent <名称> confirm
/threads [账号]
/importagent <名称> <thread_id> [账号]
/status  # 当前 Agent、Codex 登录和额度/重置时间
/reset   # 选择当前账号的 reset 重置卡
/clear   # 清除当前上下文并立即启动新会话
/stop [名称]
/help
```

`/deleteagent` 只删除 Telegram 别名，不删除 Codex 历史；`/purgeagent` 才会永久删除 thread。

`/threads [账号]` 会列出最近的历史会话并提供点击切换按钮。已绑定的会话会直接切换到对应 Agent；未绑定会话默认连接当前 Agent，查看其他账号时则连接该账号的主 Agent。切换不会删除原会话；如果原 thread 正被命令行或其他 Codex 进程写入，Bot 会自动分叉其已保存上下文并切换到新 thread，避免并发写入冲突。

`/agents` 会在每个 Agent 下显示其账号的短期和长期剩余 Usage 及对应重置时间，并提供可直接点击的切换按钮；多个 Agent 属于同一账号时只查询一次额度。`/models` 会实时读取当前 Agent 所属账号可用的模型；`/model <模型ID>` 仍可用于文字切换，选择会持久化到当前 Agent，并从下一次任务起生效。`/model default` 可恢复账号默认模型。

`/reset` 会读取当前 Agent 所属账号的可用重置卡。先选择卡片，再点击“确认使用”才会调用 Codex；卡片 ID 不会显示在 Telegram 中，选择按钮 10 分钟后失效。

`/clear` 会解除当前 Agent 与现有 thread 的绑定，立即创建全新的 thread，并保留 Agent 名称、账号和模型。旧 thread 不会被删除，仍可通过 `/threads` 查看和重新导入；若新 thread 创建失败，原上下文保持不变。

Agent 的最终回答优先通过 Telegram `sendRichMessage` 直接渲染 Rich Markdown，单条最多使用 30,000 字符并按 Markdown 块安全拆分；发送失败时自动降级为 HTML，再失败则发送纯文本，确保回答不会因格式错误而丢失。

启动后会为每套登录自动准备一个可切换的主 Agent，其名称固定为对应登录邮箱的前 10 个字符。升级时会保留原 Agent 的 thread、模型和当前选中状态并自动改名；首次向尚未启动的 Agent 发消息时才创建 thread。

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
CODEX_SANDBOX=danger-full-access
CODEX_ALLOW_DANGER_FULL_ACCESS=true
```

不要把真实 Token 写入仓库或发送到聊天中。

### 3. 启动

```bash
sudo systemctl enable --now telegram-codex-bot
sudo systemctl status telegram-codex-bot
sudo journalctl -u telegram-codex-bot -f
```

systemd 服务以 `ubuntu` 用户运行，分别复用 `CODEX_ACCOUNTS` 中配置的现有登录。服务允许 Codex 像交互式 `codex --yolo` 一样访问主机，并可使用该用户已有的免密 `sudo` 权限。不要把同一个正在终端中执行任务的 thread 再导入机器人并同时操作。

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

- 本部署明确使用 `danger-full-access + never`，Codex 可以读写主机上的任意可访问文件、联网和执行命令；若 `ubuntu` 有免密 `sudo`，也可取得 root 权限。
- Telegram Bot Token 或白名单账号一旦泄露，等同于服务器控制权泄露；必须只允许可信的私聊用户，并为 Telegram 账号启用两步验证。
- App Server 仅通过子进程 stdio 使用，没有监听公网端口。
- 仅私聊和白名单用户可以操作。
- Bot Token 的环境文件权限应保持 `0600`。
- `/purgeagent` 会永久删除 Codex thread，必须带 `confirm`。
- `/reset` 会消耗 reset 重置卡，必须通过按钮二次确认。
