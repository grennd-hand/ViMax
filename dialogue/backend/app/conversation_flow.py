"""Rolling dialogue context and evidence for naturalness review."""
from __future__ import annotations

import re
from difflib import SequenceMatcher


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
    context = {'public_history': [{k: m.get(k) for k in ('seq', 'sender', 'content', 'reply_to', 'stage', 'type', 'image_code', 'image_mode')} for m in history],
            'private_by_owner': states, 'threads': run.get('conversation_threads', []),
            'current_stage': history[-1]['stage'] if history else STAGES[0]}
    if run['config'].get('dialogue_flow_version', 1) >= 3:
        context['turn_selection'] = {
            'rule': '根据本人需要与公开消息选择发言，不按最近没说话或人头平衡补台词；私人动机不能被别人直接接住。',
            'relationship_baseline': '陌生群友，熟悉程度只依据已有公开互动；不要让所有人立即互相照料。',
            'recurrence_candidates': repeated_passages(history),
            'continuation_check': '对照全文，问题有答案就不重新教学；再次提起旧话题必须带来新事实、不同观点或个人需要。',
            'social_check': '不要把别人的最后一个名词当成下一个故事的邀请；熟悉程度只按已经实际发生的公开交流判断。',
            'dependency_check': '参加/拒绝、今晚安排、那我/我也等回应必须有当时的前因；无新触发不发布立场公告。',
        }
    return context


def repeated_passages(messages: list[dict]) -> list[dict]:
    """Flag full-run repeated wording for contextual review, never rewrite facts."""
    candidates, previous = [], []
    for message in messages:
        # Shared literals alone are not evidence of copied dialogue.
        text = re.sub(r'https?://\S+|【[^】]*】|@\w+', '', message['content'])
        text = re.sub(r'\W+', '', text).casefold()
        if len(text) < 14:
            continue
        match = next((old for old, normalized in reversed(previous)
                      if SequenceMatcher(None, normalized, text).ratio() >= .86), None)
        if match:
            candidates.append({'seq': message['seq'], 'earlier_seq': match['seq'],
                               'quote': message['content'], 'earlier_quote': match['content'],
                               'reason': '跨全文近似表达，需核对是否确有新的信息，正常引用不自动判错'})
        previous.append((message, text))
    return candidates[-12:]


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
            'self_explanation_candidates': candidates[:12],
            'repeated_passage_candidates': repeated_passages(messages)}


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
