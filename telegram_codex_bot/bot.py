from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from .agent_service import AgentService, normalize_agent_name
from .config import Config
from .state import StateStore
from .telegram_api import TelegramAPI, TelegramError

LOG = logging.getLogger(__name__)

BOT_COMMANDS = [
    {"command": "start", "description": "打开帮助"},
    {"command": "agents", "description": "查看并切换 Agent"},
    {"command": "accounts", "description": "查看 Codex 登录账号"},
    {"command": "models", "description": "查看当前账号可用模型"},
    {"command": "model", "description": "切换当前 Agent 模型"},
    {"command": "agent", "description": "切换当前 Agent"},
    {"command": "newagent", "description": "创建新 Agent"},
    {"command": "forkagent", "description": "分叉当前 Agent"},
    {"command": "renameagent", "description": "重命名 Agent"},
    {"command": "deleteagent", "description": "移除 Agent 别名"},
    {"command": "purgeagent", "description": "永久删除 Agent"},
    {"command": "threads", "description": "查看账号历史会话"},
    {"command": "importagent", "description": "导入已有会话"},
    {"command": "status", "description": "查看当前账号状态和额度"},
    {"command": "reset", "description": "选择并使用 reset 重置卡"},
    {"command": "stop", "description": "停止 Agent 当前任务"},
    {"command": "help", "description": "显示完整帮助"},
]

HELP = """Telegram Codex Bot

/agents — 列出已命名的 Agent
/accounts — 列出 Codex 登录账号
/models — 列出当前账号可用模型
/model <模型ID|default> — 切换当前 Agent 模型
/agent <名称> — 切换当前 Agent
/newagent <名称> [账号] — 创建独立 Agent
/forkagent <名称> — 从当前 Agent 分叉
/renameagent <旧名称> <新名称> — 重命名
/deleteagent <名称> — 仅移除 Telegram 别名，保留 Codex thread
/purgeagent <名称> confirm — 永久删除 Codex thread
/threads [账号] — 列出指定账号最近保存的 Codex threads
/importagent <名称> <thread_id> [账号] — 导入已有 thread
/status — 当前 Agent、Codex 登录及额度状态
/reset — 选择当前账号的 reset 重置卡（二次确认）
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
        self._reset_choices: dict[str, dict[str, Any]] = {}

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
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

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
        if not isinstance(user_id, int):
            await self._answer_callback(query_id, "无效用户", show_alert=True)
            return
        if user_id not in self.config.allowed_user_ids:
            LOG.warning("Ignored unauthorized Telegram user %s", user_id)
            return
        if chat.get("type") != "private":
            await self.telegram.send_message(chat_id, "仅允许在 Bot 私聊中使用。")
            return

        self.state.ensure_account_agents(
            user_id, self.service.account_aliases(), self.config.default_account
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

    async def _handle_callback_query(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not isinstance(query_id, str):
            return
        if user_id not in self.config.allowed_user_ids:
            LOG.warning("Ignored unauthorized Telegram callback user %s", user_id)
            await self._answer_callback(query_id, "无权操作此 Bot", show_alert=True)
            return
        if (
            not isinstance(chat_id, int)
            or not isinstance(message_id, int)
            or chat.get("type") != "private"
        ):
            await self._answer_callback(query_id, "仅允许在 Bot 私聊中使用", show_alert=True)
            return

        self.state.ensure_account_agents(
            user_id, self.service.account_aliases(), self.config.default_account
        )
        data = query.get("data")
        if not isinstance(data, str):
            await self._answer_callback(query_id, "无效按钮", show_alert=True)
            return
        try:
            if data == "noop":
                await self._answer_callback(query_id, "已经是当前选项")
                return
            if data.startswith("agent:"):
                name = normalize_agent_name(data.removeprefix("agent:"))
                if self.state.get_active_name(user_id) == name:
                    await self._answer_callback(query_id, "已经是当前 Agent")
                    return
                self.state.set_active(user_id, name)
                text, markup = self._agent_picker(user_id)
                await self._edit_or_send(chat_id, message_id, text, markup)
                await self._answer_callback(query_id, f"已切换到 Agent：{name}")
                return
            if data.startswith("model:"):
                payload = data.removeprefix("model:")
                if ":" not in payload:
                    raise ValueError("模型按钮已过期，请重新发送 /models")
                target_name, requested = payload.split(":", 1)
                target_name = normalize_agent_name(target_name)
                name = self.state.get_active_name(user_id)
                if target_name != name:
                    raise ValueError("当前 Agent 已改变，请重新发送 /models")
                model = await self.service.set_agent_model(
                    user_id, name, requested
                )
                text, markup = await self._model_picker(user_id)
                await self._edit_or_send(chat_id, message_id, text, markup)
                await self._answer_callback(query_id, f"已切换模型：{model}")
                return
            if data.startswith("resetpick:"):
                token = data.removeprefix("resetpick:")
                choice = self._get_reset_choice(token, user_id)
                self._validate_reset_choice(user_id, choice)
                text = self._format_reset_confirmation(choice)
                markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ 确认使用",
                                "callback_data": f"resetuse:{token}",
                            },
                            {
                                "text": "取消",
                                "callback_data": f"resetcancel:{token}",
                            },
                        ]
                    ]
                }
                await self._edit_or_send(chat_id, message_id, text, markup)
                await self._answer_callback(query_id, "请确认是否使用 reset 卡")
                return
            if data.startswith("resetcancel:"):
                token = data.removeprefix("resetcancel:")
                self._get_reset_choice(token, user_id)
                self._reset_choices.pop(token, None)
                await self._edit_or_send(
                    chat_id,
                    message_id,
                    "已取消，未使用 reset 重置卡。",
                    {"inline_keyboard": []},
                )
                await self._answer_callback(query_id, "已取消")
                return
            if data.startswith("resetuse:"):
                token = data.removeprefix("resetuse:")
                choice = self._get_reset_choice(token, user_id)
                self._validate_reset_choice(user_id, choice)
                self._reset_choices.pop(token, None)
                outcome = await self.service.consume_reset_credit(
                    str(choice["account"]), choice.get("credit_id")
                )
                result_text = {
                    "reset": "✅ reset 重置卡已使用，符合条件的额度窗口已重置。",
                    "nothingToReset": "当前没有符合条件、可以重置的额度窗口。",
                    "noCredit": "该账号当前没有可用的 reset 重置卡。",
                    "alreadyRedeemed": "这次 reset 请求已经成功处理过。",
                }.get(outcome, f"reset 请求已完成：{outcome}")
                picker_text, markup = await self._reset_picker(user_id)
                await self._edit_or_send(
                    chat_id, message_id, f"{result_text}\n\n{picker_text}", markup
                )
                await self._answer_callback(query_id, result_text[:170])
                return
            await self._answer_callback(query_id, "按钮已失效，请重新打开菜单", show_alert=True)
        except (ValueError, KeyError) as exc:
            detail = exc.args[0] if exc.args else str(exc)
            await self._answer_callback(
                query_id, f"操作失败：{str(detail)[:170]}", show_alert=True
            )
        except Exception:
            LOG.exception("Failed to handle Telegram callback")
            await self._answer_callback(query_id, "内部错误，请稍后重试", show_alert=True)

    async def _answer_callback(
        self, query_id: str, text: str, *, show_alert: bool = False
    ) -> None:
        try:
            await self.telegram.answer_callback_query(
                query_id, text, show_alert=show_alert
            )
        except TelegramError:
            LOG.warning("Could not answer Telegram callback", exc_info=True)

    async def _edit_or_send(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any],
    ) -> None:
        try:
            await self.telegram.edit_message_text(
                chat_id, message_id, text, reply_markup=reply_markup
            )
        except TelegramError:
            LOG.warning("Could not edit picker message; sending a new one", exc_info=True)
            await self.telegram.send_message(
                chat_id, text, reply_markup=reply_markup
            )

    async def _handle_command(
        self, user_id: int, chat_id: int, command: str, args: list[str]
    ) -> None:
        if command in {"start", "help"}:
            await self.telegram.send_message(chat_id, HELP)
        elif command == "agents":
            text, markup = self._agent_picker(user_id)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
            )
        elif command == "accounts":
            await self.telegram.send_message(chat_id, self._format_accounts(user_id))
        elif command in {"models", "model"}:
            name = self.state.get_active_name(user_id)
            if command == "model" and args:
                await self.service.set_agent_model(user_id, name, args[0])
            text, markup = await self._model_picker(user_id)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
            )
        elif command == "agent":
            self._require_args(args, 1, "/agent <名称>")
            name = normalize_agent_name(args[0])
            self.state.set_active(user_id, name)
            text, markup = self._agent_picker(user_id)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
            )
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
            agent = self.state.get_agent(user_id, old)
            if agent and agent.get("account_primary"):
                raise ValueError("账号主 Agent 的名称固定为登录邮箱前 10 个字符")
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
                user_id, self.service.account_aliases(), self.config.default_account
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
                user_id, self.service.account_aliases(), self.config.default_account
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
            agent["effective_model"] = (
                agent.get("model") or self.config.codex_model or "账号默认模型"
            )
            await self.telegram.send_message(
                chat_id,
                format_status(name, agent, account_status),
            )
        elif command == "reset":
            text, markup = await self._reset_picker(user_id)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
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

    def _agent_picker(self, user_id: int) -> tuple[str, dict[str, Any]]:
        active, agents = self.state.list_agents(user_id)
        if not agents:
            return (
                "尚无 Agent。使用 /newagent <名称> 创建。",
                {"inline_keyboard": []},
            )
        lines = ["Agent 切换", f"当前：{active}", ""]
        buttons: list[dict[str, str]] = []
        for name, agent in agents.items():
            marker = "✅" if name == active else "▫️"
            thread_marker = "已连接" if agent.get("thread_id") else "未启动"
            account = agent.get("account", self.config.default_account)
            model = agent.get("model") or self.config.codex_model or "默认模型"
            account_detail = ""
            account_alias = self.service.account_alias(str(account))
            if name != account_alias:
                account_detail = f" · 账号 {account_alias}"
            lines.append(
                f"{marker} {name}{account_detail} · {model} · "
                f"{agent.get('status', 'idle')} · {thread_marker}"
            )
            buttons.append(
                {
                    "text": f"{'✅ ' if name == active else ''}{name}",
                    "callback_data": "noop" if name == active else f"agent:{name}",
                }
            )
        lines.append("\n点击按钮即可切换：")
        return "\n".join(lines), {"inline_keyboard": _button_rows(buttons)}

    async def _model_picker(
        self, user_id: int
    ) -> tuple[str, dict[str, Any]]:
        name = self.state.get_active_name(user_id)
        agent = self.state.get_agent(user_id, name) or {}
        account = str(agent.get("account") or self.config.default_account)
        models = await self.service.list_models(account)
        selected_model = agent.get("model") or self.config.codex_model
        return format_models(
            name, self.service.account_alias(account), models, selected_model
        )

    async def _reset_picker(
        self, user_id: int
    ) -> tuple[str, dict[str, Any]]:
        self._prune_reset_choices()
        name = self.state.get_active_name(user_id)
        agent = self.state.get_agent(user_id, name) or {}
        account = str(agent.get("account") or self.config.default_account)
        snapshot = await self.service.get_reset_credits(account)
        old_tokens = [
            token
            for token, choice in self._reset_choices.items()
            if choice.get("user_id") == user_id
        ]
        for token in old_tokens:
            self._reset_choices.pop(token, None)
        available = int(snapshot.get("available_count") or 0)
        credits = snapshot.get("credits") or []
        lines = [
            "Reset 重置卡",
            f"Agent：{name}",
            f"可用：{available} 张",
        ]
        if available <= 0:
            lines.append("\n当前账号没有可用的 reset 重置卡。")
            return "\n".join(lines), {"inline_keyboard": []}

        lines.append("\n请选择要使用的卡；选择后还需再次确认：")
        buttons: list[list[dict[str, str]]] = []
        for index, credit in enumerate(credits, start=1):
            title = _single_line(credit.get("title") or f"Reset 卡 {index}", 50)
            description = _single_line(credit.get("description") or "", 100)
            expires_at = credit.get("expiresAt")
            detail = f"{index}. {title}"
            if description:
                detail += f" — {description}"
            if isinstance(expires_at, (int, float)):
                expiry = datetime.fromtimestamp(expires_at, UTC).astimezone()
                detail += f"（有效期至 {expiry:%Y-%m-%d %H:%M}）"
            lines.append(detail)
            token = self._store_reset_choice(
                user_id=user_id,
                agent=name,
                account=account,
                credit_id=str(credit["id"]),
                label=title,
                expires_at=expires_at,
            )
            buttons.append(
                [{"text": f"🎟 {title}", "callback_data": f"resetpick:{token}"}]
            )

        if available > len(credits):
            label = "由系统选择下一张 Reset 卡"
            token = self._store_reset_choice(
                user_id=user_id,
                agent=name,
                account=account,
                credit_id=None,
                label=label,
                expires_at=None,
            )
            buttons.append(
                [{"text": f"🎟 {label}", "callback_data": f"resetpick:{token}"}]
            )
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _store_reset_choice(
        self,
        *,
        user_id: int,
        agent: str,
        account: str,
        credit_id: str | None,
        label: str,
        expires_at: Any,
    ) -> str:
        token = secrets.token_urlsafe(9)
        while token in self._reset_choices:
            token = secrets.token_urlsafe(9)
        self._reset_choices[token] = {
            "user_id": user_id,
            "agent": agent,
            "account": account,
            "credit_id": credit_id,
            "label": label,
            "expires_at": expires_at,
            "created_monotonic": time.monotonic(),
        }
        return token

    def _prune_reset_choices(self) -> None:
        cutoff = time.monotonic() - 600
        expired = [
            token
            for token, choice in self._reset_choices.items()
            if float(choice.get("created_monotonic", 0)) < cutoff
        ]
        for token in expired:
            self._reset_choices.pop(token, None)

    def _get_reset_choice(self, token: str, user_id: int) -> dict[str, Any]:
        self._prune_reset_choices()
        choice = self._reset_choices.get(token)
        if not choice or choice.get("user_id") != user_id:
            raise ValueError("reset 按钮已过期，请重新发送 /reset")
        return choice

    def _validate_reset_choice(
        self, user_id: int, choice: dict[str, Any]
    ) -> None:
        if self.state.get_active_name(user_id) != choice.get("agent"):
            raise ValueError("当前 Agent 已改变，请重新发送 /reset")

    @staticmethod
    def _format_reset_confirmation(choice: dict[str, Any]) -> str:
        lines = [
            "确认使用 reset 重置卡？",
            f"Agent：{choice['agent']}",
            f"卡片：{choice['label']}",
        ]
        expires_at = choice.get("expires_at")
        if isinstance(expires_at, (int, float)):
            expiry = datetime.fromtimestamp(expires_at, UTC).astimezone()
            lines.append(f"有效期至：{expiry:%Y-%m-%d %H:%M}")
        lines.append("\n确认后会立即尝试重置符合条件的 Codex 额度窗口。")
        return "\n".join(lines)

    def _format_accounts(self, user_id: int) -> str:
        active_account = self._active_account(user_id)
        lines = ["Codex 账号："]
        for account in self.service.account_names():
            marker = "●" if account == active_account else "○"
            lines.append(f"{marker} {self.service.account_alias(account)}")
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
        f"模型：{agent.get('effective_model') or agent.get('model') or '账号默认模型'}",
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
    reset_credits = account_status.get("reset_credits") or {}
    available_resets = int(reset_credits.get("availableCount") or 0)
    lines.append(f"Reset 重置卡：{available_resets} 张可用")
    return "\n".join(lines)


def format_models(
    agent_name: str,
    account: str,
    models: list[dict[str, Any]],
    selected_model: str | None,
) -> tuple[str, dict[str, Any]]:
    if not models:
        return (
            f"账号 {account} 没有返回可用模型。",
            {"inline_keyboard": []},
        )
    default_model = next(
        (str(item.get("model")) for item in models if item.get("isDefault")),
        None,
    )
    active_model = selected_model or default_model
    active_item = next(
        (item for item in models if item.get("model") == active_model),
        None,
    )
    active_name = (
        str(active_item.get("displayName") or active_model)
        if active_item
        else str(active_model or "账号默认模型")
    )
    lines = [
        "模型切换",
        f"Agent：{agent_name}",
        f"Codex 账号：{account}",
        f"当前模型：{active_name}（{active_model or 'default'}）",
        "",
        "点击按钮切换，下次任务起生效：",
    ]
    buttons: list[dict[str, str]] = []
    for item in models:
        model = str(item.get("model") or item.get("id") or "unknown")
        display_name = str(item.get("displayName") or model)
        is_active = model == active_model
        default_marker = " · 默认" if item.get("isDefault") else ""
        buttons.append(
            {
                "text": f"{'✅ ' if is_active else ''}{display_name}{default_marker}",
                "callback_data": (
                    "noop" if is_active else f"model:{agent_name}:{model}"
                ),
            }
        )
    default_is_active = active_model == default_model
    buttons.append(
        {
            "text": "↩️ 使用账号默认模型",
            "callback_data": (
                "noop" if default_is_active else f"model:{agent_name}:default"
            ),
        }
    )
    return "\n".join(lines), {"inline_keyboard": _button_rows(buttons)}


def _button_rows(
    buttons: list[dict[str, str]], columns: int = 2
) -> list[list[dict[str, str]]]:
    return [buttons[index : index + columns] for index in range(0, len(buttons), columns)]


def _rate_window_label(duration: Any, fallback: str) -> str:
    if not isinstance(duration, int) or duration <= 0:
        return fallback
    if duration % 1440 == 0:
        return f"{duration // 1440} 天额度"
    if duration % 60 == 0:
        return f"{duration // 60} 小时额度"
    return f"{duration} 分钟额度"


def _single_line(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit]
