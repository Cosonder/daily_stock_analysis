"""Temporary live Telegram verification; used only on the verification branch."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from src.notification_sender.telegram_sender import TelegramSender, _split_rich_markdown


def main():
    sender = TelegramSender(SimpleNamespace(
        telegram_bot_token=os.environ['TELEGRAM_BOT_TOKEN'],
        telegram_chat_id=os.environ['TELEGRAM_CHAT_ID'],
        telegram_message_thread_id=os.getenv('TELEGRAM_MESSAGE_THREAD_ID') or None,
    ))
    real_post = requests.post
    evidence = []

    def observed_post(url, **kwargs):
        # This is an actual network request; retain only safe result metadata.
        try:
            result = real_post(url, **kwargs)
            body = result.json()
        except Exception as exc:
            print('::error::Telegram request exception: ' + type(exc).__name__)
            raise SystemExit(1)
        message = body.get('result') or {}
        rich = message.get('rich_message') or {}
        entry = {
            'method': url.rsplit('/', 1)[-1],
            'http_status': result.status_code,
            'ok': body.get('ok', False),
            'rich_message_returned': bool(rich),
            'top_level_blocks': len(rich.get('blocks', [])),
        }
        topic = os.getenv('TELEGRAM_MESSAGE_THREAD_ID')
        if topic:
            entry['topic_matches'] = str(message.get('message_thread_id')) == str(topic)
        evidence.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
        return result

    requests.post = observed_post
    short = (
        '# Telegram 富文本验证\n\n'
        '## 标题、粗体、链接、表格与中文\n\n'
        '**这是一条验证消息，不是投资建议。**\n\n'
        '[查看修复 PR](https://github.com/Cosonder/daily_stock_analysis/pull/1)\n\n'
        '| 验证项 | 预期 |\n| --- | --- |\n'
        '| 中文与 emoji | 正常显示 📈 |\n| Markdown 粗体 | **清晰显示** |'
    )
    long = (
        '# Telegram 富文本长报告验证\n\n'
        '**以下为合成数据，仅用于验证长表格分段。**\n\n'
        '| 序号 | 中文验证内容 | 链接 |\n| --- | --- | --- |\n'
        + ''.join(
            f'| {i:03d} | **中文分段验证** 📈 合成数据，非真实股票分析 | '
            '[测试链接](https://example.com/report) |\n'
            for i in range(1, 601)
        )
    )
    chunks = _split_rich_markdown(long)
    assert len(long.encode('utf-8')) > 32768 and len(chunks) > 1
    print('Expected short requests: 1; expected long requests:', len(chunks))
    assert sender.send_to_telegram(short, timeout_seconds=20), 'Short report delivery failed'
    time.sleep(4)
    assert sender.send_to_telegram(long, timeout_seconds=20), 'Long report delivery failed'
    assert len(evidence) == 1 + len(chunks), 'Unexpected retries or fallback requests'
    assert all(item['method'] == 'sendRichMessage' and item['ok']
               and item['rich_message_returned'] and item.get('topic_matches', True)
               for item in evidence), 'Rich API, formatting result or Topic verification failed'
    print('PASS: short and long reports accepted as rich messages; no legacy/plain-text fallback.')
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as handle:
            handle.write('## Telegram live verification\n\n')
            handle.write(f'PASS: {len(evidence)} real sendRichMessage requests, including a 600-row table.\n\n')
            handle.write('All returned ok=true and rich_message; no legacy/plain-text fallback. '
                         'Configured Topic matched when present. Client-side visual review remains manual.\n')


if __name__ == '__main__':
    main()
