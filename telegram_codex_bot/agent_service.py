from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .app_server import AppServerError, CodexAppServer
from .config import Config
from .state import StateStore

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,31}$")
LOG = logging.getLogger(__name__)


def normalize_agent_name(value: str) -> str:
    name = value.strip().lower()
    if not AGENT_NAME_RE.fullmatch(name):
        raise ValueError("Agent 名称只能包含字母、数字和 . _ + @ -，长度 1–32")
    return name


@dataclass(slots=True)
class TurnResult:
    status: str
    text: str
    error: str | None = None


@dataclass(slots=True)
class _TurnRun:
    done: asyncio.Future[dict[str, Any]]
    turn_id: str | None = None
    final_messages: list[str] = field(default_factory=list)
    deltas: list[str] = field(default_factory=list)
    error: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    stage: str = "正在启动 Codex"
    detail: str | None = None
    completed_items: int = 0
    plan_completed: int = 0
    plan_total: int = 0
    stop_requested: bool = False


class AgentService:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        apps: dict[str, CodexAppServer],
    ):
        self.config = config
        self.state = state
        self.apps = apps
        for account, app in apps.items():
            app.add_notification_handler(
                lambda method, params, account=account: self._on_notification(
                    account, method, params
                )
            )
        self._loaded_threads: set[tuple[str, str]] = set()
        self._runs: dict[tuple[str, str], _TurnRun] = {}
        self._agent_runs: dict[tuple[int, str], _TurnRun] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(config.max_parallel_turns)
        self._account_aliases = {name: name for name in apps}

    def _thread_params(self, model: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.config.codex_cwd),
            "approvalPolicy": "never",
            "sandbox": self.config.codex_sandbox,
            "serviceName": "telegram_codex_bot",
        }
        selected_model = model or self.config.codex_model
        if selected_model:
            params["model"] = selected_model
        return params

    def account_names(self) -> list[str]:
        return list(self.apps)

    def account_aliases(self) -> dict[str, str]:
        return dict(self._account_aliases)

    def account_alias(self, account: str) -> str:
        return self._account_aliases.get(account, account)

    async def load_account_aliases(self) -> dict[str, str]:
        """Load privacy-safe account labels from the first 10 email characters."""
        aliases: dict[str, str] = {}
        for account, app in self.apps.items():
            try:
                response = await app.request(
                    "account/read", {"refreshToken": False}
                )
                account_data = response.get("account") or {}
                email = str(account_data.get("email") or "").strip()
                alias = normalize_agent_name(email[:10]) if email else account
            except Exception:
                LOG.warning(
                    "Could not read account email for %s; using configured name",
                    account,
                    exc_info=True,
                )
                alias = account
            if alias in aliases.values():
                raise ValueError(
                    f"多个 Codex 账号的邮箱前 10 个字符相同：{alias}"
                )
            aliases[account] = alias
        self._account_aliases = aliases
        return dict(aliases)

    def _app(self, account: str) -> CodexAppServer:
        try:
            return self.apps[account]
        except KeyError as exc:
            raise ValueError(f"未知 Codex 账号：{account}") from exc

    async def get_account_status(self, account: str) -> dict[str, Any]:
        """Return a privacy-safe login and quota snapshot for one Codex account."""
        app = self._app(account)
        account_response = await app.request(
            "account/read", {"refreshToken": False}
        )
        account_data = account_response.get("account") or {}
        status: dict[str, Any] = {
            "name": self.account_alias(account),
            "logged_in": bool(account_data),
            "type": account_data.get("type"),
            "plan_type": account_data.get("planType"),
            "rate_limits": {},
            "reset_credits": {},
            "rate_limit_error": None,
        }
        if not account_data:
            return status
        try:
            rate_limit_response = await app.request(
                "account/rateLimits/read", {}
            )
            status["rate_limits"] = rate_limit_response.get("rateLimits") or {}
            status["reset_credits"] = (
                rate_limit_response.get("rateLimitResetCredits") or {}
            )
        except Exception as exc:
            status["rate_limit_error"] = str(exc)
        return status

    async def get_account_usage(self, account: str) -> dict[str, Any]:
        """Return the current Codex rate-limit snapshot for one account."""
        response = await self._app(account).request(
            "account/rateLimits/read", {}
        )
        return dict(response.get("rateLimits") or {})

    async def get_reset_credits(self, account: str) -> dict[str, Any]:
        response = await self._app(account).request(
            "account/rateLimits/read", {}
        )
        summary = response.get("rateLimitResetCredits") or {}
        credits = [
            dict(item)
            for item in (summary.get("credits") or [])
            if isinstance(item, dict) and item.get("status") == "available"
        ]
        return {
            "available_count": int(summary.get("availableCount") or 0),
            "credits": credits,
            "rate_limits": response.get("rateLimits") or {},
        }

    async def consume_reset_credit(
        self, account: str, credit_id: str | None
    ) -> str:
        params: dict[str, Any] = {"idempotencyKey": str(uuid.uuid4())}
        if credit_id is not None:
            params["creditId"] = credit_id
        response = await self._app(account).request(
            "account/rateLimitResetCredit/consume", params
        )
        return str(response.get("outcome") or "unknown")

    async def list_models(self, account: str) -> list[dict[str, Any]]:
        app = self._app(account)
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = await app.request(
                "model/list",
                {
                    "cursor": cursor,
                    "limit": 100,
                    "includeHidden": False,
                },
            )
            models.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def set_agent_model(
        self, user_id: int, name: str, raw_model: str
    ) -> str | None:
        agent = self.state.get_agent(user_id, name)
        if not agent:
            raise KeyError(name)
        if agent.get("status") == "running":
            raise ValueError("Agent 正在运行，请完成或停止后再切换模型")
        requested = raw_model.strip()
        account = str(agent.get("account") or self.config.default_account)
        models = await self.list_models(account)
        if requested.lower() == "default":
            match = next((item for item in models if item.get("isDefault")), None)
        else:
            match = next(
                (
                    item
                    for item in models
                    if requested.lower()
                    in {
                        str(item.get("model", "")).lower(),
                        str(item.get("id", "")).lower(),
                    }
                ),
                None,
            )
        if match is None:
            if requested.lower() == "default":
                raise ValueError(f"账号 {account} 没有返回默认模型")
            raise ValueError(f"账号 {account} 不支持模型：{requested}")
        model = str(match["model"])
        supported_efforts = {
            effort.lower(): effort
            for effort in self._supported_efforts(match)
        }
        current_effort = str(agent.get("effort") or "")
        effort = supported_efforts.get(current_effort.lower())
        self.state.update_agent(user_id, name, model=model, effort=effort)
        return model

    async def set_agent_effort(
        self, user_id: int, name: str, raw_effort: str
    ) -> str | None:
        agent = self.state.get_agent(user_id, name)
        if not agent:
            raise KeyError(name)
        if agent.get("status") == "running":
            raise ValueError("Agent 正在运行，请完成或停止后再切换 Effort")
        account = str(agent.get("account") or self.config.default_account)
        models = await self.list_models(account)
        selected_model = agent.get("model") or self.config.codex_model
        if selected_model:
            model = next(
                (
                    item
                    for item in models
                    if str(selected_model).lower()
                    in {
                        str(item.get("model", "")).lower(),
                        str(item.get("id", "")).lower(),
                    }
                ),
                None,
            )
        else:
            model = next(
                (item for item in models if item.get("isDefault")), None
            )
        if model is None:
            raise ValueError(f"账号 {account} 没有返回当前模型信息")

        requested = raw_effort.strip()
        if requested.lower() == "default":
            effort = None
        else:
            supported = {
                value.lower(): value
                for value in self._supported_efforts(model)
            }
            effort = supported.get(requested.lower())
            if effort is None:
                choices = "、".join(supported.values()) or "无"
                raise ValueError(
                    f"模型 {model.get('model')} 不支持 Effort：{requested}；"
                    f"可用值：{choices}"
                )
        self.state.update_agent(user_id, name, effort=effort)
        return effort

    @staticmethod
    def _supported_efforts(model: dict[str, Any]) -> list[str]:
        efforts: list[str] = []
        for option in model.get("supportedReasoningEfforts") or []:
            if isinstance(option, dict):
                value = str(option.get("reasoningEffort") or "").strip()
            else:
                value = str(option).strip()
            if value and value not in efforts:
                efforts.append(value)
        return efforts

    async def create_agent(
        self,
        user_id: int,
        raw_name: str,
        account: str | None = None,
    ) -> str:
        name = normalize_agent_name(raw_name)
        if self.state.get_agent(user_id, name):
            raise ValueError(f"Agent 已存在：{name}")
        account = (account or self.config.default_account).lower()
        app = self._app(account)
        result = await app.request("thread/start", self._thread_params())
        thread_id = str(result["thread"]["id"])
        self._loaded_threads.add((account, thread_id))
        self.state.create_agent(user_id, name, thread_id, account=account)
        return name

    async def import_agent(
        self,
        user_id: int,
        raw_name: str,
        thread_id: str,
        account: str | None = None,
    ) -> str:
        name = normalize_agent_name(raw_name)
        if self.state.get_agent(user_id, name):
            raise ValueError(f"Agent 已存在：{name}")
        account = (account or self.config.default_account).lower()
        app = self._app(account)
        await app.request(
            "thread/read", {"threadId": thread_id.strip(), "includeTurns": False}
        )
        self.state.create_agent(
            user_id, name, thread_id.strip(), account=account
        )
        return name

    async def fork_agent(self, user_id: int, raw_name: str) -> str:
        name = normalize_agent_name(raw_name)
        if self.state.get_agent(user_id, name):
            raise ValueError(f"Agent 已存在：{name}")
        source_name = self.state.get_active_name(user_id)
        source = self.state.get_agent(user_id, source_name)
        if not source or not source.get("thread_id"):
            raise ValueError("当前 Agent 尚无可分叉的会话")
        if source.get("status") == "running":
            raise ValueError("当前 Agent 正在运行，请完成或停止后再分叉")
        account = str(source.get("account") or self.config.default_account)
        app = self._app(account)
        model = source.get("model")
        effort = source.get("effort")
        params = {"threadId": source["thread_id"], **self._thread_params(model)}
        result = await app.request("thread/fork", params)
        thread_id = str(result["thread"]["id"])
        self._loaded_threads.add((account, thread_id))
        self.state.create_agent(user_id, name, thread_id, account=account)
        self.state.update_agent(user_id, name, model=model, effort=effort)
        return name

    async def list_server_threads(
        self, account: str, limit: int = 15
    ) -> list[dict[str, Any]]:
        app = self._app(account)
        result = await app.request(
            "thread/list",
            {
                "limit": limit,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            },
        )
        return list(result.get("data", []))

    async def switch_agent_thread(
        self,
        user_id: int,
        name: str,
        account: str,
        thread_id: str,
    ) -> str:
        """Bind an Agent to an existing thread without deleting its old thread."""
        name = normalize_agent_name(name)
        lock = self._locks.setdefault((user_id, name), asyncio.Lock())
        if lock.locked():
            raise ValueError("Agent 正在运行，请完成或停止后再切换会话")
        async with lock:
            agent = self.state.get_agent(user_id, name)
            if not agent:
                raise KeyError(name)
            if agent.get("status") == "running":
                raise ValueError("Agent 正在运行，请完成或停止后再切换会话")
            agent_account = str(
                agent.get("account") or self.config.default_account
            )
            if agent_account != account:
                raise ValueError("所选会话与目标 Agent 不属于同一 Codex 账号")
            old_thread_id = agent.get("thread_id")
            if str(old_thread_id or "") == thread_id:
                return "unchanged"
            app = self._app(account)
            params = {
                "threadId": thread_id,
                **self._thread_params(agent.get("model")),
            }
            selected_thread_id = thread_id
            outcome = "resumed"
            try:
                await app.request("thread/resume", params)
            except AppServerError as exc:
                if "already has an active writer" not in str(exc):
                    raise
                result = await app.request("thread/fork", params)
                selected_thread_id = str(result["thread"]["id"])
                outcome = "forked"
            self.state.update_agent(
                user_id,
                name,
                thread_id=selected_thread_id,
                status="idle",
                active_turn_id=None,
                last_error=None,
            )
            self._loaded_threads.add((account, selected_thread_id))
            if old_thread_id:
                self._loaded_threads.discard((account, str(old_thread_id)))
            return outcome

    async def run_turn(self, user_id: int, name: str, text: str) -> TurnResult:
        name = normalize_agent_name(name)
        lock = self._locks.setdefault((user_id, name), asyncio.Lock())
        async with lock, self._semaphore:
            loop = asyncio.get_running_loop()
            run = _TurnRun(done=loop.create_future())
            agent_key = (user_id, name)
            self._agent_runs[agent_key] = run
            self.state.update_agent(
                user_id,
                name,
                status="running",
                active_turn_id=None,
                last_error=None,
            )
            account: str | None = None
            thread_id: str | None = None
            try:
                account, thread_id = await self._ensure_thread(user_id, name)
                app = self._app(account)
                agent = self.state.get_agent(user_id, name) or {}
                self._runs[(account, thread_id)] = run
                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                }
                selected_model = agent.get("model") or self.config.codex_model
                if selected_model:
                    turn_params["model"] = selected_model
                turn_params["effort"] = agent.get("effort")
                response = await app.request("turn/start", turn_params)
                run.turn_id = str(response["turn"]["id"])
                self.state.update_agent(user_id, name, active_turn_id=run.turn_id)
                if run.stop_requested:
                    await app.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": run.turn_id},
                    )
                completed = await asyncio.wait_for(
                    asyncio.shield(run.done),
                    timeout=self.config.turn_timeout_seconds,
                )
                status = str(completed.get("status", "completed"))
                error_obj = completed.get("error") or {}
                error = run.error or error_obj.get("message")
                text_result = "\n\n".join(msg for msg in run.final_messages if msg).strip()
                if not text_result:
                    text_result = "".join(run.deltas).strip()
                if not text_result:
                    text_result = "任务已结束，但 Codex 没有返回文本消息。"
                self.state.update_agent(
                    user_id,
                    name,
                    status="idle" if status == "completed" else status,
                    active_turn_id=None,
                    last_error=error,
                )
                return TurnResult(status=status, text=text_result, error=error)
            except TimeoutError:
                if account and thread_id and run.turn_id:
                    await app.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": run.turn_id},
                    )
                error = f"任务超过 {self.config.turn_timeout_seconds} 秒，已请求中止"
                self.state.update_agent(
                    user_id,
                    name,
                    status="failed",
                    active_turn_id=None,
                    last_error=error,
                )
                return TurnResult(status="failed", text=error, error=error)
            except Exception as exc:
                self.state.update_agent(
                    user_id,
                    name,
                    status="failed",
                    active_turn_id=None,
                    last_error=str(exc),
                )
                raise
            finally:
                if account and thread_id:
                    self._runs.pop((account, thread_id), None)
                if self._agent_runs.get(agent_key) is run:
                    self._agent_runs.pop(agent_key, None)

    def get_turn_progress(
        self, user_id: int, name: str
    ) -> dict[str, Any] | None:
        run = self._agent_runs.get((user_id, normalize_agent_name(name)))
        if not run:
            return None
        return {
            "stage": run.stage,
            "detail": run.detail,
            "completed_items": run.completed_items,
            "plan_completed": run.plan_completed,
            "plan_total": run.plan_total,
            "elapsed_seconds": max(0, int(time.monotonic() - run.started_monotonic)),
            "stop_requested": run.stop_requested,
        }

    async def stop_agent(self, user_id: int, name: str) -> bool:
        name = normalize_agent_name(name)
        agent = self.state.get_agent(user_id, name)
        if not agent:
            raise KeyError(name)
        run = self._agent_runs.get((user_id, name))
        if not run or run.done.done():
            return False
        if run.stop_requested:
            return True
        run.stop_requested = True
        run.stage = "正在中断任务"
        run.detail = None
        thread_id = agent.get("thread_id")
        turn_id = run.turn_id or agent.get("active_turn_id")
        if not thread_id or not turn_id:
            return True
        account = str(agent.get("account") or self.config.default_account)
        await self._app(account).request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        return True

    async def clear_agent_context(self, user_id: int, name: str) -> bool:
        """Replace the current thread with a fresh session, preserving settings."""
        name = normalize_agent_name(name)
        lock = self._locks.setdefault((user_id, name), asyncio.Lock())
        if lock.locked():
            raise ValueError("Agent 正在运行，请完成或停止后再清除上下文")
        async with lock:
            agent = self.state.get_agent(user_id, name)
            if not agent:
                raise KeyError(name)
            if agent.get("status") == "running":
                raise ValueError("Agent 正在运行，请完成或停止后再清除上下文")
            account = str(agent.get("account") or self.config.default_account)
            old_thread_id = agent.get("thread_id")
            app = self._app(account)
            result = await app.request(
                "thread/start", self._thread_params(agent.get("model"))
            )
            new_thread_id = str(result["thread"]["id"])
            self.state.update_agent(
                user_id,
                name,
                thread_id=new_thread_id,
                status="idle",
                active_turn_id=None,
                last_error=None,
            )
            self._loaded_threads.add((account, new_thread_id))
            if old_thread_id:
                self._loaded_threads.discard((account, str(old_thread_id)))
            return bool(old_thread_id)

    async def purge_agent(self, user_id: int, name: str) -> None:
        agent = self.state.get_agent(user_id, name)
        if not agent:
            raise KeyError(name)
        if agent.get("status") == "running":
            raise ValueError("Agent 正在运行，请先 /stop")
        thread_id = agent.get("thread_id")
        account = str(agent.get("account") or self.config.default_account)
        if thread_id:
            await self._app(account).request(
                "thread/delete", {"threadId": thread_id}
            )
            self._loaded_threads.discard((account, thread_id))
        self.state.detach_agent(user_id, name)

    async def _ensure_thread(self, user_id: int, name: str) -> tuple[str, str]:
        agent = self.state.get_agent(user_id, name)
        if not agent:
            raise KeyError(name)
        account = str(agent.get("account") or self.config.default_account)
        app = self._app(account)
        model = agent.get("model")
        thread_id = agent.get("thread_id")
        if not thread_id:
            result = await app.request("thread/start", self._thread_params(model))
            thread_id = str(result["thread"]["id"])
            self.state.update_agent(user_id, name, thread_id=thread_id)
            self._loaded_threads.add((account, thread_id))
            return account, thread_id
        if (account, thread_id) not in self._loaded_threads:
            params = {"threadId": thread_id, **self._thread_params(model)}
            await app.request("thread/resume", params)
            self._loaded_threads.add((account, thread_id))
        return account, str(thread_id)

    def _on_notification(
        self, account: str, method: str, params: dict[str, Any]
    ) -> None:
        thread_id = params.get("threadId")
        run = (
            self._runs.get((account, str(thread_id)))
            if thread_id
            else None
        )
        if not run:
            turn = params.get("turn") or {}
            turn_id = params.get("turnId") or (
                turn.get("id") if isinstance(turn, dict) else None
            )
            if turn_id:
                run = next(
                    (
                        candidate
                        for (run_account, _), candidate in self._runs.items()
                        if run_account == account
                        and candidate.turn_id == str(turn_id)
                    ),
                    None,
                )
        if not run:
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                run.deltas.append(delta)
        elif method == "turn/started":
            run.stage = "正在分析任务"
            run.detail = None
        elif method == "turn/plan/updated":
            plan = params.get("plan") or []
            if isinstance(plan, list):
                run.plan_total = len(plan)
                run.plan_completed = sum(
                    1
                    for step in plan
                    if isinstance(step, dict)
                    and step.get("status") == "completed"
                )
                active_step = next(
                    (
                        step.get("step")
                        for step in plan
                        if isinstance(step, dict)
                        and step.get("status") == "inProgress"
                    ),
                    None,
                )
                run.stage = "正在执行计划"
                run.detail = _progress_detail(active_step)
        elif method == "item/started":
            item = params.get("item") or {}
            run.stage, run.detail = _item_progress(item)
        elif method == "item/completed":
            item = params.get("item") or {}
            run.completed_items += 1
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                if item.get("phase") == "commentary":
                    run.stage = "Codex 进度更新"
                    run.detail = _progress_detail(item["text"], limit=240)
                else:
                    run.final_messages.append(item["text"])
                    run.stage = "正在整理最终回复"
                    run.detail = None
            elif not run.stop_requested:
                run.stage = "继续处理中"
                run.detail = None
        elif method == "turn/diff/updated":
            run.stage = "正在整理代码改动"
            run.detail = None
        elif method == "item/commandExecution/outputDelta":
            run.stage = "命令仍在执行"
            run.detail = None
        elif method == "error":
            error = params.get("error") or {}
            run.error = str(error.get("message") or "Codex turn failed")
            run.stage = "任务执行失败"
            run.detail = _progress_detail(run.error)
        elif method == "turn/completed" and not run.done.done():
            run.done.set_result(params.get("turn") or {})
        if run.stop_requested and method not in {"error", "turn/completed"}:
            run.stage = "正在中断任务"
            run.detail = None


def _progress_detail(value: Any, limit: int = 120) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _item_progress(item: dict[str, Any]) -> tuple[str, str | None]:
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        return "正在执行命令", _progress_detail(item.get("cwd"))
    if item_type == "fileChange":
        changes = item.get("changes") or []
        count = len(changes) if isinstance(changes, list) else 0
        detail = f"{count} 个文件变更" if count else None
        return "正在修改文件", detail
    if item_type == "webSearch":
        return "正在搜索资料", None
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = item.get("tool") or item.get("name") or item.get("server")
        return "正在调用工具", _progress_detail(tool)
    if item_type == "reasoning":
        return "正在分析任务", None
    if item_type == "plan":
        return "正在制定计划", None
    if item_type == "contextCompaction":
        return "正在压缩上下文", None
    if item_type == "agentMessage":
        return "正在整理回复", None
    if item_type in {"collabAgentToolCall", "collabAgentToolCallOutput"}:
        return "正在协调子任务", None
    return "正在处理任务", _progress_detail(item_type)
