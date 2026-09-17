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
PROGRESS_UPDATE_SECONDS = 8

BOT_COMMANDS = [
    {"command": "agents", "description": "查看并切换 Agent"},
    {"command": "models", "description": "查看当前账号可用模型"},
    {"command": "effort", "description": "切换当前模型推理强度"},
    {"command": "status", "description": "查看当前账号状态和额度"},
    {"command": "threads", "description": "查看账号历史会话"},
    {"command": "clear", "description": "清除当前上下文并启动新会话"},
    {"command": "stop", "description": "停止 Agent 当前任务"},
    {"command": "newagent", "description": "创建新 Agent"},
    {"command": "forkagent", "description": "分叉当前 Agent"},
    {"command": "accounts", "description": "查看 Codex 登录账号"},
    {"command": "reset", "description": "选择并使用 reset 重置卡"},
    {"command": "agent", "description": "切换当前 Agent"},
    {"command": "model", "description": "切换当前 Agent 模型"},
    {"command": "renameagent", "description": "重命名 Agent"},
    {"command": "deleteagent", "description": "移除 Agent 别名"},
    {"command": "importagent", "description": "导入已有会话"},
    {"command": "purgeagent", "description": "永久删除 Agent"},
    {"command": "help", "description": "显示完整帮助"},
    {"command": "start", "description": "打开帮助"},
]

HELP = """Telegram Codex Bot

/agents — 列出已命名的 Agent
/accounts — 列出 Codex 登录账号
/models — 列出当前账号可用模型
/model <模型ID|default> — 切换当前 Agent 模型
/effort <档位|default> — 切换当前模型的推理强度
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
/clear — 清除当前 Agent 上下文并立即启动新会话
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


def _format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分"
    if minutes:
        return f"{minutes} 分 {secs:02d} 秒"
    return f"{secs} 秒"


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
        self._thread_choices: dict[str, dict[str, Any]] = {}

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
                text, markup = await self._agent_picker(user_id)
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
            if data.startswith("effort:"):
                payload = data.removeprefix("effort:")
                if ":" not in payload:
                    raise ValueError("Effort 按钮已过期，请重新发送 /models")
                target_name, requested = payload.split(":", 1)
                target_name = normalize_agent_name(target_name)
                name = self.state.get_active_name(user_id)
                if target_name != name:
                    raise ValueError("当前 Agent 已改变，请重新发送 /models")
                effort = await self.service.set_agent_effort(
                    user_id, name, requested
                )
                text, markup = await self._model_picker(user_id)
                await self._edit_or_send(chat_id, message_id, text, markup)
                await self._answer_callback(
                    query_id, f"已切换 Effort：{effort or '模型默认'}"
                )
                return
            if data.startswith("thread:"):
                token = data.removeprefix("thread:")
                choice = self._get_thread_choice(token, user_id)
                name = str(choice["agent"])
                account = str(choice["account"])
                thread_id = str(choice["thread_id"])
                outcome = await self.service.switch_agent_thread(
                    user_id, name, account, thread_id
                )
                self.state.set_active(user_id, name)
                text, markup = await self._thread_picker(user_id, account)
                await self._edit_or_send(chat_id, message_id, text, markup)
                if outcome == "forked":
                    answer = "原会话正被其他 Codex 使用，已分叉上下文并切换"
                else:
                    answer = f"已切换到会话：{choice['label']}"
                await self._answer_callback(query_id, answer)
                return
            if data.startswith("stop:"):
                name = normalize_agent_name(data.removeprefix("stop:"))
                stopped = await self.service.stop_agent(user_id, name)
                if stopped:
                    current_text = str(message.get("text") or f"Agent {name} 正在运行")
                    if "正在请求中断" not in current_text:
                        current_text += "\n\n⏹ 正在请求中断……"
                    await self._edit_or_send(
                        chat_id,
                        message_id,
                        current_text,
                        {"inline_keyboard": []},
                    )
                    await self._answer_callback(query_id, f"已请求中断 Agent：{name}")
                else:
                    await self._answer_callback(query_id, f"Agent {name} 当前没有运行任务")
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
            text, markup = await self._agent_picker(user_id)
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
        elif command == "effort":
            name = self.state.get_active_name(user_id)
            if args:
                await self.service.set_agent_effort(user_id, name, args[0])
            text, markup = await self._model_picker(user_id)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
            )
        elif command == "agent":
            self._require_args(args, 1, "/agent <名称>")
            name = normalize_agent_name(args[0])
            self.state.set_active(user_id, name)
            text, markup = await self._agent_picker(user_id)
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
            text, markup = await self._thread_picker(user_id, account)
            await self.telegram.send_message(
                chat_id, text, reply_markup=markup
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
        elif command == "clear":
            name = self.state.get_active_name(user_id)
            had_context = await self.service.clear_agent_context(user_id, name)
            if had_context:
                message = (
                    f"已清除 Agent {name} 的当前上下文；"
                    "已启动全新会话。旧会话仍可通过 /threads 找回。"
                )
            else:
                message = f"Agent {name} 已启动全新会话。"
            await self.telegram.send_message(chat_id, message)
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
            initial_text = f"⏳ Agent {name} 正在运行；这条消息已排队。"
        else:
            initial_text = f"⏳ Agent {name} 已接收任务，正在启动 Codex。"
        stop_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "⏹ 中断任务",
                        "callback_data": f"stop:{name}",
                    }
                ]
            ]
        }
        status_message_id = await self.telegram.send_message(
            chat_id, initial_text, reply_markup=stop_markup
        )
        try:
            await self.telegram.send_typing(chat_id)
        except TelegramError:
            LOG.debug("sendChatAction failed", exc_info=True)
        started_monotonic = time.monotonic()
        turn_task = asyncio.create_task(
            self.service.run_turn(user_id, name, text)
        )
        try:
            while not turn_task.done():
                done, _ = await asyncio.wait(
                    {turn_task}, timeout=PROGRESS_UPDATE_SECONDS
                )
                if done:
                    break
                progress = self.service.get_turn_progress(user_id, name)
                if status_message_id is not None and progress:
                    await self._update_progress_message(
                        chat_id,
                        status_message_id,
                        name,
                        progress,
                        stop_markup,
                    )
                try:
                    await self.telegram.send_typing(chat_id)
                except TelegramError:
                    LOG.debug("sendChatAction refresh failed", exc_info=True)
            result = await turn_task
        except Exception:
            if not turn_task.done():
                turn_task.cancel()
                await asyncio.gather(turn_task, return_exceptions=True)
            if status_message_id is not None:
                try:
                    await self.telegram.edit_message_text(
                        chat_id,
                        status_message_id,
                        (
                            "❌ 任务执行失败\n"
                            f"Agent：{name}\n"
                            f"耗时：{_format_duration(int(time.monotonic() - started_monotonic))}"
                        ),
                        reply_markup={"inline_keyboard": []},
                    )
                except TelegramError:
                    LOG.warning("Could not mark progress message failed", exc_info=True)
            raise
        except BaseException:
            if not turn_task.done():
                turn_task.cancel()
                await asyncio.gather(turn_task, return_exceptions=True)
            raise

        elapsed = max(0, int(time.monotonic() - started_monotonic))
        if status_message_id is not None:
            final_status = {
                "completed": "✅ 任务已完成",
                "interrupted": "⏹ 任务已中止",
            }.get(result.status, "❌ 任务执行失败")
            try:
                await self.telegram.edit_message_text(
                    chat_id,
                    status_message_id,
                    f"{final_status}\nAgent：{name}\n耗时：{_format_duration(elapsed)}",
                    reply_markup={"inline_keyboard": []},
                )
            except TelegramError:
                LOG.warning("Could not finalize progress message", exc_info=True)
        if result.status == "completed":
            await self.telegram.send_rich_markdown(
                chat_id, f"## {name}\n\n{result.text}"
            )
        elif result.status == "interrupted":
            await self.telegram.send_rich_markdown(
                chat_id, f"## {name} · 已中止\n\n{result.text}"
            )
        else:
            detail = result.error or result.text
            await self.telegram.send_rich_markdown(
                chat_id, f"## {name} · 执行失败\n\n{detail}"
            )

    async def _update_progress_message(
        self,
        chat_id: int,
        message_id: int,
        name: str,
        progress: dict[str, Any],
        reply_markup: dict[str, Any],
    ) -> None:
        lines = [
            "⏳ 任务执行中",
            f"Agent：{name}",
            f"阶段：{progress.get('stage') or '正在处理'}",
        ]
        detail = progress.get("detail")
        if detail:
            lines.append(f"当前：{detail}")
        plan_total = int(progress.get("plan_total") or 0)
        if plan_total:
            plan_completed = int(progress.get("plan_completed") or 0)
            lines.append(f"计划进度：{plan_completed}/{plan_total}")
        lines.append(f"已完成事件：{int(progress.get('completed_items') or 0)}")
        lines.append(
            f"已运行：{_format_duration(int(progress.get('elapsed_seconds') or 0))}"
        )
        try:
            await self.telegram.edit_message_text(
                chat_id,
                message_id,
                "\n".join(lines),
                reply_markup=reply_markup,
            )
        except TelegramError:
            LOG.warning("Could not update progress message", exc_info=True)

    async def _agent_picker(
        self, user_id: int
    ) -> tuple[str, dict[str, Any]]:
        active, agents = self.state.list_agents(user_id)
        if not agents:
            return (
                "尚无 Agent。使用 /newagent <名称> 创建。",
                {"inline_keyboard": []},
            )
        accounts = list(
            dict.fromkeys(
                str(agent.get("account") or self.config.default_account)
                for agent in agents.values()
            )
        )
        usage_results = await asyncio.gather(
            *(self.service.get_account_usage(account) for account in accounts),
            return_exceptions=True,
        )
        usage_by_account = dict(zip(accounts, usage_results, strict=True))
        lines = ["Agent 切换", f"当前：{active}", ""]
        buttons: list[dict[str, str]] = []
        for name, agent in agents.items():
            marker = "✅" if name == active else "▫️"
            thread_marker = "已连接" if agent.get("thread_id") else "未启动"
            account = agent.get("account", self.config.default_account)
            model = agent.get("model") or self.config.codex_model or "默认模型"
            effort = agent.get("effort") or "模型默认 Effort"
            account_detail = ""
            account_alias = self.service.account_alias(str(account))
            if name != account_alias:
                account_detail = f" · 账号 {account_alias}"
            lines.append(
                f"{marker} {name}{account_detail} · {model} · {effort} · "
                f"{agent.get('status', 'idle')} · {thread_marker}"
            )
            lines.append(
                f"   Usage：{_format_compact_usage(usage_by_account[str(account)])}"
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
            name,
            self.service.account_alias(account),
            models,
            selected_model,
            agent.get("effort"),
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

    async def _thread_picker(
        self, user_id: int, account: str
    ) -> tuple[str, dict[str, Any]]:
        self._prune_thread_choices()
        target_name = self._thread_target_agent(user_id, account)
        active_name, agents = self.state.list_agents(user_id)
        threads = await self.service.list_server_threads(account)
        old_tokens = [
            token
            for token, choice in self._thread_choices.items()
            if choice.get("user_id") == user_id
        ]
        for token in old_tokens:
            self._thread_choices.pop(token, None)
        account_label = self.service.account_alias(account)
        if not threads:
            return (
                f"账号 {account_label} 没有可切换的 Codex thread。",
                {"inline_keyboard": []},
            )
        lines = [
            "会话切换",
            f"账号：{account_label}",
            f"未绑定的会话将连接到 Agent：{target_name}",
            "\n点击按钮即可切换；旧会话不会被删除：",
        ]
        buttons: list[list[dict[str, str]]] = []
        for index, item in enumerate(threads, start=1):
            thread_id = str(item.get("id") or "").strip()
            if not thread_id:
                continue
            title = _single_line(
                item.get("name") or item.get("preview") or "未命名", 70
            )
            status_obj = item.get("status") or {}
            status = (
                status_obj.get("type")
                if isinstance(status_obj, dict)
                else status_obj
            )
            bound_name = next(
                (
                    name
                    for name, agent in agents.items()
                    if str(
                        agent.get("account") or self.config.default_account
                    )
                    == account
                    and str(agent.get("thread_id") or "") == thread_id
                ),
                None,
            )
            choice_agent = bound_name or target_name
            selected = (
                choice_agent == active_name
                and thread_id
                == str((agents.get(choice_agent) or {}).get("thread_id") or "")
            )
            marker = "✅" if selected else f"{index}."
            binding = f" · Agent {bound_name}" if bound_name else ""
            lines.append(
                f"{marker} {title} · {status or 'unknown'}{binding}"
            )
            lines.append(f"   ID：{thread_id}")
            if selected:
                callback_data = "noop"
            else:
                token = self._store_thread_choice(
                    user_id=user_id,
                    agent=choice_agent,
                    account=account,
                    thread_id=thread_id,
                    label=title,
                )
                callback_data = f"thread:{token}"
            buttons.append(
                [
                    {
                        "text": f"{'✅ ' if selected else ''}{index}. {title[:48]}",
                        "callback_data": callback_data,
                    }
                ]
            )
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _thread_target_agent(self, user_id: int, account: str) -> str:
        active_name, agents = self.state.list_agents(user_id)
        if active_name:
            active = agents.get(active_name) or {}
            if str(active.get("account") or self.config.default_account) == account:
                return active_name
        for name, agent in agents.items():
            if (
                str(agent.get("account") or self.config.default_account) == account
                and agent.get("account_primary") is True
            ):
                return name
        for name, agent in agents.items():
            if str(agent.get("account") or self.config.default_account) == account:
                return name
        raise ValueError(f"账号 {account} 没有可用于切换会话的 Agent")

    def _store_thread_choice(
        self,
        *,
        user_id: int,
        agent: str,
        account: str,
        thread_id: str,
        label: str,
    ) -> str:
        token = secrets.token_urlsafe(9)
        while token in self._thread_choices:
            token = secrets.token_urlsafe(9)
        self._thread_choices[token] = {
            "user_id": user_id,
            "agent": agent,
            "account": account,
            "thread_id": thread_id,
            "label": label,
            "created_monotonic": time.monotonic(),
        }
        return token

    def _prune_thread_choices(self) -> None:
        cutoff = time.monotonic() - 600
        expired = [
            token
            for token, choice in self._thread_choices.items()
            if float(choice.get("created_monotonic", 0)) < cutoff
        ]
        for token in expired:
            self._thread_choices.pop(token, None)

    def _get_thread_choice(self, token: str, user_id: int) -> dict[str, Any]:
        self._prune_thread_choices()
        choice = self._thread_choices.get(token)
        if not choice or choice.get("user_id") != user_id:
            raise ValueError("会话按钮已过期，请重新发送 /threads")
        return choice

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
        f"Effort：{agent.get('effort') or '模型默认'}",
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
    selected_effort: str | None,
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
    effort_options = _model_effort_options(active_item or {})
    default_effort = str(
        (active_item or {}).get("defaultReasoningEffort") or ""
    ).strip()
    effective_effort = selected_effort or default_effort or "模型默认"
    effort_suffix = "（模型默认）" if selected_effort is None else ""
    lines = [
        "模型切换",
        f"Agent：{agent_name}",
        f"Codex 账号：{account}",
        f"当前模型：{active_name}（{active_model or 'default'}）",
        f"当前 Effort：{effective_effort}{effort_suffix}",
        "",
        "点击按钮切换，下次任务起生效：",
    ]
    model_buttons: list[dict[str, str]] = []
    for item in models:
        model = str(item.get("model") or item.get("id") or "unknown")
        display_name = str(item.get("displayName") or model)
        is_active = model == active_model
        default_marker = " · 默认" if item.get("isDefault") else ""
        model_buttons.append(
            {
                "text": f"{'✅ ' if is_active else ''}{display_name}{default_marker}",
                "callback_data": (
                    "noop" if is_active else f"model:{agent_name}:{model}"
                ),
            }
        )
    default_is_active = active_model == default_model
    model_buttons.append(
        {
            "text": "↩️ 使用账号默认模型",
            "callback_data": (
                "noop" if default_is_active else f"model:{agent_name}:default"
            ),
        }
    )
    keyboard = _button_rows(model_buttons)
    if effort_options:
        lines.extend(
            ["", f"可用 Effort：{'、'.join(effort_options)}"]
        )
        effort_buttons = [
            {
                "text": (
                    "✅ Effort 默认"
                    if selected_effort is None
                    else "↩️ Effort 默认"
                ),
                "callback_data": (
                    "noop"
                    if selected_effort is None
                    else f"effort:{agent_name}:default"
                ),
            }
        ]
        effort_buttons.extend(
            {
                "text": f"{'✅ ' if effort == selected_effort else ''}{effort}",
                "callback_data": (
                    "noop"
                    if effort == selected_effort
                    else f"effort:{agent_name}:{effort}"
                ),
            }
            for effort in effort_options
        )
        keyboard.extend(_button_rows(effort_buttons))
    else:
        lines.extend(["", "当前模型没有可调 Effort。"])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def _model_effort_options(model: dict[str, Any]) -> list[str]:
    efforts: list[str] = []
    for option in model.get("supportedReasoningEfforts") or []:
        if isinstance(option, dict):
            effort = str(option.get("reasoningEffort") or "").strip()
        else:
            effort = str(option).strip()
        if effort and effort not in efforts:
            efforts.append(effort)
    return efforts


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


def _format_compact_usage(value: Any) -> str:
    if isinstance(value, Exception):
        return "查询失败"
    if not isinstance(value, dict) or not value:
        return "未返回额度信息"
    windows: list[str] = []
    for key, fallback_label in (("primary", "主要"), ("secondary", "次要")):
        window = value.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        label = _rate_window_label(duration, fallback_label).removesuffix("额度")
        try:
            used = int(window.get("usedPercent", 0))
        except (TypeError, ValueError):
            continue
        remaining = max(0, min(100, 100 - used))
        detail = f"{label}剩余 {remaining}%"
        resets_at = window.get("resetsAt")
        if isinstance(resets_at, (int, float)):
            reset_time = datetime.fromtimestamp(resets_at, UTC).astimezone()
            detail += f"（重置 {reset_time:%m-%d %H:%M}）"
        windows.append(detail)
    if windows:
        return " · ".join(windows)
    if value.get("rateLimitReachedType"):
        return "已达到额度上限"
    return "未返回额度窗口"
