"""HIST-02: authorized, branch-aware, bounded public conversation windows."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.privacy_service import PrivacyService
from limbowave.domain.conversation import Branch, Message, MessageRole
from limbowave.domain.permissions import PermissionPreset
from limbowave.infrastructure.database.sqlite_repositories import open_connection
from limbowave.ui.permission_prompt import ONCE
from tests.unit.test_chat_history import history_stack as history_stack

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def seed_turns(factory, count=9):
    with factory() as uow:
        for n in range(count):
            for suffix, role in [('u', MessageRole.USER), ('a', MessageRole.ASSISTANT)]:
                uow.messages.add(Message(
                    f't{n}-{suffix}', 'third', 'b-third', role, f'turn {n} {suffix}',
                    NOW + timedelta(seconds=n * 2 + (suffix == 'a')),
                    thinking='PRIVATE_THINKING',
                ))
        uow.commit()


def ids(data):
    return [r['message_id'] for r in data['results']]


def test_default_expands_two_whole_turns_each_side(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory)
    data = service.query(message_id='t4-a')
    assert ids(data) == [f't{n}-{s}' for n in range(2, 7) for s in ('u', 'a')]
    assert data['mode'] == 'context' and data['order'] == 'oldest_first'
    assert data['anchor_message_id'] == 't4-a'
    assert data['context_branch_id'] == 'b-third'
    assert data['has_more_before'] and data['has_more_after']
    assert data['count'] == 10 and not data['output_truncated']
    assert [r['message_id'] for r in data['results'] if r['is_anchor']] == ['t4-a']
    assert all(r['conversation_id'] == 'third' for r in data['results'])
    assert 'PRIVATE_THINKING' not in json.dumps(data)


@pytest.mark.parametrize('anchor', ['t4-u', 't4-a'])
def test_zero_window_still_returns_anchor_turn(history_stack, anchor):
    service, _, _, factory = history_stack
    seed_turns(factory)
    data = service.query(message_id=anchor, before_turns=0, after_turns=0)
    assert ids(data) == ['t4-u', 't4-a']


def test_three_turns_and_end_of_branch(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory, 4)
    data = service.query(message_id='t3-u', before_turns=3, after_turns=3)
    assert ids(data)[-8:] == [f't{n}-{s}' for n in range(4) for s in ('u', 'a')]
    assert not data['has_more_after']


@pytest.mark.parametrize('params', [
    {'message_id': ''}, {'message_id': '  '}, {'message_id': 'x' * 201},
    {'message_id': True}, {'message_id': 'm2', 'before_turns': True},
    {'message_id': 'm2', 'after_turns': 4}, {'message_id': 'm2', 'before_turns': -1},
    {'message_id': 'm2', 'after_turns': 1.5}, {'before_turns': 2},
    {'after_turns': 0}, {'message_id': 'm2', 'query': ''},
    {'message_id': 'm2', 'offset': 0}, {'message_id': 'm2', 'limit': 20},
])
def test_invalid_context_params_rejected_before_approval(history_stack, params):
    _, permissions, gateway, _ = history_stack
    result = gateway.invoke('query_chat_history', params, 'current', user_confirmed=True)
    assert not result.ok and not result.data.get('needs_confirmation')
    assert permissions.list_audit('current') == []


def test_missing_anchor_and_scope_mismatch_do_not_fall_back_to_search(history_stack):
    service, _, _, _ = history_stack
    for params in [
        {'message_id': 'missing'}, {'message_id': 'm2', 'conversation_id': 'third'},
        {'message_id': 'm2', 'branch_id': 'b-third'},
    ]:
        with pytest.raises(ValueError):
            service.query(**params)


@pytest.mark.parametrize('include', [False, True])
def test_fork_prefix_isolation(history_stack, include):
    service, _, _, factory = history_stack
    seed_turns(factory, 5)
    with factory() as uow:
        uow.branches.add(Branch('child', 'third', NOW + timedelta(seconds=7),
                                parent_branch_id='b-third', forked_from_message_id='t2-a',
                                include_fork_message=include))
        uow.messages.add(Message('child-u', 'third', 'child', MessageRole.USER,
                                 'child followup', NOW + timedelta(seconds=20)))
        uow.commit()
    data = service.query(message_id='child-u', before_turns=3)
    assert ('t2-a' in ids(data)) is include
    assert not {'t3-u', 't3-a', 't4-u', 't4-a'} & set(ids(data))
    inherited = service.query(message_id='t1-u', branch_id='child', after_turns=3)
    assert 'child-u' in ids(inherited) and 't3-u' not in ids(inherited)
    with pytest.raises(ValueError):
        service.query(message_id='t4-u', branch_id='child')
    if not include:
        with pytest.raises(ValueError):
            service.query(message_id='t2-a', branch_id='child')


def test_context_requires_its_own_approval_and_keeps_requester_identity(history_stack, monkeypatch):
    service, permissions, gateway, _ = history_stack
    permissions.set_preset('current', PermissionPreset.READ_ONLY)
    original = service.query
    monkeypatch.setattr(service, 'query', lambda **_: pytest.fail('read before approval'))
    pending = gateway.invoke('query_chat_history', {'message_id': 'm2'}, 'current')
    assert not pending.ok and pending.data['needs_confirmation']
    monkeypatch.setattr(service, 'query', original)
    approved = gateway.invoke('query_chat_history', {'message_id': 'm2'}, 'current',
                              user_confirmed=True)
    assert approved.ok and 'm2' in ids(approved.data)
    assert permissions.list_audit('current') and not permissions.list_audit('other')
    assert gateway.invoke('query_chat_history', {'message_id': 'm2'}, 'current').data[
        'needs_confirmation']


async def test_context_gate_and_origin_change(history_stack, monkeypatch):
    _, permissions, gateway, _ = history_stack
    ask = AsyncMock(return_value=ONCE)
    monkeypatch.setattr('limbowave.app._ask_permission', ask)
    handler = _make_permission_handler(None, permissions, lambda: 'current', gateway)
    detail = json.dumps({'tool': 'query_chat_history', 'input': {'message_id': 'm2'}})
    assert await handler('limbowave.gate:query_chat_history', detail)
    ask.assert_awaited_once()
    gateway.origin_provider = lambda: 'web'
    assert not gateway.invoke('query_chat_history', {'message_id': 'm2'}, 'current',
                              user_confirmed=True).ok


def test_excluded_anchor_and_fresh_start_fail_closed(history_stack):
    service, _, _, factory = history_stack
    privacy = PrivacyService(factory)
    privacy.add('Docker')
    with pytest.raises(ValueError):
        service.query(message_id='m2', requester_conversation_id='current')
    with factory() as uow:
        privacy.mark_fresh(uow, 'current')
        uow.commit()
    with pytest.raises(ValueError):
        service.query(message_id='m4', requester_conversation_id='current')
    assert ids(service.query(message_id='m1', requester_conversation_id='current')) == ['m1']


def test_context_byte_budget_preserves_anchor_and_flags_truncation(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory)
    with factory() as uow:
        for n in range(9):
            m = uow.messages.get(f't{n}-a')
            uow.messages.update(replace(m, content='长' * 6000))
        uow.commit()
    data = service.query(message_id='t4-a', max_output_bytes=1800)
    assert 't4-a' in ids(data) and data['output_truncated']
    assert len(json.dumps(data, ensure_ascii=False).encode('utf-8')) <= 1800
    assert next(r for r in data['results'] if r['is_anchor'])['content_truncated']
    with pytest.raises(ValueError):
        service.query(message_id='t4-a', max_output_bytes=10)


def test_context_never_decrypts_thinking_tools_or_segments(history_stack):
    service, _, _, factory = history_stack
    if not hasattr(factory, '_path'):
        pytest.skip('SQLite ciphertext projection test')
    with closing(open_connection(factory._path)) as conn, conn:
        conn.execute('UPDATE messages SET thinking_enc = ?, tool_steps_enc = ?, segments_enc = ?',
                     ('INVALID', 'INVALID', 'INVALID'))
        conn.execute("UPDATE messages SET content_enc = ? WHERE id IN ('m1', 'm3', 'm4')",
                     ('INVALID',))
    assert 'm2' in ids(service.query(message_id='m2'))


def test_private_user_turn_cannot_leak_via_newer_assistant_paraphrase(history_stack):
    service, _, _, factory = history_stack
    rule = PrivacyService(factory).add('blocked topic')
    now = rule.created_at + timedelta(seconds=10)
    with factory() as uow:
        for mid, role, text in [
            ('safe-u', MessageRole.USER, 'safe question'),
            ('safe-a', MessageRole.ASSISTANT, 'safe answer'),
            ('private-u', MessageRole.USER, 'blocked topic'),
            ('private-a', MessageRole.ASSISTANT, 'opaque paraphrase'),
            ('next-u', MessageRole.USER, 'next question'),
            ('next-a', MessageRole.ASSISTANT, 'next answer'),
        ]:
            now += timedelta(seconds=1)
            uow.messages.add(Message(mid, 'third', 'b-third', role, text, now, run_id='r'))
        uow.commit()
    result = service.query(message_id='safe-a', after_turns=2)
    assert 'next-a' in ids(result) and result['privacy_filtered']
    assert not {'private-u', 'private-a'} & set(ids(result))
    assert all(r['conversation_title'] == '历史会话' for r in result['results'])
    with pytest.raises(ValueError):
        service.query(message_id='private-a', before_turns=0, after_turns=0)


def test_equal_timestamps_are_stably_ordered_and_consecutive_users_are_turns(history_stack):
    service, _, _, factory = history_stack
    with factory() as uow:
        for n in (4, 2, 1, 3, 0):
            uow.messages.add(Message(f'eq-{n}', 'other', 'b-other', MessageRole.USER,
                                     f'message {n}', NOW))
        uow.commit()
    data = service.query(message_id='eq-2', before_turns=1, after_turns=1)
    assert ids(data) == ['eq-1', 'eq-2', 'eq-3']


def test_nested_fork_inherited_anchor_and_sibling_isolation(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory, 5)
    with factory() as uow:
        for bid, parent, anchor in [
            ('child', 'b-third', 't3-a'), ('sibling', 'b-third', 't3-a'),
            ('nested', 'child', 't1-a'),
        ]:
            uow.branches.add(Branch(bid, 'third', NOW + timedelta(seconds=40),
                                    parent_branch_id=parent, forked_from_message_id=anchor,
                                    include_fork_message=True))
            uow.messages.add(Message(f'{bid}-u', 'third', bid, MessageRole.USER,
                                     f'{bid} answer', NOW + timedelta(seconds=50)))
        uow.commit()
    data = service.query(message_id='t1-a', branch_id='nested', after_turns=3)
    assert ids(data)[-1] == 'nested-u'
    assert not {'child-u', 'sibling-u', 't2-u', 't3-u', 't4-u'} & set(ids(data))
    with pytest.raises(ValueError):
        service.query(message_id='child-u', branch_id='nested')


def test_deleted_anchor_does_not_retarget_a_neighbor(history_stack):
    service, _, _, factory = history_stack
    assert 'm2' in ids(service.query(message_id='m2'))
    with factory() as uow:
        uow.conversations.delete('other')
        uow.commit()
    with pytest.raises(ValueError):
        service.query(message_id='m2')


def test_malformed_branch_cycle_fails_closed(history_stack):
    service, _, _, factory = history_stack
    with factory() as uow:
        uow.branches.add(Branch('cycle', 'other', NOW, parent_branch_id='cycle'))
        uow.messages.add(Message('cycle-u', 'other', 'cycle', MessageRole.USER, 'cycle', NOW))
        uow.commit()
    with pytest.raises(ValueError):
        service.query(message_id='cycle-u')


def test_reads_are_bounded_for_oversized_turns(history_stack, monkeypatch):
    from limbowave.application.services.chat_history_context import MAX_CONTEXT_SIDE_MESSAGES

    service, _, _, factory = history_stack
    with factory() as uow:
        repository_type = type(uow.messages)
        for n in range(300):
            uow.messages.add(Message(f'long-{n:03}', 'third', 'b-third', MessageRole.ASSISTANT,
                                     'reply', NOW + timedelta(seconds=n), run_id='r'))
        uow.commit()
    original = repository_type.page_chat_history
    reads = []

    def record(self, *args, **kwargs):
        rows = original(self, *args, **kwargs)
        reads.append(len(rows))
        return rows

    monkeypatch.setattr(repository_type, 'page_chat_history', record)
    data = service.query(message_id='long-150', before_turns=0, after_turns=0)
    assert data['window_truncated'] and 'long-150' in ids(data)
    assert data['count'] <= 2 * MAX_CONTEXT_SIDE_MESSAGES
    assert sum(reads) <= 2 * MAX_CONTEXT_SIDE_MESSAGES + 16
    assert max(reads) <= 8
    PrivacyService(factory).add('unrelated boundary')
    # The owning user is outside the hard read bound: provenance is unknown.
    with pytest.raises(ValueError):
        service.query(message_id='long-150', before_turns=0, after_turns=0)


def test_anchor_only_assistant_prefix_and_end_flags(history_stack):
    service, _, _, factory = history_stack
    with factory() as uow:
        uow.branches.add(Branch('assistant-only', 'third', NOW))
        uow.messages.add(Message('a-only', 'third', 'assistant-only', MessageRole.ASSISTANT,
                                 'greeting', NOW))
        uow.commit()
    data = service.query(message_id='a-only')
    assert ids(data) == ['a-only']
    assert not data['has_more_before'] and not data['has_more_after']


async def test_invalid_window_never_opens_permission_prompt(history_stack, monkeypatch):
    _, permissions, gateway, _ = history_stack
    ask = AsyncMock(side_effect=AssertionError('invalid range must not prompt'))
    monkeypatch.setattr('limbowave.app._ask_permission', ask)
    handler = _make_permission_handler(None, permissions, lambda: 'current', gateway)
    assert not await handler('limbowave.gate:query_chat_history', json.dumps({
        'tool': 'query_chat_history', 'input': {'message_id': 'm2', 'after_turns': 100},
    }))
    ask.assert_not_called()
    assert permissions.list_audit('current') == []


def test_approval_shows_explicit_default_window_even_with_long_scope_params(history_stack):
    from limbowave.ui.permission_prompt import describe_request

    _, _, gateway, _ = history_stack
    request = gateway.prepare_request('query_chat_history', {
        'conversation_id': 'c' * 200, 'branch_id': 'b' * 200, 'message_id': 'm2',
    })
    assert request.raw_params['before_turns'] == 2
    assert request.raw_params['after_turns'] == 2
    _, description = describe_request(request, 'confirm', None)
    assert '历史锚点："m2"' in description
    assert '前 2 轮 + 锚点所在轮 + 后 2 轮' in description


def test_multiple_assistant_replies_belong_to_one_turn(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory, 4)
    with factory() as uow:
        uow.messages.add(Message('t1-extra', 'third', 'b-third', MessageRole.ASSISTANT,
                                 'extra reply', NOW + timedelta(seconds=3, microseconds=1)))
        uow.commit()
    assert ids(service.query(message_id='t1-a', before_turns=0, after_turns=0)) == [
        't1-u', 't1-a', 't1-extra',
    ]
    assert ids(service.query(message_id='t1-extra', before_turns=1, after_turns=1)) == [
        't0-u', 't0-a', 't1-u', 't1-a', 't1-extra', 't2-u', 't2-a',
    ]


def test_keyset_expansion_does_not_drift_with_newer_search_hits(history_stack):
    service, _, _, factory = history_stack
    seed_turns(factory)
    before = ids(service.query(message_id='t4-a', before_turns=1, after_turns=1))
    with factory() as uow:
        uow.messages.add(Message('newest', 'third', 'b-third', MessageRole.USER,
                                 'new topic', NOW + timedelta(days=1)))
        uow.commit()
    assert ids(service.query(message_id='t4-a', before_turns=1, after_turns=1)) == before


def test_permission_preview_escapes_anchor_control_characters(history_stack):
    from limbowave.ui.permission_prompt import describe_request

    _, _, gateway, _ = history_stack
    request = gateway.prepare_request('query_chat_history', {'message_id': 'm2\nforged scope'})
    _, description = describe_request(request, 'confirm', None)
    assert 'm2\nforged scope' not in description
    assert 'm2\\nforged scope' in description
