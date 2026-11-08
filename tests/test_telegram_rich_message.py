"""Telegram rich-message delivery and deterministic fallback contracts."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from src.formatters import utf16_len, utf8_len
from src.notification_sender.telegram_sender import (
    RICH_MAX_BLOCKS,
    RICH_MAX_BYTES,
    TelegramSender,
    _rich_block_budget,
    _split_rich_markdown,
)


@pytest.fixture
def sender():
    return TelegramSender(SimpleNamespace(
        telegram_bot_token='BOT', telegram_chat_id='CHAT', telegram_message_thread_id='42',
    ))


def response(status=200, **body):
    return Mock(status_code=status, json=Mock(return_value=body or {'ok': True}),
                text=str(body), headers={})


@pytest.mark.parametrize('content', [
    '# 日报\n\n## 市场概览', '**买入**与__持有__',
    '[详情](https://example.com/report?q=中文&symbol=AAPL)',
    '| 股票 | 信号 |\n| --- | --- |\n| 贵州茅台 | **强势** |',
    '中文报告，关注*ST股票 (风险) 📈',
])
def test_original_markdown_is_sent_without_legacy_conversion(sender, content):
    with patch('src.notification_sender.telegram_sender.requests.post', return_value=response()) as post, \
            patch.object(sender, '_convert_to_telegram_markdown', side_effect=AssertionError('legacy conversion')):
        assert sender.send_to_telegram(content)
    post.assert_called_once_with(
        'https://api.telegram.org/botBOT/sendRichMessage',
        json={'chat_id': 'CHAT', 'message_thread_id': '42', 'rich_message': {'markdown': content}},
        timeout=10,
    )


def test_overrides_and_timeout_are_preserved(sender):
    with patch('src.notification_sender.telegram_sender.requests.post', return_value=response()) as post:
        assert sender.send_to_telegram('# 测试', chat_id='OTHER', message_thread_id='99', timeout_seconds=3)
    assert post.call_args.kwargs['json']['chat_id'] == 'OTHER'
    assert post.call_args.kwargs['json']['message_thread_id'] == '99'
    assert post.call_args.kwargs['timeout'] == 3


def test_no_topic_and_empty_content(sender):
    with patch('src.notification_sender.telegram_sender.requests.post', return_value=response()) as post:
        assert not sender.send_to_telegram(' \n ')
        post.assert_not_called()
        assert sender.send_to_telegram('hello', message_thread_id='')
    assert 'message_thread_id' not in post.call_args.kwargs['json']


@pytest.mark.parametrize('content', [
    ('# 日报\n\n**买入** [详情](https://example.com) 中文📈\n\n') * 1200,
    '中文📈' * 30000,
    ''.join(f'- 股票{i}\n' for i in range(1500)),
    ''.join(f'## 股票{i}\n\n观点 **持有**\n\n' for i in range(700)),
])
def test_long_reports_preserve_all_content_and_stay_within_limits(sender, content):
    with patch('src.notification_sender.telegram_sender.requests.post', return_value=response()) as post:
        assert sender.send_to_telegram(content)
    chunks = [call.kwargs['json']['rich_message']['markdown'] for call in post.call_args_list]
    assert len(chunks) > 1
    assert ''.join(chunks) == content.strip()
    assert all(utf8_len(chunk) <= RICH_MAX_BYTES for chunk in chunks)
    assert all(_rich_block_budget(chunk) <= RICH_MAX_BLOCKS for chunk in chunks)
    assert all(chunk.count('**') % 2 == 0 for chunk in chunks)
    assert all(call.args[0].endswith('/sendRichMessage') for call in post.call_args_list)


def test_large_table_repeats_header_and_keeps_rows():
    header = '| 股票 | 信号 |\n| --- | --- |\n'
    rows = [f'| 股票{i} | **持有** |\n' for i in range(900)]
    chunks = _split_rich_markdown(header + ''.join(rows))
    assert len(chunks) > 1
    assert all(chunk.startswith(header) for chunk in chunks)
    assert ''.join(chunk[len(header):] for chunk in chunks) == ''.join(rows)
    assert all(_rich_block_budget(chunk) <= RICH_MAX_BLOCKS for chunk in chunks)


@pytest.mark.parametrize('fence', ['```python', '~~~~python'])
def test_large_code_block_is_closed_and_reopened(fence):
    marker = fence.rstrip('python')
    body = ''.join(f'print("中文{i}")\n' for i in range(1200))
    chunks = _split_rich_markdown(f'{fence}\n{body}{marker}')
    assert len(chunks) > 1
    assert all(chunk.startswith(fence + '\n') and chunk.endswith(marker + '\n') for chunk in chunks)
    assert ''.join(chunk[len(fence) + 1:-(len(marker) + 2)] for chunk in chunks) == body


def test_long_paragraph_keeps_links_and_bold_together():
    link = '[详细报告](https://example.com/' + 'a' * 300 + ')'
    content = ('正文 ' * 5000) + link + (' **谨慎持有** ' * 5000)
    chunks = _split_rich_markdown(content)
    assert ''.join(chunks) == content
    assert sum(link in chunk for chunk in chunks) == 1
    assert all(chunk.count('**') % 2 == 0 for chunk in chunks)


@pytest.mark.parametrize('failure', [
    response(404, ok=False, description='Not Found'),
    response(400, ok=False, description='too many blocks'),
    response(200, ok=False, description='rich message rejected'),
    response(500, ok=False, description='server error'),
    response(429, ok=False, description='Too Many Requests'),
    requests.Timeout('request timed out'),
    requests.ConnectionError('connection failed'),
])
def test_api_failure_falls_back_to_legacy(sender, failure, caplog):
    with patch('src.notification_sender.telegram_sender.requests.post',
               side_effect=[failure, response()]) as post:
        assert sender.send_to_telegram('# 报告\n\n**持有**', timeout_seconds=7)
    assert post.call_count == 2
    assert post.call_args_list[0].args[0].endswith('/sendRichMessage')
    assert post.call_args_list[1].args[0].endswith('/sendMessage')
    assert post.call_args.kwargs['json']['text'] == '报告\n\n*持有*'
    assert post.call_args.kwargs['json']['parse_mode'] == 'Markdown'
    assert all(call.kwargs['json']['message_thread_id'] == '42' for call in post.call_args_list)
    assert all(call.kwargs['timeout'] == 7 for call in post.call_args_list)
    assert 'sendRichMessage' in caplog.text


def test_non_json_rich_response_falls_back(sender):
    invalid = response()
    invalid.json.side_effect = ValueError('invalid JSON')
    with patch('src.notification_sender.telegram_sender.requests.post', side_effect=[invalid, response()]) as post:
        assert sender.send_to_telegram('内容')
    assert post.call_count == 2


@pytest.mark.parametrize('body', [None, [], 'upstream error'])
def test_non_object_rich_response_falls_back(sender, body):
    invalid = response()
    invalid.json.return_value = body
    with patch('src.notification_sender.telegram_sender.requests.post', side_effect=[invalid, response()]) as post:
        assert sender.send_to_telegram('内容')
    assert post.call_count == 2


def test_rich_then_legacy_then_plain_text(sender):
    content = '# 报告\n\n**持有** *ST (中文)'
    with patch('src.notification_sender.telegram_sender.requests.post', side_effect=[
        response(404, ok=False), response(400, ok=False, description="can't parse entities"), response(),
    ]) as post:
        assert sender.send_to_telegram(content)
    assert post.call_count == 3
    plain = post.call_args.kwargs['json']
    assert 'parse_mode' not in plain
    assert plain['text'] == content
    assert plain['message_thread_id'] == '42'


def test_partial_failure_does_not_resend_successful_chunks(sender):
    content = ''.join(f'## 股票{i}\n\n观点 **持有**\n\n' for i in range(1000))
    chunks = _split_rich_markdown(content.strip())
    with patch('src.notification_sender.telegram_sender.requests.post', side_effect=[
        response(), response(404, ok=False),
    ]) as post, patch.object(sender, '_send_legacy_report', return_value=True) as legacy:
        assert sender.send_to_telegram(content)
    assert post.call_count == 2
    assert [call.args[2] for call in legacy.call_args_list] == chunks[1:]


def test_failed_chunk_does_not_prevent_remaining_chunks(sender):
    content = ''.join(f'## 股票{i}\n\n观点 **持有**\n\n' for i in range(700))
    chunks = _split_rich_markdown(content.strip())
    with patch.object(sender, '_send_rich_message', return_value=False), \
            patch.object(sender, '_send_legacy_report', side_effect=[False] + [True] * (len(chunks) - 1)) as legacy:
        assert not sender.send_to_telegram(content)
    assert legacy.call_count == len(chunks)


def test_indivisible_oversized_link_uses_length_limited_legacy(sender):
    content = '[详情](https://example.com/' + 'a' * RICH_MAX_BYTES + ')'
    with patch('src.notification_sender.telegram_sender.requests.post', return_value=response()) as post:
        assert sender.send_to_telegram(content)
    assert all(call.args[0].endswith('/sendMessage') for call in post.call_args_list)
    assert all(utf16_len(call.kwargs['json']['text']) <= 4096 for call in post.call_args_list)


def test_all_fallbacks_fail_returns_false(sender):
    with patch('src.notification_sender.telegram_sender.requests.post', side_effect=[
        response(404, ok=False), response(400, ok=False, description="can't parse entities"),
        response(400, ok=False),
    ]):
        assert not sender.send_to_telegram('内容')


@pytest.mark.parametrize('content', [
    '```' + 'language' * 5000 + '\nbody\n```',
    '[ref]: https://example.com\n\n' + '[详情][ref] ' * 5000,
    '<details>\n' + '正文' * 20000 + '\n</details>',
])
def test_unsafe_long_scoped_constructs_use_legacy_without_hanging(sender, content):
    with patch.object(sender, '_send_rich_message') as rich, \
            patch.object(sender, '_send_legacy_report', return_value=True) as legacy:
        assert sender.send_to_telegram(content)
    rich.assert_not_called()
    assert legacy.call_args.args[2] == content.strip()


def test_legacy_chunk_failure_does_not_skip_later_sections(sender):
    content = 'A' * 5000 + '\n---\n' + 'B' * 5000
    with patch.object(sender, '_send_telegram_message', side_effect=[False, True, True, True]) as send:
        assert not sender._send_telegram_chunked('url', 'CHAT', content, 4096)
    assert send.call_count == 4
    assert ''.join(call.args[2] for call in send.call_args_list) == 'A' * 5000 + 'B' * 5000
