"""Adapt ViMax's novel Event contract to a source-backed group conversation."""
from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from .opening import opening_requirement
from .model_stream import ModelRefusalError, plain_text_refusal

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
    seed: str = Field(description='可聊的一件具体经历或问题及其细节，不写谁接谁、讨论原则或统一结论')


class PersonSituation(BaseModel):
    role: str
    personal_now: str = Field(description='本人眼前的小事，别人只有听他说出后才知道')
    cares_about: str
    may_ignore: str = Field(description='哪些与自己无关的消息可以不接')


class ConversationBeat(module.Event):
    description: str = Field(min_length=1, description='可触发的具体经历或信息差，不预定转场或群体结论')
    process_chain: list[str] = Field(min_length=1, description='素材里有哪些不同的实际细节，不安排逐人表态或依次领悟')
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
    experience_seed: str = Field(default='', description='本人可能愿意提起的一件具体生活经历，含实际细节；不是固定道具或待办，不要求每人都讲')


class OrganicPlan(BaseModel):
    events: list[ConversationBeat] = Field(min_length=1, max_length=18)
    threads: list[ConversationThread] = Field(min_length=1, max_length=8)
    people: list[PersonIntent] = Field(min_length=1)


DialoguePlan.model_rebuild()


def independent_planning_requirement(run: dict) -> str:
    # Adapt descriptions for fresh creation, keeping the shared parser and
    # fixed-mode schema untouched. These are still the same OrganicPlan fields.
    schema = copy.deepcopy(OrganicPlan.model_json_schema())
    descriptions = {
        'PersonIntent': {
            'wants': '本人此刻想做、想说或想感受的事；可以只是好奇、分享或想被听见，不必是待解决的问题',
            'attention': '结合人格，哪些见闻、喜好、话语或处境会吸引本人注意',
            'reply_when': '本人愿意怎样接哪类话；可以回应感受、表达喜恶、开玩笑或继续分享',
            'voice': '本人自然的表达长度、节奏和直接程度，随交流需要变化',
            'experience_seed': '本人可能愿意提起的见闻或经历及具体细节；可留空，是否分享由当时兴趣决定',
        },
        'ConversationThread': {
            'seed': '可聊的见闻、喜好、玩笑、经历或疑问及其具体内容，保留继续交流的可能',
        },
        'ConversationBeat': {
            'description': '一份可自由展开的交流素材，可以是分享、感受、玩笑、好奇或实际需要',
            'process_chain': '素材中可供回应、联想或继续分享的具体内容；不是必须依次完成的动作',
            'cause': '本人此时为什么想开口，可以只是感兴趣或想分享',
            'outcome': '交流可能留下的内容、感受、认识或动作；日常交流不必解决问题或贡献业务目标',
            'trigger': '什么情境、兴趣或公开话语可能引出它；本人想分享也可以成为起点',
            'stop_when': '本人说够、兴趣转移或无人接话时可以停下，不要求得到答案或统一结论',
        },
    }
    for name, fields in descriptions.items():
        for field, description in fields.items():
            schema['$defs'][name]['properties'][field]['description'] = description
    return (
        '\n' + opening_requirement(run) +
        '这是从零创作本份群聊的素材规划，按本份人格和风格方案形成各人的关注、表达节奏与接话意愿。'
        'people覆盖名单是人物画像，不是发言任务；wants可以是想分享、好奇、表达喜恶或仅想被听见，'
        'reply_when写本人愿意接什么话，不要求每人有问题，也不要求每句帮助别人或贡献业务目标。'
        '先从每个人自己的生活、兴趣和当下表达欲写people，再判断他是否关心任务；'
        '人物差异不等于分工检查链接、材料、时间或证据边界。'
        '母本流程直接约束事实和动作先后；只有人物真正不明白或有不同判断时才形成提问事件，'
        '不要把已经清楚说明的每个步骤再拆成复述、确认、漏看补问素材。'
        '分享见闻、随口玩笑、感受与兴趣本身都能成为话题；可以接着聊、略过、意见不同或自然转开。'
        '兴趣和情绪可以自行引起开口，不需要等业务结束或从任务态度转译成生活话题。'
        '按目标篇幅准备有具体内容、能够持续交流的素材，数量随内容需要；'
        '素材可以展开较长，也可以只用一点，不用排满事件上限或逐人传故事。'
        'events和threads是可选素材，不是时间表；index从0连续，is_last只表示素材列表结束，'
        '不分配消息区间、发言配额或固定执行者。trigger和stop_when记录自然聊起与停下的可能。'
        '必要业务事实和原值只来自母本，保持活动条件、链接、联系人和代码；愿意者依公开说明完成母本目标。'
        '操作和截图遵守实际因果：先公开说明，再操作和截图，随后按母本顺序提交；'
        '母本要求截图发群时，规划相应图片事件，素材码放媒体字段。申请提交与外部确认、到账分开。'
        '每人的私有处境只约束本人，别人依据此前公开的话回应；生活见闻可创作，业务规则和外部结果依母本。'
        '人物起初陌生，关系随实际交流形成；规划本身不代表背景、动作或共同过去已经公开发生。'
        '保留读懂交流所需的对象与前因，字段简洁；仅返回以下JSON结构，所有说明使用输出语言：\n' +
        json.dumps(schema, ensure_ascii=False)
    )


def planning_requirement(run: dict) -> str:
    if (run['config'].get('creation_mode') == 'independent_random'
            and run['config'].get('dialogue_flow_version', 1) >= 3):
        return independent_planning_requirement(run)
    if run['config'].get('dialogue_flow_version', 1) >= 3:
        return (
            '\n' + opening_requirement(run) +
            '先把人格卡转成每个人的wants、attention、reply_when、leave_unanswered、voice；'
            '角色差异必须影响注意、误解、选择和接话对象，不是给每个人分配一种生活道具。'
            '本人还可以有与参与任务无关的真实兴趣和具体生活经历，不把所有人的需要都写成评估任务或观察别人如何作决定。'
            '初入群互不熟悉；不编造共同过去。少数人可逐渐聊熟，别人不必跟着捧场。'
            '事件是可触发的交流素材，不是时间表。禁止分配消息编号、条数配额、固定主行动者或轮流完成名单。'
            'index只标识素材，is_last只表示列表结束，不代表群聊必须按顺序演完；'
            '无需把所有素材都用上。process_chain描述信息差或真实意见如何变化，而非逐项操作汇报。'
            '长篇也只准备少数真正有交流需要的素材，业务流程作为完整因果线；'
            '支线围绕少数人实际关心的问题，不为每人准备一段可轮流展示的生活故事。'
            '每个字段用简短描述，不为填满schema上限凑18个事件，不把同一次说明拆成多个理解障碍。'
            '每个素材给出trigger和stop_when；问题有答案、笑点已接住、个人小事已办完就停止，不续写道具状态。'
            '熟悉必须有过程：开始以低门槛回应和当下问题为主，有具体共同兴趣的少数人经过公开问答才愿意多说；'
            '不把刚进群的所有人写成立即互讲往事的朋友，也不靠改成老群友或编造共同过去解决。'
            'experience_seed可以留空，personal_now只是本人当下背景，不是必须公开的故事任务。'
            '素材之间不能只靠共享一个名词相连，如钥匙接遥控器接书架接菜谱；'
            '个人项目和个人决定不自动变成全群的共同项目，不给其他角色追加相同需求或轮流投票的责任；'
            '提出新话题的人须有当时的实际疑问、未解决的需要或对前话的不同判断，不预排谁负责接龙。'
            '人格中的幽默是可选反应，不给幽默人物安排每隔几条一句拟人比喻；普通短答、没接话也符合人格。'
            '可以聊出分歧、答非预期或无人接话，不预排谁赞同谁；不以开新业务条件或页面故障制造信息量。'
            '不把自主选择、个人边界、理解对方、一次和长期的区别、客气话含义或语气误会规划成长话题；'
            '这些是创作约束，不是群友要共同讨论的课程。不能让一句普通告别引发多人分析态度。'
            '个人经历可以有普通物品、具体场景和小插曲，明确属于本人生活，不暗示它是任务商品、任务页面或母本活动的真实结果。'
            '素材须包含读懂交流所需的具体背景和动机，但背景只属于本人，是否说出由交流需要决定；不能依赖读者脑补未呈现的关键经历。'
            '业务事实仅来自母本；人物决定和动作仍须遵守已公开信息与因果顺序。'
            '不提前安排今晚、下次、固定时段等母本和公开聊天未提出的预约或承诺；'
            '没有人在问个人是否参加时，不必突然发布参加或不参加声明。'
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
    independent = organic and run['config'].get('creation_mode') == 'independent_random'
    if independent and plain_text_refusal(text):
        # The raw response is already retained by RecordedChat. Do not bypass a
        # refusal by extracting a later object, or echo private response text.
        raise ModelRefusalError('refusal', '规划响应明确拒绝，未尝试提取后续JSON')
    incomplete = ('事件规划格式不完整，需要包含人物动机、话题触发与停止条件' if organic else
                  '事件规划格式不完整，需要包含起因、过程、结果和消息区间')
    try:
        plan = TrailingCommaTolerantPydanticOutputParser(pydantic_object=OrganicPlan if organic else DialoguePlan).parse(text)
    except Exception as exc:
        start = text.find('{') if isinstance(text, str) else -1
        if not independent or start < 1 or not text[:start].strip():
            raise ValueError(incomplete) from exc
        try:
            # Only discard a non-JSON preamble before one complete object. The
            # standard decoder must consume the whole remainder: no partial
            # JSON repair, invented field values, multiple objects or trailing
            # prose. Existing schema-defined optional defaults stay unchanged.
            value, end = json.JSONDecoder().raw_decode(text, start)
            if not isinstance(value, dict) or text[end:].strip():
                raise ValueError('顶层JSON对象之后仍有其他内容')
        except (ValueError, TypeError) as extraction_error:
            raise ValueError('独立事件规划不是唯一完整JSON对象；未补写缺失字段或截断内容') from extraction_error
        try:
            plan = OrganicPlan.model_validate(value)
        except Exception as validation_error:
            raise ValueError(incomplete) from validation_error
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
