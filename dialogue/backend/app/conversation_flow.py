"""Rolling dialogue context and evidence for naturalness review."""
from __future__ import annotations

import re


STAGES = ['寒暄暖场', '了解群用途', '自然群聊']


def rolling_context(run: dict, history: list[dict]) -> dict:
    states = {}
    for role, session in run['agent_sessions'].items():
        own = [m for m in history if m['sender'] == role]
        addressed = [m for m in history if m.get('reply_to') in {item['seq'] for item in own}]
        choices = [m for m in own if m.get('task_state') in {'declined', 'paused', 'submitted'}]
        states[role] = {
            'personal_now': run.get('person_situations', {}).get(role, {}),
            'own_recent': [{k: m[k] for k in ('seq', 'content')} for m in own[-4:]],
            'received_replies': [{k: m[k] for k in ('seq', 'sender', 'content')} for m in addressed[-3:]],
            'last_spoke': own[-1]['seq'] if own else None,
            'explicit_choices': [{'seq': m['seq'], 'content': m['content'], 'state': m['task_state']} for m in choices[-3:]],
            'last_nonactive_choice': choices[-1]['task_state'] if choices else 'unknown',
            'past_experience': session.get('long_term_memory', [])[:4],
        }
    return {'public_history': [{k: m[k] for k in ('seq', 'sender', 'content', 'reply_to', 'stage')} for m in history],
            'private_by_owner': states, 'threads': run.get('conversation_threads', []),
            'current_stage': history[-1]['stage'] if history else STAGES[0]}


def naturalness_evidence(messages: list[dict]) -> dict:
    total = len(messages)
    replies = sum(m.get('reply_to') is not None for m in messages)
    # These are review candidates, not a ban on occasional legitimate explanations.
    pattern = re.compile(r'不是别人劝|不用等我报进度|不给自己安排同步打卡|大家不用同步|'
                         r'不替谁按开始键|我只说自己做完|比跟着喊.{0,10}自然|只评价这一次')
    candidates = [{'seq': m['seq'], 'quote': m['content'], 'reason': '疑似替编剧交代自主性或交流规则'}
                  for m in messages if pattern.search(m['content'])]
    return {'messages': total, 'explicit_reply_ratio': round(replies / total, 3) if total else 0,
            'delay_values': sorted({int(m.get('delay') or 1) for m in messages}),
            'self_explanation_candidates': candidates[:12]}


def flow_errors(messages: list[dict], source_literals: list[str], complete: bool = True) -> list[str]:
    errors = []
    previous = 0
    for message in messages:
        stage = message.get('stage')
        if stage not in STAGES:
            errors.append(f"消息 {message['seq']} 缺少有效交流阶段")
            continue
        rank = STAGES.index(stage)
        if rank < previous or rank > previous + 1:
            errors.append(f"消息 {message['seq']} 的开场顺序不连贯")
        previous = rank
        if rank < 2 and any(value in message['content'] for value in source_literals):
            errors.append(f"消息 {message['seq']} 在寒暄或了解群用途阶段提前公布原值")
    if messages and messages[0].get('stage') != STAGES[0]:
        errors.append('第一条应先打招呼暖场')
    if complete and messages and previous != 2:
        errors.append('对话没有从寒暄进入正题')
    if complete and len(messages) >= 30:
        evidence = naturalness_evidence(messages)
        if evidence['explicit_reply_ratio'] > .9:
            errors.append('几乎每条都引用回复，接话过于整齐；普通接话应使用回复编号0')
        if len(evidence['delay_values']) < 2:
            errors.append('消息间隔全部相同，应依据说话长度与停顿给出不同间隔')
    return errors
