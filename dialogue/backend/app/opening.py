"""A shared, versioned opening contract for new dialogue runs."""
from __future__ import annotations


def opening_policy(total: int) -> dict:
    # Keep a few exchanges for short previews, and eight for a 150-message run.
    warm_end = min(8, max(1, total // 20 + 2), max(1, total - 2))
    orientation_end = min(total - 1, warm_end + min(4, max(1, total // 30)))
    return {'version': 1, 'warm_end': warm_end, 'orientation_end': orientation_end,
            'main_start': orientation_end + 1}


def opening_requirement(run: dict) -> str:
    policy = run['config'].get('opening_policy') or opening_policy(run['config']['target_messages'])
    warm_end, orientation_end = policy['warm_end'], policy['orientation_end']
    return (
        f'开场必须按顺序进行：第1至{warm_end}条先寒暄暖场，第1条坐席只打招呼，'
        '不立刻说明群用途、活动、奖励或操作。其他人刚进群，不预先知道这是做什么的群；'
        '有人回招呼、有人说眼前一件日常小事、有人接别人的玩笑、有人慢热少说。'
        '日常小事是人物的当下情况，不得编造共同过去、地理位置、活动时间或奖励规则。'
        '不同人格决定接谁的话、说多少和是否接话；不用每人打卡，不轮流报到，不堆天气饭点问题。'
        f'第{warm_end + 1}至{orientation_end}条自然转到了解群用途：一至两人问群是做什么的，'
        '坐席在收到疑问后及时用一两句解释，其他人按兴趣、顾虑或个人安排回应。'
        '已解释后不能继续假装不知道，也不让参与者替坐席提前说明母本内容。'
        f'第{policy["main_start"]}条起才进入母本正题，公布入口、联系人、固定代码和具体操作。'
        '暖场的结果可以是有人回应、关系稍熟和愿意听说明，不要求每句产生业务动作。'
        '这段开场计入总消息数，不额外增加条数；历史经历不代表已经知道本群用途。'
    )


def opening_stage(seq: int, policy: dict | None) -> str:
    if not policy:
        return '自然群聊'
    if seq <= policy['warm_end']:
        return '寒暄暖场'
    if seq <= policy['orientation_end']:
        return '了解群用途'
    return '自然群聊'


def opening_schedule(schedule: list[dict], policy: dict | None) -> list[dict]:
    if not policy:
        return schedule
    return [{**slot, 'stage': opening_stage(index + 1, policy)} for index, slot in enumerate(schedule)]
