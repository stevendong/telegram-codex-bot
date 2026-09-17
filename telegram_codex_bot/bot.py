from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from .agent_service import AgentService, normalize_agent_name
from .config import Config
from .state import StateStore
from .telegram_api import TelegramAPI, TelegramError

LOG = logging.getLogger(__name__)

HELP = """Telegram Codex Bot

/agents — 列出已命名的 Agent
/accounts — 列出 Codex 登录账号
/agent <名称> — 切换当前 Agent
/newagent <名称> [账号] — 创建独立 Agent
/forkagent <名称> — 从当前 Agent 分叉
/renameagent <旧名称> <新名称> — 重命名
/deleteagent <名称> — 仅移除 Telegram 别名，保留 Codex thread
/purgeagent <名称> confirm — 永久删除 Codex thread
/threads [账号] — 列出指定账号最近保存的 Codex threads
/importagent <名称> <thread_id> [账号] — 导入已有 thread
/status — 当前 Agent、Codex 登录及额度状态
/stop [名称] — 中止 Agent 当前任务
/help — 显示帮助

普通文本会发送给当前 Agent。切换 Agent 不会停止其他 Agent 的任务。"""


def parse_command(text: str) -> tuple[str, list[str]] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0][1:].split("@", 1)[0].lower()
    return command, parts[1:]


class TelegramCodexBot:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        service: AgentService,
        telegram: TelegramAPI,
    ):
        self.config = config
        self.state = state
        self.service = service
        self.telegram = telegram
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop_event: asyncio.Event) -> None:
        offset = self.state.get_telegram_offset()
        backoff = 1
        while not stop_event.is_set():
            try:
                updates = await self.telegram.get_updates(
                    offset, self.config.poll_timeout_seconds
                )
                backoff = 1
                for update in updates:
                    offset = max(offset, int(update["update_id"]) + 1)
                    self.state.set_telegram_offset(offset)
                    task = asyncio.create_task(self._handle_update(update))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except TelegramError:
                LOG.exception("Telegram polling failed; retrying in %ss", backoff)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)

        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=10)
            del done
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if not isinstance(text, str):
            return
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if user_id not in self.config.allowed_user_ids:
            LOG.warning("Ignored unauthorized Telegram user %s", user_id)
            return
        if chat.get("type") != "private":
            await self.telegram.send_message(chat_id, "仅允许在 Bot 私聊中使用。")
            return

        self.state.ensure_account_agents(
            user_id, self.service.account_names(), self.config.default_account
        )
        command = parse_command(text)
        try:
            if command:
                await self._handle_command(user_id, chat_id, *command)
            else:
                await self._handle_prompt(user_id, chat_id, text)
        except (ValueError, KeyError) as exc:
            detail = exc.args[0] if exc.args else str(exc)
            await self.telegram.send_message(chat_id, f"操作失败：{detail}")
        except Exception as exc:
            LOG.exception("Failed to handle Telegram update")
            await self.telegram.send_message(chat_id, f"内部错误：{exc}")

    async def _handle_command(
        self, user_id: int, chat_id: int, command: str, args: list[str]
    ) -> None:
        if command in {"start", "help"}:
            await self.telegram.send_message(chat_id, HELP)
        elif command == "agents":
            await self.telegram.send_message(chat_id, self._format_agents(user_id))
        elif command == "accounts":
            await self.telegram.send_message(chat_id, self._format_accounts(user_id))
        elif command == "agent":
            self._require_args(args, 1, "/agent <名称>")
            name = normalize_agent_name(args[0])
            self.state.set_active(user_id, name)
            await self.telegram.send_message(chat_id, f"已切换到 Agent：{name}")
        elif command == "newagent":
            self._require_args(args, 1, "/newagent <名称> [账号]")
            account = args[1].lower() if len(args) > 1 else self._active_account(user_id)
            name = await self.service.create_agent(user_id, args[0], account)
            await self.telegram.send_message(
                chat_id, f"已创建并切换到 Agent：{name}（账号：{account}）"
            )
        elif command == "forkagent":
            self._require_args(args, 1, "/forkagent <新名称>")
            name = await self.service.fork_agent(user_id, args[0])
            await self.telegram.send_message(chat_id, f"已分叉并切换到 Agent：{name}")
        elif command in {"rename", "renameagent"}:
            self._require_args(args, 2, "/renameagent <旧名称> <新名称>")
            old = normalize_agent_name(args[0])
            new = normalize_agent_name(args[1])
            self.state.rename_agent(user_id, old, new)
            await self.telegram.send_message(chat_id, f"已将 {old} 重命名为 {new}")
        elif command == "deleteagent":
            self._require_args(args, 1, "/deleteagent <名称>")
            name = normalize_agent_name(args[0])
            agent = self.state.get_agent(user_id, name)
            if not agent:
                raise KeyError(name)
            if agent.get("status") == "running":
                raise ValueError("Agent 正在运行，请先 /stop")
            self.state.detach_agent(user_id, name)
            self.state.ensure_account_agents(
                user_id, self.service.account_names(), self.config.default_account
            )
            await self.telegram.send_message(
                chat_id, f"已移除别名 {name}；Codex thread 仍保留，可用 /threads 找回。"
            )
        elif command == "purgeagent":
            self._require_args(args, 2, "/purgeagent <名称> confirm")
            if args[1].lower() != "confirm":
                raise ValueError("永久删除必须以 confirm 确认")
            name = normalize_agent_name(args[0])
            await self.service.purge_agent(user_id, name)
            self.state.ensure_account_agents(
                user_id, self.service.account_names(), self.config.default_account
            )
            await self.telegram.send_message(chat_id, f"已永久删除 Agent：{name}")
        elif command == "threads":
            account = args[0].lower() if args else self._active_account(user_id)
            threads = await self.service.list_server_threads(account)
            await self.telegram.send_message(
                chat_id, self._format_threads(account, threads)
            )
        elif command == "importagent":
            self._require_args(args, 2, "/importagent <名称> <thread_id> [账号]")
            account = args[2].lower() if len(args) > 2 else self._active_account(user_id)
            name = await self.service.import_agent(user_id, args[0], args[1], account)
            await self.telegram.send_message(
                chat_id, f"已导入并切换到 Agent：{name}（账号：{account}）"
            )
        elif command == "status":
            name = self.state.get_active_name(user_id)
            agent = self.state.get_agent(user_id, name) or {}
            account = str(agent.get("account") or self.config.default_account)
            account_status = await self.service.get_account_status(account)
            await self.telegram.send_message(
                chat_id,
                format_status(name, agent, account_status),
            )
        elif command == "stop":
            name = normalize_agent_name(args[0]) if args else self.state.get_active_name(user_id)
            stopped = await self.service.stop_agent(user_id, name)
            message = f"已请求停止 Agent：{name}" if stopped else f"Agent {name} 当前没有运行任务"
            await self.telegram.send_message(chat_id, message)
        else:
            await self.telegram.send_message(chat_id, f"未知命令：/{command}\n\n{HELP}")

    async def _handle_prompt(self, user_id: int, chat_id: int, text: str) -> None:
        name = self.state.get_active_name(user_id)
        agent = self.state.get_agent(user_id, name) or {}
        if agent.get("status") == "running":
            await self.telegram.send_message(chat_id, f"Agent {name} 正在运行；这条消息已排队。")
        else:
            await self.telegram.send_message(chat_id, f"⏳ Agent {name} 已接收任务。")
        try:
            await self.telegram.send_typing(chat_id)
        except TelegramError:
            LOG.debug("sendChatAction failed", exc_info=True)
        result = await self.service.run_turn(user_id, name, text)
        prefix = f"[{name}]"
        if result.status == "completed":
            await self.telegram.send_message(chat_id, f"{prefix}\n{result.text}")
        elif result.status == "interrupted":
            await self.telegram.send_message(chat_id, f"{prefix} 已中止\n{result.text}")
        else:
            detail = result.error or result.text
            await self.telegram.send_message(chat_id, f"{prefix} 执行失败\n{detail}")

    def _format_agents(self, user_id: int) -> str:
        active, agents = self.state.list_agents(user_id)
        if not agents:
            return "尚无 Agent。使用 /newagent <名称> 创建。"
        lines = ["Agents："]
        for name, agent in agents.items():
            marker = "●" if name == active else "○"
            thread_marker = "已连接" if agent.get("thread_id") else "未启动"
            account = agent.get("account", self.config.default_account)
            lines.append(
                f"{marker} {name} @{account} — {agent.get('status', 'idle')} / {thread_marker}"
            )
        return "\n".join(lines)

    def _format_accounts(self, user_id: int) -> str:
        active_account = self._active_account(user_id)
        lines = ["Codex 账号："]
        for account in self.service.account_names():
            marker = "●" if account == active_account else "○"
            lines.append(f"{marker} {account}")
        lines.append("\n通过 /agent <名称> 切换对应账号的 Agent。")
        return "\n".join(lines)

    def _active_account(self, user_id: int) -> str:
        name = self.state.get_active_name(user_id)
        agent = self.state.get_agent(user_id, name) or {}
        return str(agent.get("account") or self.config.default_account)

    @staticmethod
    def _format_threads(account: str, threads: list[dict[str, Any]]) -> str:
        if not threads:
            return f"账号 {account} 没有可导入的 Codex thread。"
        lines = [f"账号 {account} 最近的 Codex threads："]
        for item in threads:
            title = item.get("name") or item.get("preview") or "未命名"
            title = " ".join(str(title).split())[:70]
            status_obj = item.get("status") or {}
            status = status_obj.get("type") if isinstance(status_obj, dict) else status_obj
            lines.extend(
                [
                    f"\n{title}",
                    f"状态：{status or 'unknown'}",
                    f"ID：{item.get('id')}",
                ]
            )
        lines.append(f"\n导入：/importagent <名称> <ID> {account}")
        return "\n".join(lines)

    @staticmethod
    def _require_args(args: list[str], count: int, usage: str) -> None:
        if len(args) < count:
            raise ValueError(f"用法：{usage}")


def format_status(
    name: str,
    agent: dict[str, Any],
    account_status: dict[str, Any],
) -> str:
    account_name = str(
        account_status.get("name") or agent.get("account") or "unknown"
    )
    thread_id = agent.get("thread_id") or "尚未创建"
    lines = [
        f"当前 Agent：{name}",
        f"Codex 账号：{account_name}",
        f"任务状态：{agent.get('status', 'idle')}",
        f"Thread：{thread_id}",
    ]

    if not account_status.get("logged_in"):
        lines.append("登录状态：未登录")
        return "\n".join(lines)

    account_type = {
        "chatgpt": "ChatGPT",
        "apiKey": "API Key",
        "amazonBedrock": "Amazon Bedrock",
    }.get(
        str(account_status.get("type")),
        str(account_status.get("type") or "未知"),
    )
    plan = account_status.get("plan_type")
    login_detail = f"{account_type} / {plan}" if plan else account_type
    lines.append(f"登录状态：已登录（{login_detail}）")

    error = account_status.get("rate_limit_error")
    if error:
        lines.append(f"额度状态：查询失败（{str(error)[:160]}）")
        return "\n".join(lines)

    limits = account_status.get("rate_limits") or {}
    reached_type = limits.get("rateLimitReachedType")
    if reached_type:
        reached_text = {
            "rate_limit_reached": "已达到额度上限",
            "workspace_owner_credits_depleted": "工作区所有者额度已耗尽",
            "workspace_member_credits_depleted": "工作区成员额度已耗尽",
            "workspace_owner_usage_limit_reached": "工作区所有者已达到用量上限",
            "workspace_member_usage_limit_reached": "工作区成员已达到用量上限",
        }.get(str(reached_type), str(reached_type))
        lines.append(f"额度状态：{reached_text}")
    elif limits:
        lines.append("额度状态：可用")
    else:
        lines.append("额度状态：未返回额度信息")

    for key, fallback_label in (("primary", "主要额度"), ("secondary", "次要额度")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        label = _rate_window_label(duration, fallback_label)
        used = int(window.get("usedPercent", 0))
        remaining = max(0, 100 - used)
        line = f"{label}：已用 {used}%，剩余 {remaining}%"
        resets_at = window.get("resetsAt")
        if isinstance(resets_at, (int, float)):
            reset_time = datetime.fromtimestamp(resets_at, UTC).astimezone()
            line += f"，重置 {reset_time:%m-%d %H:%M}"
        lines.append(line)
    return "\n".join(lines)


def _rate_window_label(duration: Any, fallback: str) -> str:
    if not isinstance(duration, int) or duration <= 0:
        return fallback
    if duration % 1440 == 0:
        return f"{duration // 1440} 天额度"
    if duration % 60 == 0:
        return f"{duration // 60} 小时额度"
    return f"{duration} 分钟额度"
