from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

LOG = logging.getLogger(__name__)
RICH_MARKDOWN_LIMIT = 30_000


class TelegramError(RuntimeError):
    pass


def split_text(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if not text:
        return ["（空消息）"]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def split_rich_markdown(
    text: str, limit: int = RICH_MARKDOWN_LIMIT
) -> list[str]:
    """Split Markdown on block boundaries while keeping fenced code valid."""
    text = text.strip()
    if not text:
        return ["（空消息）"]
    if limit < 32:
        raise ValueError("Markdown chunk limit must be at least 32")

    blocks = _markdown_blocks(text)
    pieces: list[str] = []
    for block in blocks:
        pieces.extend(_split_markdown_block(block, limit))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current}\n\n{piece}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


def _markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
        if not line.strip() and fence is None:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return blocks


def _split_markdown_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    lines = block.splitlines()
    opening = re.match(r"^\s*(`{3,}|~{3,})[^\n]*$", lines[0]) if lines else None
    if opening and len(lines) >= 2:
        marker = opening.group(1)
        closing = lines[-1].strip()
        if closing.startswith(marker):
            opener = lines[0]
            closer = lines[-1]
            allowance = limit - len(opener) - len(closer) - 2
            if allowance >= 1:
                content = "\n".join(lines[1:-1])
                return [
                    f"{opener}\n{piece}\n{closer}"
                    for piece in _split_preserving(content, allowance)
                ]
    return split_text(block, limit=limit)


def _split_preserving(text: str, limit: int) -> list[str]:
    if not text:
        return [""]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
            chunks.append(text[:cut])
            text = text[cut:]
        else:
            chunks.append(text[:cut])
            text = text[cut + 1 :]
    chunks.append(text)
    return chunks


def markdown_to_telegram_html(markdown: str) -> str:
    """Conservative HTML fallback for common Agent Markdown constructs."""
    output: list[str] = []
    code_lines: list[str] = []
    code_language = ""
    fence: str | None = None
    for line in markdown.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0]
                code_language = match.group(2)
                code_lines = []
                continue
            if marker[0] == fence:
                language_attr = (
                    f' class="language-{html.escape(code_language, quote=True)}"'
                    if code_language
                    else ""
                )
                code = html.escape("\n".join(code_lines), quote=False)
                output.append(f"<pre><code{language_attr}>{code}</code></pre>")
                fence = None
                code_language = ""
                code_lines = []
                continue
        if fence is not None:
            code_lines.append(line)
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            output.append(f"<b>{_inline_markdown_html(heading.group(1))}</b>")
        elif line.startswith("> "):
            output.append(
                f"<blockquote>{_inline_markdown_html(line[2:])}</blockquote>"
            )
        elif re.match(r"^\s*[-*+]\s+", line):
            item = re.sub(r"^\s*[-*+]\s+", "", line)
            output.append(f"• {_inline_markdown_html(item)}")
        else:
            output.append(_inline_markdown_html(line))
    if fence is not None:
        code = html.escape("\n".join(code_lines), quote=False)
        output.append(f"<pre>{code}</pre>")
    return "\n".join(output)


def _inline_markdown_html(text: str) -> str:
    output: list[str] = []
    index = 0
    markers = (("**", "b"), ("__", "b"), ("~~", "s"), ("||", "tg-spoiler"))
    while index < len(text):
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end != -1:
                output.append(
                    f"<code>{html.escape(text[index + 1:end], quote=False)}</code>"
                )
                index = end + 1
                continue
        if text[index] == "[":
            label_end = text.find("](", index + 1)
            url_end = text.find(")", label_end + 2) if label_end != -1 else -1
            if label_end != -1 and url_end != -1:
                label = text[index + 1 : label_end]
                url = text[label_end + 2 : url_end]
                if url.startswith(("https://", "http://", "mailto:", "tel:")):
                    output.append(
                        f'<a href="{html.escape(url, quote=True)}">'
                        f"{html.escape(label, quote=False)}</a>"
                    )
                    index = url_end + 1
                    continue
        matched = False
        for marker, tag in markers:
            if text.startswith(marker, index):
                end = text.find(marker, index + len(marker))
                if end != -1:
                    content = _inline_markdown_html(
                        text[index + len(marker) : end]
                    )
                    output.append(f"<{tag}>{content}</{tag}>")
                    index = end + len(marker)
                    matched = True
                    break
        if matched:
            continue
        output.append(html.escape(text[index], quote=False))
        index += 1
    return "".join(output)


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        return await asyncio.to_thread(
            self._call_sync, method, payload or {}, timeout
        )

    def _call_sync(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise TelegramError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Telegram connection failed: {exc.reason}") from exc
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or "Telegram API failed"))
        return data.get("result")

    async def get_updates(self, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=poll_timeout + 15,
        )
        return list(result or [])

    async def configure_command_menu(
        self, commands: list[dict[str, str]]
    ) -> None:
        await self.call(
            "setMyCommands",
            {
                "commands": commands,
                "scope": {"type": "all_private_chats"},
            },
        )
        await self.call(
            "setChatMenuButton",
            {"menu_button": {"type": "commands"}},
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_text(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            await self.call(
                "sendMessage",
                payload,
            )

    async def send_rich_markdown(self, chat_id: int, markdown: str) -> None:
        for chunk in split_rich_markdown(markdown):
            try:
                await self.call(
                    "sendRichMessage",
                    {
                        "chat_id": chat_id,
                        "rich_message": {"markdown": chunk},
                    },
                )
            except TelegramError:
                LOG.warning(
                    "Rich Markdown send failed; falling back to HTML/plain text",
                    exc_info=True,
                )
                await self._send_markdown_fallback(chat_id, chunk)

    async def _send_markdown_fallback(
        self, chat_id: int, markdown: str
    ) -> None:
        for chunk in split_rich_markdown(markdown, limit=3000):
            html_text = markdown_to_telegram_html(chunk)
            if len(html_text) <= 3900:
                try:
                    await self.call(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": html_text,
                            "parse_mode": "HTML",
                            "link_preview_options": {"is_disabled": True},
                        },
                    )
                    continue
                except TelegramError:
                    LOG.warning(
                        "HTML fallback failed; sending plain text",
                        exc_info=True,
                    )
            await self.send_message(chat_id, chunk)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        await self.call("answerCallbackQuery", payload)

    async def send_typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
