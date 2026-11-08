# -*- coding: utf-8 -*-
"""
Telegram 发送提醒服务

职责：
1. 通过 Telegram Bot API 发送 文本消息
2. 通过 Telegram Bot API 发送 图片消息
"""
import logging
from typing import Optional
import requests
import time
import re

from src.config import Config
from src.formatters import _custom_unit_to_index, strip_hidden_markdown_metadata, utf16_len, utf8_len


logger = logging.getLogger(__name__)

# https://core.telegram.org/bots/api#rich-message-limits
# Bound the Markdown source in UTF-8 bytes, conservatively below the rendered
# 32768-character limit, including for Chinese and emoji.
RICH_MAX_BYTES = 32768
RICH_MAX_BLOCKS = 500


def _rich_block_budget(text: str) -> int:
    """Conservative source-based bound, including nested lists/quotes and rows.

    Counting two blocks per nonempty source line overcounts ordinary paragraphs
    and table rows, leaving room for their containers. Explicit HTML tags and
    nesting markers get additional room; the server remains authoritative.
    """
    return sum(
        2 + line.count('>')
        + (len(line) - len(line.lstrip())) // 2
        + len(re.findall(r'<[a-zA-Z][^>]*>', line))
        for line in text.splitlines() if line.strip()
    )


def _split_rich_markdown(content: str) -> list[str]:
    """Keep paragraphs/inline formatting intact, repeat table headers and fences.

    An indivisible oversized inline construct is rejected before sending any
    chunks so that the caller can deliver it through the legacy fallback.
    """
    def fits(text: str) -> bool:
        return utf8_len(text) <= RICH_MAX_BYTES and _rich_block_budget(text) <= RICH_MAX_BLOCKS

    if fits(content):
        return [content]
    # These constructs can span distant source blocks. Let the existing
    # delivery path handle them instead of breaking HTML or reference scopes.
    if re.search(r'^\s*\[[^\]]+\]:\s+\S|<\s*/?[a-zA-Z][^>]*>', content, re.MULTILINE):
        raise ValueError('long report contains scoped HTML or reference definitions')

    def pack(parts: list[str], prefix: str = '', suffix: str = '') -> list[str]:
        result = []
        current = ''
        for part in parts:
            if not fits(prefix + part + suffix):
                raise ValueError('indivisible Markdown block exceeds rich message limits')
            if current and not fits(prefix + current + part + suffix):
                result.append(prefix + current + suffix)
                current = ''
            current += part
        if current:
            result.append(prefix + current + suffix)
        return result

    lines = content.splitlines(keepends=True)
    blocks = []
    index = 0
    while index < len(lines):
        start = index
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})[^\n]*', lines[index])
        if fence:
            marker = fence.group(1)
            index += 1
            while index < len(lines):
                closing = re.match(r'^ {0,3}' + re.escape(marker[0]) + r'{' + str(len(marker)) + r',}\s*$', lines[index])
                index += 1
                if closing:
                    break
            else:
                raise ValueError('unclosed Markdown fence')
            block = ''.join(lines[start:index])
            if fits(block):
                blocks.append(block)
            else:
                # Reopen each code chunk with its original language and fence.
                if not fits(lines[start] + '\n' + marker + '\n'):
                    raise ValueError('Markdown fence exceeds rich message limits')
                body = lines[start + 1:index - 1]
                pieces = []
                for line in body:
                    while utf8_len(line) > RICH_MAX_BYTES - utf8_len(lines[start] + '\n' + marker):
                        cut = _custom_unit_to_index(line, RICH_MAX_BYTES // 2, utf8_len)
                        pieces.append(line[:cut] + '\n')
                        line = line[cut:]
                    pieces.append(line)
                blocks.extend(pack(pieces, lines[start], '\n' + marker + '\n'))
            continue

        # GFM pipe table: split only between rows, retaining a complete header.
        if index + 1 < len(lines) and '|' in lines[index] and re.fullmatch(
            r'\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*', lines[index + 1]
        ):
            index += 2
            while index < len(lines) and lines[index].strip() and '|' in lines[index]:
                index += 1
            block = ''.join(lines[start:index])
            if fits(block):
                blocks.append(block)
            else:
                blocks.extend(pack(lines[start + 2:index], ''.join(lines[start:start + 2])))
            continue

        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(
            r'^\s*(?:#{1,6}\s|`{3,}|~{3,}|\|)', lines[index]
        ):
            index += 1
        block = ''.join(lines[start:index])
        if fits(block):
            blocks.append(block)
            continue
        # Protect complete inline constructs. Plain words can be cut at Unicode
        # boundaries; an oversized link/code/emphasis goes to legacy delivery.
        inline = (
            r'(`+)[\s\S]*?\1|\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)|'
            r'\*\*[\s\S]*?\*\*|__[\s\S]*?__|\*[^*]+\*|_[^_]+_|~~[\s\S]*?~~'
        )
        tokens = []
        pos = 0
        for match in re.finditer(inline, block):
            tokens.extend(block[pos:match.start()])
            tokens.append(match.group())
            pos = match.end()
        tokens.extend(block[pos:])
        # Aggregate characters before pack() to avoid repeatedly measuring a
        # whole growing paragraph for every character.
        fragments = []
        current = ''
        current_bytes = 0
        for token in tokens:
            token_bytes = utf8_len(token)
            if current and (current_bytes + token_bytes > 8192 or token == '\n'):
                fragments.append(current)
                current = ''
                current_bytes = 0
            current += token
            current_bytes += token_bytes
        if current:
            fragments.append(current)
        blocks.extend(pack(fragments))
    return pack(blocks)


class TelegramSender:

    def __init__(self, config: Config):
        """
        初始化 Telegram 配置

        Args:
            config: 配置对象
        """
        self._telegram_config = {
            'bot_token': getattr(config, 'telegram_bot_token', None),
            'chat_id': getattr(config, 'telegram_chat_id', None),
            'message_thread_id': getattr(config, 'telegram_message_thread_id', None),
        }

    def _is_telegram_configured(self) -> bool:
        """检查 Telegram 配置是否完整"""
        return bool(self._telegram_config['bot_token'] and self._telegram_config['chat_id'])

    def send_to_telegram(
        self,
        content: str,
        *,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Telegram 机器人

        优先以原始 Markdown 调用 sendRichMessage；逐块失败后回退旧版
        sendMessage，Markdown 解析失败时最后回退纯文本。

        Args:
            content: 消息内容（Markdown 格式）

        Returns:
            是否发送成功
        """
        target_chat_id = chat_id if chat_id is not None else self._telegram_config.get("chat_id")
        target_message_thread_id = (
            message_thread_id
            if message_thread_id is not None
            else self._telegram_config.get("message_thread_id")
        )

        if not (self._telegram_config["bot_token"] and target_chat_id):
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        bot_token = self._telegram_config['bot_token']
        chat_id = target_chat_id
        message_thread_id = target_message_thread_id

        try:
            # Telegram API 端点
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            # Telegram 消息最大长度 4096（按 UTF-16 code unit 计）
            max_length = 4096

            sanitized_content = strip_hidden_markdown_metadata(content).strip()
            if not sanitized_content:
                logger.warning("Telegram 消息内容为空，跳过推送")
                return False

            try:
                chunks = _split_rich_markdown(sanitized_content)
            except ValueError as exc:
                logger.error("Telegram 富文本分段失败，回退 sendMessage: %s", exc)
                chunks = [sanitized_content]
                rich_enabled = False
            else:
                rich_enabled = True

            all_success = True
            for chunk in chunks:
                if rich_enabled and self._send_rich_message(
                    api_url.replace('/sendMessage', '/sendRichMessage'),
                    chat_id, chunk, message_thread_id, timeout_seconds=timeout_seconds,
                ):
                    continue
                # Only retry the failed chunk, never resend rich chunks already
                # delivered. Stop probing an unavailable rich API for this report.
                rich_enabled = False
                if not self._send_legacy_report(
                    api_url, chat_id, chunk, max_length, message_thread_id,
                    timeout_seconds=timeout_seconds,
                ):
                    all_success = False
            return all_success

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    def _send_rich_message(
        self, api_url: str, chat_id: str, text: str,
        message_thread_id: Optional[str] = None, *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        payload = {'chat_id': chat_id, 'rich_message': {'markdown': text}}
        if message_thread_id:
            payload['message_thread_id'] = message_thread_id
        try:
            response = requests.post(api_url, json=payload, timeout=timeout_seconds or 10)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError('Telegram response is not an object')
            if response.status_code == 200 and result.get('ok'):
                logger.info('Telegram 富文本消息发送成功')
                return True
            logger.error('Telegram sendRichMessage 失败: HTTP %s, %s；回退 sendMessage',
                         response.status_code, result.get('description', '未知错误'))
        except (requests.RequestException, ValueError) as exc:
            # Do not log request exceptions: their URLs may contain the bot token.
            logger.error('Telegram sendRichMessage 异常: %s；回退 sendMessage', type(exc).__name__)
        return False

    def _send_legacy_report(
        self, api_url: str, chat_id: str, sanitized_content: str, max_length: int,
        message_thread_id: Optional[str] = None, *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        telegram_content = self._convert_to_telegram_markdown(sanitized_content)

        if utf16_len(telegram_content) <= max_length:
            # 单条消息发送
            return self._send_telegram_message(
                api_url,
                chat_id,
                sanitized_content,
                message_thread_id,
                timeout_seconds=timeout_seconds,
            )
        else:
            # 按 Markdown 转义后的最终 payload 分段，避免转义字符使请求超限
            return self._send_telegram_chunked(
                api_url,
                chat_id,
                telegram_content,
                max_length,
                message_thread_id,
                timeout_seconds=timeout_seconds,
            )

    def _send_telegram_message(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
        markdown_converted: bool = False,
    ) -> bool:
        """Send a single Telegram message with exponential backoff retry (Fixes #287)"""
        # Convert Markdown to Telegram-compatible format
        telegram_text = text if markdown_converted else self._convert_to_telegram_markdown(text)

        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        if message_thread_id:
            payload['message_thread_id'] = message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(api_url, json=payload, timeout=timeout_seconds or 10)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    delay = 2 ** attempt  # 2s, 4s
                    logger.warning(f"Telegram request failed (attempt {attempt}/{max_retries}): {e}, "
                                   f"retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"Telegram request failed after {max_retries} attempts: {e}")
                    return False

            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    logger.info("Telegram 消息发送成功")
                    return True
                else:
                    error_desc = result.get('description', '未知错误')
                    logger.error(f"Telegram 返回错误: {error_desc}")

                    # If Markdown parsing failed, fall back to plain text
                    if self._should_fallback_to_plain_text(error_desc=error_desc):
                        if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                            return True

                    return False
            elif response.status_code == 429:
                # Rate limited — respect Retry-After header
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                if attempt < max_retries:
                    logger.warning(f"Telegram rate limited, retrying in {retry_after}s "
                                   f"(attempt {attempt}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Telegram rate limited after {max_retries} attempts")
                    return False
            else:
                if attempt < max_retries and response.status_code >= 500:
                    delay = 2 ** attempt
                    logger.warning(f"Telegram server error HTTP {response.status_code} "
                                   f"(attempt {attempt}/{max_retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if self._should_fallback_to_plain_text(response_text=response.text):
                    if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                        return True
                logger.error(f"Telegram 请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        return False

    @staticmethod
    def _should_fallback_to_plain_text(error_desc: str = "", response_text: str = "") -> bool:
        """Detect Telegram Markdown parsing failures that should retry as plain text."""
        haystack = f"{error_desc}\n{response_text}".lower()
        markers = (
            "can't parse entities",
            "can't parse entity",
            "can't find end of the entity",
            "parse entities",
            "parse_mode",
            "markdown",
        )
        return any(marker in haystack for marker in markers)

    def _send_plain_text_fallback(
        self,
        api_url: str,
        payload: dict,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Retry Telegram send without parse_mode when Markdown parsing fails."""
        logger.info("Telegram Markdown 解析失败，尝试使用纯文本格式重新发送...")
        plain_payload = dict(payload)
        plain_payload.pop('parse_mode', None)
        plain_payload['text'] = text

        try:
            response = requests.post(api_url, json=plain_payload, timeout=timeout_seconds or 10)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.error(f"Telegram plain-text fallback failed: {e}")
            return False

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                logger.error("Telegram 纯文本回退失败: 响应不是有效 JSON")
                logger.error(f"响应内容: {response.text}")
                return False

            if result.get('ok'):
                logger.info("Telegram 消息发送成功（纯文本）")
                return True

            logger.error("Telegram 纯文本回退失败: Telegram API 返回 ok=false")
            logger.error(f"响应内容: {response.text}")
            return False

        logger.error(f"Telegram 纯文本回退失败: HTTP {response.status_code}")
        logger.error(f"响应内容: {response.text}")
        return False

    def _send_telegram_chunked(
        self,
        api_url: str,
        chat_id: str,
        content: str,
        max_length: int,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """按已转换的 Telegram Markdown payload 分段发送长消息。"""
        # 按段落分割
        sections = content.split("\n---\n")
        delimiter = "\n---\n"
        delimiter_length = len(delimiter)

        current_chunk = []
        current_length = 0
        all_success = True
        chunk_index = 1

        def _flush_chunk() -> bool:
            nonlocal current_chunk, current_length, chunk_index, all_success
            if not current_chunk:
                return all_success

            chunk_content = "\n---\n".join(current_chunk)
            logger.info(f"发送 Telegram 消息块 {chunk_index}...")
            chunk_index += 1
            current_chunk = []
            current_length = 0
            if not self._send_telegram_message(
                api_url,
                chat_id,
                chunk_content,
                message_thread_id,
                timeout_seconds=timeout_seconds,
                markdown_converted=True,
            ):
                all_success = False
            return all_success

        def _split_long_section(section: str, limit: int) -> list[str]:
            chunks: list[str] = []
            while utf16_len(section) > limit:
                cut = max(1, _custom_unit_to_index(section, limit, utf16_len))
                chunks.append(section[:cut])
                section = section[cut:]
            chunks.append(section)
            return chunks

        for section in sections:
            if utf16_len(section) > max_length:
                # 单段超限时强制切片，避免依赖“\\n---\\n”边界导致的整段超长发送
                _flush_chunk()
                for long_chunk in _split_long_section(section, max_length):
                    logger.info(f"发送 Telegram 消息块 {chunk_index}...")
                    chunk_index += 1
                    if not self._send_telegram_message(
                        api_url,
                        chat_id,
                        long_chunk,
                        message_thread_id,
                        timeout_seconds=timeout_seconds,
                        markdown_converted=True,
                    ):
                        all_success = False
                continue

            additional_length = utf16_len(section)
            if current_chunk:
                additional_length += delimiter_length

            if current_length + additional_length > max_length:
                _flush_chunk()
                current_chunk = [section]
                current_length = utf16_len(section)
                continue

            current_chunk.append(section)
            current_length += additional_length

        # 发送最后一块
        _flush_chunk()
        return all_success

    def _send_telegram_photo(self, image_bytes: bytes) -> bool:
        """Send image via Telegram sendPhoto API (Issue #289)."""
        if not self._is_telegram_configured():
            return False
        bot_token = self._telegram_config['bot_token']
        chat_id = self._telegram_config['chat_id']
        message_thread_id = self._telegram_config.get('message_thread_id')
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            data = {"chat_id": chat_id}
            if message_thread_id:
                data['message_thread_id'] = message_thread_id
            files = {"photo": ("report.png", image_bytes, "image/png")}
            response = requests.post(api_url, data=data, files=files, timeout=30)
            if response.status_code == 200 and response.json().get('ok'):
                logger.info("Telegram 图片发送成功")
                return True
            logger.error("Telegram 图片发送失败: %s", response.text[:200])
            return False
        except Exception as e:
            logger.error("Telegram 图片发送异常: %s", e)
            return False

    def _convert_to_telegram_markdown(self, text: str) -> str:
        """
        将标准 Markdown 转换为 Telegram 支持的格式

        Telegram Markdown 限制：
        - 不支持 # 标题
        - 使用 *bold* 而非 **bold**
        - 使用 _italic_
        """
        result = strip_hidden_markdown_metadata(text)

        # 移除 # 标题标记（Telegram 不支持）
        result = re.sub(r'^#{1,6}\s+', '', result, flags=re.MULTILINE)

        # 转换 **bold** 为 *bold*
        result = re.sub(r'\*\*(.+?)\*\*', r'*\1*', result)

        # Escape special characters for Telegram Markdown, but preserve link syntax [text](url)
        # Step 1: temporarily protect markdown links
        import uuid as _uuid
        _link_placeholder = f"__LINK_{_uuid.uuid4().hex[:8]}__"
        _links = []
        def _save_link(m):
            _links.append(m.group(0))
            return f"{_link_placeholder}{len(_links) - 1}"
        result = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _save_link, result)

        # Step 2: escape remaining special chars
        for char in ['[', ']', '(', ')']:
            result = result.replace(char, f'\\{char}')

        # Step 3: restore links
        for i, link in enumerate(_links):
            result = result.replace(f"{_link_placeholder}{i}", link)

        return result
