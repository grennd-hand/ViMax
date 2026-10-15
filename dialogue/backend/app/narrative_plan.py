"""Adapt ViMax's novel Event contract to a source-backed group conversation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Literal

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
    threads: list['ConversationThread'] = Field(default_factory=list, max_length=8)
    people: list['PersonSituation'] = Field(default_factory=list)


class ConversationThread(BaseModel):
    topic: str
    kind: Literal['业务', '日常', '关系']
    roles: list[str]
    seed: str = Field(description='可引出具体交流的情境，不写预定台词或统一结论')


class PersonSituation(BaseModel):
    role: str
    personal_now: str = Field(description='本人眼前的小事，别人只有听他说出后才知道')
    cares_about: str
    may_ignore: str = Field(description='哪些与自己无关的消息可以不接')


class ConversationBeat(module.Event):
    roles: list[str] = Field(min_length=1)
    cause: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    carry_forward: str = Field(min_length=1)
    trigger: str = Field(min_length=1, description='什么公开消息或本人需要能引出它；没有触发就不用')
    stop_when: str = Field(min_length=1, description='何时话题已经说够，停止续写；不预定消息区间')


class PersonIntent(PersonSituation):
    wants: str = Field(min_length=1, description='本人此刻想解决的具体事情，不预定参加或拒绝')
    attention: str = Field(min_length=1, description='结合人格，什么信息值得本人开口')
    reply_when: str = Field(min_length=1, description='对谁的哪类话愿意接，接话给对方带来什么新内容')
    leave_unanswered: str = Field(min_length=1, description='即使看到了也不想回应的内容')
    voice: str = Field(min_length=1, description='表达长度、直接程度、解释习惯，不写固定口头禅')
    relationship: str = Field(min_length=1, description='起初是陌生群友；熟悉程度只随公开互动改变')


class OrganicPlan(BaseModel):
    events: list[ConversationBeat] = Field(min_length=1, max_length=18)
    threads: list[ConversationThread] = Field(min_length=1, max_length=8)
    people: list[PersonIntent] = Field(min_length=1)


DialoguePlan.model_rebuild()


def planning_requirement(run: dict) -> str:
    if run['config'].get('dialogue_flow_version', 1) >= 3:
        return (
            '\n' + opening_requirement(run) +
            '先把人格卡转成每个人的wants、attention、reply_when、leave_unanswered、voice；'
            '角色差异必须影响注意、误解、选择和接话对象，不是给每个人分配一种生活道具。'
            '初入群互不熟悉；不编造共同过去。少数人可逐渐聊熟，别人不必跟着捧场。'
            '事件是可触发的交流素材，不是时间表。禁止分配消息编号、条数配额、固定主行动者或轮流完成名单。'
            'index只标识素材，is_last只表示列表结束，不代表群聊必须按顺序演完；'
            '无需把所有素材都用上。process_chain描述信息差或真实意见如何变化，而非逐项操作汇报。'
            '每个素材给出trigger和stop_when；问题有答案、笑点已接住、个人小事已办完就停止，不续写道具状态。'
            '为所需长篇准备有区别的兴趣、真实疑问、个人经历和观点碰撞，日常选择无需变成一串清洁或吃饭步骤。'
            '可以聊出分歧、答非预期或无人接话，不预排谁赞同谁；不以开新业务条件或页面故障制造信息量。'
            '业务事实仅来自母本；人物决定和动作仍须遵守已公开信息与因果顺序。'
            '仅返回以下JSON结构，所有说明使用输出语言：\n' +
            json.dumps(OrganicPlan.model_json_schema(), ensure_ascii=False)
        )
    policy = run['config'].get('opening_policy')
    legacy = policy and policy.get('version', 1) == 1
    boundaries = (f"寒暄暖场必须为独立事件并结束于第{policy['warm_end']}条，"
                  f"了解群用途必须为独立事件并结束于第{policy['orientation_end']}条。" if legacy else
                  '事件区间仅为篇幅参考，允许话题交错，不按区间轮流考核步骤。'
                  '同时返回threads与people：为长篇准备业务之外的具体日常话题和每个人自己的当下处境，'
                  '不设一个主角让其余人陪练，不预定所有人的最终立场，动作完成位置不限。'
                  '日常话题要能产生实质内容，如具体吃饭选择、工作中的小窘事、手机使用习惯；'
                  '不能只有忙、没空、按自己的节奏等空泛态度；这些不是新的活动。'
                  '关系话题应是某两个人因具体交流产生的熟悉、误会或兴趣，'
                  '不要把先问清再动手、不参加也能聊天、不跟风等制作原则设成整段话题。')
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
    organic = run['config'].get('dialogue_flow_version', 1) >= 3
    try:
        plan = TrailingCommaTolerantPydanticOutputParser(pydantic_object=OrganicPlan if organic else DialoguePlan).parse(text)
    except Exception as exc:
        raise ValueError('事件规划格式不完整，需要包含人物动机、话题触发与停止条件' if organic else
                         '事件规划格式不完整，需要包含起因、过程、结果和消息区间') from exc
    expected = 1
    roles = set(run['agent_sessions'])
    for index, event in enumerate(plan.events):
        if event.index != index or (not organic and (event.message_start != expected or event.message_end < expected)):
            raise ValueError('事件规划的编号或消息区间不连续')
        if not set(event.roles) <= roles:
            raise ValueError('事件规划使用了未设置的人物')
        if not event.process_chain or any(not item.strip() for item in event.process_chain):
            raise ValueError('事件规划缺少具体交流过程')
        if event.is_last != (index == len(plan.events)-1):
            raise ValueError('事件规划的结束标记不正确')
        if not organic:
            expected = event.message_end + 1
    if not organic and expected != run['config']['target_messages'] + 1:
        raise ValueError('事件规划未覆盖所需消息总数')
    policy = run['config'].get('opening_policy')
    if not organic and policy and policy.get('version', 1) == 1:
        ends = {event.message_end for event in plan.events}
        if not {policy['warm_end'], policy['orientation_end']} <= ends:
            raise ValueError('事件规划需将寒暄暖场、了解群用途与正题分开，保持规定的开场区间')
    if run['config'].get('dialogue_flow_version', 1) >= 2:
        if not plan.threads or len(plan.people) != len(roles) or {p.role for p in plan.people} != roles:
            raise ValueError('续写规划需包含交流话题和每个人的当下处境')
        if any(not set(thread.roles) <= roles for thread in plan.threads):
            raise ValueError('交流话题使用了未设置的人物')
        run['conversation_threads'] = [t.model_dump() for t in plan.threads]
        run['person_situations'] = {p.role: p.model_dump() for p in plan.people}
    return [event.model_dump() for event in plan.events]
