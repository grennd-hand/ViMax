"""Adapt ViMax's novel Event contract to a source-backed group conversation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from pydantic import BaseModel, Field
from .opening import opening_requirement

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('vimax_novel_event', ROOT / 'interfaces' / 'event.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DialogueEvent(module.Event):
    message_start: int = Field(ge=1, description='事件开始的消息编号')
    message_end: int = Field(ge=1, description='事件结束的消息编号')
    roles: list[str] = Field(min_length=1, description='参与本事件的已有角色')
    cause: str = Field(min_length=1, description='由前面的公开信息或角色动机引起的具体原因')
    outcome: str = Field(min_length=1, description='本事件带来的新信息、实际选择或动作变化')
    carry_forward: str = Field(min_length=1, description='后续必须保持的人物选择、已知信息或已完成动作')


class DialoguePlan(BaseModel):
    events: list[DialogueEvent] = Field(min_length=1, max_length=18)


def planning_requirement(run: dict) -> str:
    policy = run['config'].get('opening_policy')
    boundaries = (f"寒暄暖场必须为独立事件并结束于第{policy['warm_end']}条，"
                  f"了解群用途必须为独立事件并结束于第{policy['orientation_end']}条；后续事件才进入正题。" if policy else '')
    return (
        '\n' + opening_requirement(run) + boundaries +
        '\n借用ViMax小说事件规划的完整因果链，将每个不同交流事件写成起因、过程、结果，并保留跨事件人物连续性。'
        '不同事件不能重演同一步骤，结果应改变公开信息或某个人的选择；解决的问题后续不再重新询问。'
        '只规划台词将要呈现的事件，不把计划当作已经发生，不编造外部结果或新业务规则。'
        '人物改变主意必须有前面实际公开的新信息或自己的明确理由；拒绝者不被安排继续操作。'
        f"消息区间从1到{run['config']['target_messages']}连续覆盖，index从0递增，只有最后事件is_last为true。"
        '角色只使用已有角色名，process_chain至少包含一项有因果关系的具体交流过程。'
        '仅返回符合以下结构的JSON；所有描述用输出语言，150条一般规划12至18个不同事件：\n'
        + json.dumps(DialoguePlan.model_json_schema(), ensure_ascii=False)
    )


def parse_plan(text: str, run: dict) -> list[dict]:
    # Reuse the upstream parser, including its local trailing-comma recovery.
    import sys
    if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
    from utils.robust_json_parser import TrailingCommaTolerantPydanticOutputParser
    try:
        plan = TrailingCommaTolerantPydanticOutputParser(pydantic_object=DialoguePlan).parse(text)
    except Exception as exc:
        raise ValueError('事件规划格式不完整，需要包含起因、过程、结果和消息区间') from exc
    expected = 1
    roles = set(run['agent_sessions'])
    for index, event in enumerate(plan.events):
        if event.index != index or event.message_start != expected or event.message_end < expected:
            raise ValueError('事件规划的编号或消息区间不连续')
        if not set(event.roles) <= roles:
            raise ValueError('事件规划使用了未设置的人物')
        if not event.process_chain or any(not item.strip() for item in event.process_chain):
            raise ValueError('事件规划缺少具体交流过程')
        if event.is_last != (index == len(plan.events)-1):
            raise ValueError('事件规划的结束标记不正确')
        expected = event.message_end + 1
    if expected != run['config']['target_messages'] + 1:
        raise ValueError('事件规划未覆盖所需消息总数')
    policy = run['config'].get('opening_policy')
    if policy:
        ends = {event.message_end for event in plan.events}
        if not {policy['warm_end'], policy['orientation_end']} <= ends:
            raise ValueError('事件规划需将寒暄暖场、了解群用途与正题分开，保持规定的开场区间')
    return [event.model_dump() for event in plan.events]
