"""ViMax writing with source-backed dialogue contracts and independent memories."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

import httpx
from langchain_openai import ChatOpenAI

from .core import validate_run, source_literals
from .framework_team import run_group_chat
from .locale import language_context, message_char_limit
from .persona_memory import record_public_memory
from .memory_store import normalize_reflections
from .memory_store import SQLiteMemoryStore
from .memory_cognition import prepare_memory_context
from .dialogue_quality import explicit_refusal
from .scene_memory import apply_review_actions, apply_review_decisions
from .narrative_plan import planning_requirement, parse_plan
from .opening import opening_requirement, opening_stage
from .conversation_flow import STAGES, rolling_context, naturalness_evidence, flow_errors
from .media_contract import decode_media, screenshot_errors
from .version_archive import archive_candidate
from .creative_variation import build_variation_prompt
from .dialogue_corpus import build_reference_prompt
from .independent_writing import WRITER_PROMPT as INDEPENDENT_WRITE_PROMPT, creation_brief

ROOT = Path(__file__).resolve().parents[3]
PLAN_PROMPT = """你是多人群聊编剧。母本提供业务事实，群友有自己的关注和需求。先规划人为什么开口，再准备可能被触发的话题；不写逐句台词，不替每个人安排一段生活展示或任务表演。业务动作按已公开信息发生，话题不按人头或完成位置排队。普通经历可以创作，不得变成新业务条件、已核实结果或共同过去。"""
WRITE_PROMPT = """你在续写多人群聊。先在内部确定这个人为什么此刻想说、对哪条公开话有反应；没有动机的人不分配台词。不要输出这个判断过程。只说本人当下想说的那一点，不要求每条都成为完整见解；可以短反应、半句补充或同一人连续几条解释，但不机械拆句凑数。
逐句检查听者能否直接明白说的是什么、为什么此刻这样接。不要为了像口语而省掉关键对象、动作经过或比较参照；先、也、又、还等词要有可理解的前因，慢、那个、这样等所指应能从已公开上下文辨明。已说清的内容可以自然省略，不能要求读者替人物补出一段未交代的经历；发现含糊就换成当事人会说的具体表达，不追加一段解释台词。
人物的attention、reply_when、leave_unanswered和人格卡决定接话对象、发言多少、观点和解释习惯。有人直问短答，有人愿意讲自己的具体经历，有人接一个细节或不认同；不是大家都说一句体谅、打趣或总结。不要用‘我就这样的人’解释人设，不照搬名人身份、口头禅或原作经历。
先回应对方真正问的部分；若不知道就不装懂。已回答的问题没有新的个人情境就不重问。普通陈述不需要别人确认，提问也不用被夸‘问得好’，玩笑可没人接。群友初时陌生，不立即相互管饭、提醒喝水或替对方安排；亲近需要前面的公开互动。
陌生群友开始只交流自己此刻愿意说的部分；少数人因具体共同兴趣逐渐多聊，不让全群立即轮流讲生活往事。接话要接对方的问题、需要或观点，不靠最后一个名词接出下一段自己的故事。不必人人有类似经历，更不能把每个人的experience_seed排队用完。某段两个人有内容可多聊，旁人可以持续不发言。
一个人的选片、买东西、做小事不等于全群共同负责的项目。对方已作出选择后，没有新问题就不让多人继续论证、投票或总结同一理由；不能为了留住话题，临时给旁人安排相同需求。别人的社区通知、表格等资料只有当事人已公开说出的内容才可用，其他人不能像也见过原件一样说漏看了后半句。
每次想写‘我也有一次’时先确认这段经历给当前对方带来什么；仅证明自己也遇过不必加入。新话题可以无缝插进来，也可以被忽略，不需要造一座名词桥。幽默角色可以认真问、短答或没话说，不承担持续逗笑职责；不用量勺出走、清单审核、衣服诚实一类成套拟人梗维持人设。
让真实兴趣、个人经历、不同看法和信息差带动长篇。个人经历要有实际发生的事和具体细节，不能只说以前如何、现在如何、所以想明白了什么。不要拿热饭、照片、水杯等任一道具反复更新小状态；一件小事说完就放下。话题回流必须有新内容或新角度，不是提醒它还没完成。别人说生活小事时，不必人人接‘我也’并附一句温柔建议。避免每条都自洽、机智、解释完整；短回应和长叙述由本人动机决定，不机械交替，不强塞错字或无意义噪声。
创作约束不能变成台词主旨。不要让大家反复讨论个人边界、空闲安排、如何问明白、一次尝试和长期承诺的区别；也不要分析别人一句普通回复、告别或改口背后的态度。两个人有具体事说可以继续，没有新的事实、细节或不同意见就换有内容的话头。知道一个道理不需要每个人轮流举例再总结。每段续写重新查看此前已经聊透的内容，不能把上一段结论换几种说法继续讲。
任务可以在合适位置完成，只让需要反馈的人说确实做过的动作。材料和代码由坐席先公布；后续群友依上下文表达，不复制同一套全流程汇报。不要替审阅者解释‘只算眼前这一项’‘按公开说明完成’或反复证明不跟风。业务完成后仍由人物兴趣决定接什么话，不强制转成日常专场，不预排结尾总结。
人物没有被问、没有新的需要时不必宣布参加或拒绝；不能为业务回流插入悬空的立场声明。今晚、约时间、预留空闲等说法须有当前公开聊天中的前因。修订前文后，重新核对后面的‘那我’‘也’‘今晚’等依赖是否还成立，原有业务句也允许移除或重写，不当作不可移动的检查点。
普通接话不必引用，只有隔着几条容易混淆或针对旧话时才设置回复编号，其余用0。允许话题交错、漏看、暂时不发言；业务事实、人物已知信息和动作因果必须一致，不虚构规则、页面细节或外部结果。仅返回JSON的script数组，台词格式遵循本次要求。
{format_instructions}"""


def screenwriter_class():
    # Loading this one upstream module avoids importing the video/GPU stack.
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    spec = importlib.util.spec_from_file_location("vimax_dialogue_screenwriter", ROOT / "agents" / "screenwriter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Screenwriter


def decode_scenes(scenes: list[str], run: dict, start: int = 1, expected_count: int | None = None) -> list[dict]:
    messages = []
    roles = set(run["agent_sessions"])
    for scene in scenes:
        for line in scene.splitlines():
            if not re.match(r"^\s*\d{3}\|", line):
                continue
            rolling = run['config'].get('dialogue_flow_version', 1) >= 2
            media = run['config'].get('dialogue_flow_version', 1) >= 4
            if rolling:
                parts = line.strip().split('|', 7 if media else 5)
                if len(parts) != (8 if media else 6):
                    raise ValueError('台词需要编号、人物、回复编号、间隔秒、交流阶段、消息类型、图片码和内容八项' if media else
                                     '新台词需要编号、人物、回复编号、间隔秒、交流阶段和文字六项')
                if media:
                    number, speaker, reply, delay, stage, kind, image_code, text = parts
                else:
                    number, speaker, reply, delay, stage, text = parts
                if not delay.isdigit() or not 0 <= int(delay) <= 600 or stage not in STAGES:
                    raise ValueError('消息间隔需为0至600秒，交流阶段需为寒暄暖场、了解群用途或自然群聊')
            else:
                number, speaker, reply, text = line.strip().split('|', 3)
                delay, stage = '1', opening_stage(int(number), run['config'].get('opening_policy'))
            seq = int(number)
            reply_to = int(reply) if reply.strip() not in {"0", "-", "null"} else None
            if seq != len(messages) + start:
                raise ValueError(f"消息编号不连续：期望{len(messages)+start}，实际{seq}")
            if speaker not in roles:
                raise ValueError(f"消息{seq}使用了未设置的人物：{speaker}")
            if reply_to is not None and not 0 < reply_to < seq:
                raise ValueError(f"消息{seq}回复了尚未出现的消息")
            payload = decode_media(kind, image_code, text) if media else {'type': '文本', 'content': text.strip(), 'image_code': None, 'image_mode': None}
            if (not payload['content'] and payload['type'] != '图片') or len(payload['content']) > message_char_limit(run["config"].get("language"), seat=speaker == "坐席"):
                raise ValueError(f"消息{seq}为空或超出单条长度")
            declined = speaker != "坐席" and explicit_refusal(text, run["config"].get("language")) is True
            messages.append({"seq": seq, "sender": speaker, **payload, "reply_to": reply_to,
                             "delay": delay,
                             "replace_date": None, "source_seq": None, "source_row": None,
                             "source_rows": [], "reply_source_seq": None, "stage": stage,
                             "reaction": "refuse" if declined else "continue", "task_state": "declined" if declined else "active", "performed_steps": []})
    count = expected_count if expected_count is not None else run['config']['target_messages']
    if len(messages) != count:
        raise ValueError(f'需要{count}条消息，实际生成{len(messages)}条')
    if start == 1 and messages[0]['sender'] != '坐席':
        raise ValueError('第一条需要由坐席发言')
    if not run['config'].get('dialogue_flow_version') and messages[-1]['sender'] != '坐席':
        raise ValueError('第一条和最后一条需要由坐席发言')
    return messages


def writing_contract(template: dict, run: dict) -> str:
    target = run["config"]["target_messages"]
    source = template["source"]
    facts = [{"row": m["source_row"], "text": m["content"]} for m in source["messages"] if m["sender"] == "坐席"]
    context = {"goal": template["analysis"], "source_seat_evidence": facts,
               "protected_literals": source_literals(source),
               "cast": list(run['agent_sessions']),
               "roles": run["config"]["persona_cards"] if run['config'].get('persona_strength', 0.85) > 0 else {},
               "persona_strength": run['config'].get('persona_strength', 0.85),
               "language": language_context(run["config"].get("language")),
               "past_experience_by_owner": {role:state.get("long_term_memory",[]) for role,state in run["agent_sessions"].items()},
               "additional_plot": run["config"].get("story_notes", "")}
    variation_instruction = (build_variation_prompt(run['creative_variation'], style_examples=(run.get('corpus_reference') or {}).get('samples'))
                             if run.get('creative_variation') else '')
    variation_instruction += build_reference_prompt(run.get('corpus_reference'))
    if run['config'].get('creation_mode') == 'independent_random':
        return (creation_brief(target) + '\n' + opening_requirement(run) + '\n'
                + json.dumps(context, ensure_ascii=False) + '\n' + variation_instruction)
    return (
        "这是基于Excel母本重新创作的群聊，不是影视对白。最高优先级是坐席内容的实际目的和source_seat_evidence。"
        "母本账号身份、数量、台词顺序均不需要沿用；只使用cast中的人物。"
        "不要虚构任务的新业务规则、指定商品名称、页面布局、页面故障、奖励到账或外部审批结果。"
        "与任务无关的个人生活经历可创作普通物品和具体场景，不得暗示那是任务商品或本次活动的外部结果。"
        "奖励、付款处理和到账时长只按活动方原说明转述，不能保证到账，也不能把说明当作已验证事实；"
        "这是事实边界，不是每个人都要复述的台词模板，坐席说明清楚即可。"
        "原文询问某个条件不等于已经确认该条件；例如有人问是否每天有任务，不能据此宣布每天一定有任务。"
        "坐席解释和引导方式可以完全改写，但不能改坐席说明中的必要事实、时间、顺序、链接、联系人和固定代码。"
        "protected_literals必须逐字复制，括号也是原值的一部分；例如【代码】不能改成不带括号的代码。"
        "入口第一次发布就必须写出完整实际链接，禁止只写活动链接冒号后留空。"
        "固定代码是坐席提供，不是页面生成；不添加联系人通过好友、确认材料后才能发送材料的新条件。"
        "参与者必须等坐席在公开对话中给出原值后再用，不能因为你看到了母本而提前知道。"
        "从知悉任务到自主操作、互动、截图、联系和提交按母本因果顺序发生，不能只说以后准备做。"
        "需要参与者遵循的提交步骤先由坐席说明；群友不能从编剧所见母本里提前知道尚未公开的规定。"
        "有人可以不参加，但其余愿意者应完成已确认目标；不能把群聊改成全员等待核实或停止参与。"
        "至少一位愿意者完成母本目标即可，不预先指定主行动者；其他人根据兴趣回应、晚点加入或不接话，不安排逐人展示。"
        "一个动作实际发生就够，不拆成我准备做、我正在做、我已经做三条；其他人不逐项点评或提醒，不充当陪练。"
        "保持人设、个人安排和已做动作一致，做过的不再重复做；质疑得到答案就继续或作决定。"
        "围绕不同的具体问题、动机和体验展开，人物会接话、打岔、犹豫、纠正自己，长短交错。"
        "persona_strength越高，越让priority、decision_rule和reaction_cues影响人物选择、接话对象与发言频率；"
        "低值只保留轻微口吻差异，零值不用人格卡；不管数值多少都不能改母本事实。"
        "人物名单只用cast；roles是这些普通群友的行为卡，不能自称原型名人或带入原作身份。"
        "不要让所有人轮流汇报同一步骤，不要反复复述风险或已解决的问题，不要新增未经母本支持的活动。"
        f"最终恰好{target}条消息，先由坐席打招呼；结尾可由任何人接话，不需要坐席作总结。"
        "不要把原始账号人数或逐人发言覆盖当成目标；目标完成和自然接话优先。"
        "条数由有内容的交流支撑，不为凑数延长动作、回收笑点或重复等待；任务完成不等于必须进入日常话题专场。" +
        ("截图必须是一条独立图片消息或图片+文本消息，带SCREENSHOT_开头的素材码；"
         "这是剧本待补素材事件，不是真实截图，不把素材标记写成角色台词。"
         "给指定商品点赞后、按母本要求在群里发截图时生成该图片事件；后文才能说已发群。"
         "纯文字说已经分享不代替图片事件，图片也计入总条数。普通群聊仍用文本，文本图片码用-。"
         if run['config'].get('dialogue_flow_version', 1) >= 4 else
         "文字消息不含图片素材，禁止用[已完成截图]之类假图片占位符；可以用文字说明已分享截图。") +
        "不要让人物说‘不虚构结果’‘公开说明的动作’‘活动文本提到’等创作/审阅用语，直接像真人接话。"
        "参与者的私有判断只属于本人，其他人只能依据已公开的话回应。不要输出角色内心、制作解释或旁白。"
        "\n" + opening_requirement(run) + "\n" + json.dumps(context, ensure_ascii=False) +
        ("\n" + variation_instruction if variation_instruction else "")
    )


class RecordedChat:
    def __init__(self, chat, api, session_id, stage):
        self.chat, self.api, self.session_id, self.stage = chat, api, session_id, stage

    async def ainvoke(self, messages):
        began = time.monotonic()
        try:
            response = await self.chat.ainvoke(messages)
        except Exception as exc:
            run = self.api.get_run(self.session_id)
            run.setdefault('vimax_failed_calls', []).append({
                'stage': self.stage, 'seconds': round(time.monotonic()-began, 2),
                'model': self.chat.model_name, 'reasoning_effort': self.chat.reasoning_effort,
                'error_type': type(exc).__name__, 'response_received': False,
            })
            self.api.save_run(run)
            raise
        run = self.api.get_run(self.session_id)
        if run["status"] != "running":
            raise asyncio.CancelledError()
        run.setdefault("vimax_calls", []).append({"stage": self.stage, "seconds": round(time.monotonic()-began, 2),
                                                 "model": self.chat.model_name, "reasoning_effort": self.chat.reasoning_effort})
        run.setdefault("vimax_raw", {})[self.stage] = response.content
        if run['config'].get('dialogue_flow_version', 1) >= 4:
            run.setdefault('vimax_samples', []).append({'stage': self.stage, 'call': len(run['vimax_calls']),
                                                       'content': response.content})
        self.api.save_run(run)
        return response


def output_format(run):
    if run['config'].get('dialogue_flow_version', 1) >= 4:
        return ('八项格式NNN|人物|回复编号或0|间隔秒|交流阶段|消息类型|图片码或-|内容。'
                '文本的图片码填-；图片或图片+文本必须有SCREENSHOT_开头的唯一素材码。'
                '图片行内容可空，图片+文本的内容是本人实际附言，不写舞台说明。'
                '例如001|坐席|0|4|寒暄暖场|文本|-|大家好。')
    if run['config'].get('dialogue_flow_version', 1) >= 2:
        return '六项格式NNN|人物|回复编号或0|间隔秒|交流阶段|文字，例如001|坐席|0|4|寒暄暖场|大家好。'
    return '四项格式NNN|人物|回复编号或0|文字。'


def checkpoint_candidate(api, run, messages, reason):
    if run['config'].get('dialogue_flow_version', 1) < 4:
        return
    latest = api.get_run(run['id'])
    archive_candidate(api.DATA_DIR, latest, messages, reason)
    api.save_run(latest)
    run['candidate_versions'] = latest.get('candidate_versions', [])


async def process(session_id, api):
    try:
        run = api.get_run(session_id)
        template = api.get_template(run["template_id"])
        if run.get("character_bindings") and run["config"].get("memory_enabled") and not run.get("memory_status"):
            memories=await prepare_memory_context(SQLiteMemoryStore(api.DB_PATH),run,template["analysis"].get("primary_goal",""))
            latest=api.get_run(session_id)
            if latest["status"]!="running": return
            for role,items in memories.pop("roles").items(): latest["agent_sessions"][role]["long_term_memory"]=items
            latest["memory_status"]=memories
            api.save_run(latest)
            run=latest
        model_timeout = float(os.getenv('MODEL_TIMEOUT_SECONDS', '90'))
        async with httpx.AsyncClient(trust_env=False, timeout=model_timeout) as http_client:
            base,key,model_name = api.model_settings()
            chat = ChatOpenAI(model=run["config"].get("dialogue_model") or model_name,
                              api_key=key, base_url=base,
                              streaming=True, max_retries=0,
                              max_tokens=32000 if run['config'].get('creation_mode') == 'independent_random' else 16000,
                              timeout=model_timeout,
                              reasoning_effort=api.model_reasoning_effort(run["config"]) or ("low" if re.fullmatch(r"gpt-(?:5|6)(?:\.\d+)?",run["config"].get("dialogue_model") or model_name) else None), http_async_client=http_client)
            writer_type = screenwriter_class()
            contract = writing_contract(template, run)
            if not run.get("vimax_story"):
                run["review_progress"] = {"phase": "planning", "repair_rounds": 0}
                api.save_run(run)
                writer = writer_type(chat_model=RecordedChat(chat, api, session_id, "plan"))
                independent = run['config'].get('creation_mode') == 'independent_random'
                idea = "设计本份陌生群友会愿意聊的内容和人物处境，母本事实与目标按输入保留。" if independent else "根据坐席母本目的，设计群聊事件线。"
                detail = ("只输出本份可触发的交流素材和人物意图，不写逐句台词；分享、见闻、情绪和玩笑也能带来交流。"
                          if independent else "只输出精简事件大纲，不写完整剧本；每个事件提供不同的交流内容、变化与目标贡献；不要按人头轮流报告。")
                story = await writer.develop_story(idea, contract + detail + planning_requirement(run),system_prompt=PLAN_PROMPT)
                run = api.get_run(session_id)
                run["vimax_events"] = parse_plan(story, run)
                run["vimax_story"] = json.dumps({"events": run["vimax_events"]}, ensure_ascii=False)
                api.save_run(run)
            if run['config'].get('creation_mode') == 'independent_random':
                if not run.get('vimax_draft'):
                    run['review_progress'].update(phase='writing', drafted=0, target=run['config']['target_messages'])
                    api.save_run(run)
                    writer = writer_type(chat_model=RecordedChat(chat, api, session_id, 'independent_full_draft'))
                    request = contract + '\n这是本份第一次完整创作。仅依据本份新规划、人格和公开语料参考，从001独立写到目标条数。'
                    request += '\n本份刚规划的人物私有处境和可触发话题：' + json.dumps(rolling_context(run, []), ensure_ascii=False)
                    request += '\n以上私有内容只属于各自人物，不等于其他群友已知；写作中只依据此前实际公开的消息接话。'
                    request += '\n不读取、不续接、不改写其他版本的台词或情节；本份规划中的素材也不用全部排队演完。'
                    request += '\n返回script数组，每个元素是连续编号台词字符串；所有元素合计必须恰好' + str(run['config']['target_messages']) + '条。'
                    request += '\n' + output_format(run)
                    for format_attempt in range(2):
                        scenes = await writer.write_script_based_on_story(run['vimax_story'], request, system_prompt=INDEPENDENT_WRITE_PROMPT)
                        run = api.get_run(session_id)
                        try:
                            draft = decode_scenes(scenes, run)
                            break
                        except ValueError as exc:
                            if format_attempt:
                                raise
                            request += '\n本份初稿格式检查失败：' + str(exc)
                            request += '\n只修正本份格式、连续编号和条数，保留人物与已有情节；完整返回全部目标台词。原始返回：' + json.dumps(scenes, ensure_ascii=False)
                    checkpoint_candidate(api, run, draft, '独立随机完整初稿，尚未结构与质量检查')
                    run['vimax_draft'] = draft
                    run['vimax_segments'] = [{'start': 1, 'end': len(draft), 'strategy': 'whole_fresh_draft'}]
                    run['schedule'] = [{**slot, 'stage': draft[i]['stage']} for i, slot in enumerate(run['schedule'])]
                    run['review_progress']['phase'] = 'validate'
                    api.save_run(run)
            elif run['config'].get('dialogue_flow_version', 1) >= 2:
                target = run['config']['target_messages']
                history = list(run.get('vimax_draft', []))
                while len(history) < target:
                    start = len(history) + 1
                    count = min(30, target - len(history))
                    run['review_progress'].update(phase='writing', drafted=len(history), target=target)
                    api.save_run(run)
                    request = contract + '\n仅续写本段，不重写已生成台词。读取public_history保持事实连续；private_by_owner里的个人处境未公开前仅本人知道。'
                    request += '\n' + json.dumps(rolling_context(run, history), ensure_ascii=False)
                    request += f'\n本段恰好{count}条，从{start:03d}至{start+count-1:03d}。总篇幅{target}条，剩余{target-len(history)}条。'
                    request += output_format(run)
                    request += ('总条数不代表每个人都要发言。根据话题与本人关注选择发言者，不轮流点名，不让没人回应的话硬被接住。'
                                '间隔按输入长度、停顿与忙自己的事自然变化，阶段为寒暄暖场、了解群用途、自然群聊。'
                                '引用回复只在确有指向需要时使用，普通接话用0。'
                                '只剩本段时保证愿意者已完成母本目标；可有普通接话结尾，不必坐席总结。')
                    for attempt in range(2):
                        writer = writer_type(chat_model=RecordedChat(chat, api, session_id, f'write_{start}'))
                        scenes = await writer.write_script_based_on_story(run['vimax_story'], request, system_prompt=WRITE_PROMPT)
                        try:
                            segment = decode_scenes(scenes, run, start, count)
                            combined = history + segment
                            if len(combined) >= target:
                                checkpoint_candidate(api, run, combined, '完整写作候选，尚未结构检查')
                            partial = {**run, 'messages': combined, 'status': 'running'}
                            errors = contract_errors(template['source'], partial)
                            if errors:
                                raise ValueError('；'.join(errors))
                            break
                        except ValueError as exc:
                            if attempt:
                                raise
                            request += '\n上次本段不合格：' + str(exc) + '。重新返回本段完整台词，保留之前已生成历史。'
                    latest = api.get_run(session_id)
                    if latest['status'] != 'running':
                        return
                    history = combined
                    latest['vimax_draft'] = history
                    latest['schedule'] = [{**slot, 'stage': history[i]['stage'] if i < len(history) else '自然群聊'}
                                          for i, slot in enumerate(latest['schedule'])]
                    latest.setdefault('vimax_segments', []).append({'start': start, 'end': len(history)})
                    latest['review_progress'].update(phase='writing', drafted=len(history), target=target)
                    api.save_run(latest)
                    run = latest
                run['review_progress']['phase'] = 'validate'
                api.save_run(run)
            elif not run.get('vimax_draft'):
                writer = writer_type(chat_model=RecordedChat(chat, api, session_id, "write"))
                run["review_progress"]["phase"] = "writing"
                api.save_run(run)
                scenes = await writer.write_script_based_on_story(run["vimax_story"], contract +
                    "\n输出格式：script数组每个元素是一段群聊字符串，内容只能是编号台词行，"
                    "例如001|坐席|0|大家好呀。\n002|客户1|1|你好，刚忙完坐下来。\n"
                    "四个字段分别是三位连续编号、发言人、回复的已有消息编号（不回复则0）、消息文字。"
                    "编号从001连续到目标条数，段与段之间不能重置编号。不要表格、动作、空场景或未编号的引语。"
                    "单条坐席尽量80字内，参与者45字内，长链接必须保持完整，其他必要长度遵循单条上限。"
                    "每条都推进交流或人物选择，不以机械短回答凑数。",system_prompt=WRITE_PROMPT)
                run = api.get_run(session_id)
                draft = decode_scenes(scenes, run)
                run["vimax_draft"] = draft
                run["review_progress"]["phase"] = "validate"
                api.save_run(run)
            # Structural repair works against a draft, before anything is accepted.
            run = api.get_run(session_id)
            virtual = {**run, "messages": run["vimax_draft"], "status": "completed"}
            errors = contract_errors(template["source"], virtual)
            for attempt in range(2):
                if not errors:
                    break
                issues = [{"seq": int(re.search(r"消息\s*(\d+)", error).group(1)) if re.search(r"消息\s*(\d+)", error) else 1,
                           "reason": error} for error in errors]
                await revise(writer_type, chat, api, session_id, template, virtual, issues, "完整结构检查：" + "；".join(errors))
                errors = contract_errors(template["source"], virtual)
                run = api.get_run(session_id)
                run["vimax_draft"] = virtual["messages"]
                run['schedule'] = virtual['schedule']
                run.setdefault("vimax_structural_repairs", []).append({"attempt": attempt+1, "remaining": errors})
                api.save_run(run)
            if errors:
                raise ValueError("；".join(errors))
            # Broadcast through independently scoped role sessions, without extra sampling.
            while True:
                run = api.get_run(session_id)
                if run["status"] != "running":
                    return
                remaining = run["vimax_draft"][run["cursor"]:run["cursor"]+12]
                if not remaining:
                    break
                planned = [{"speaker": m["sender"]} for m in remaining]
                async def own_turn(name, index, public, session):
                    message = dict(remaining[index])
                    message["_private_memory"] = message["content"]
                    return message
                accepted = await run_group_chat(run, planned, own_turn)
                latest = api.get_run(session_id)
                if latest["status"] != "running" or latest["cursor"] != run["cursor"]:
                    return
                for message in accepted:
                    memory = message.pop("_private_memory", "")
                    message["source_rows"] = template["analysis"].get("evidence_rows", []) if message["sender"] == "坐席" else []
                    latest["messages"].append(message)
                    record_public_memory(latest["agent_sessions"], message, latest["messages"])
                    own = latest["agent_sessions"][message["sender"]]
                    own.update(memory=memory, last_seq=message["seq"], turns=own["turns"]+1)
                    latest["cursor"] += 1
                latest["review_progress"]["phase"] = "broadcast"
                api.save_run(latest)
                await asyncio.sleep(0)
            run = api.get_run(session_id)
            run["review_progress"]["phase"] = "review"
            api.save_run(run)
            review = await api.review_dialogue(template, run)
            for attempt in range(2):
                run = api.get_run(session_id)
                if run["status"] != "running":
                    return
                run["quality_review"] = review["issues"]
                run["outcome_review"] = {"met": review["goal_met"], "reason": review["goal_reason"]}
                api.save_run(run)
                if not review["issues"] and review["goal_met"]:
                    break
                run["review_progress"].update(phase="repair", repair_rounds=attempt+1)
                api.save_run(run)
                issues = review["issues"] or [{"seq": len(run["messages"]), "reason": review["goal_reason"]}]
                revisions = await revise(writer_type, chat, api, session_id, template, run, issues, review["goal_reason"])
                run.setdefault("review_revisions", []).extend(revisions)
                run["review_progress"]["phase"] = "verify"
                rebuild_memories(run)
                api.save_run(run)
                review = await api.review_dialogue(template, run)
            run = api.get_run(session_id)
            if run["status"] != "running":
                return
            run["naturalness_report"] = naturalness_evidence(run["messages"])
            run["quality_review"] = review["issues"]
            run["outcome_review"] = {"met": review["goal_met"], "reason": review["goal_reason"]}
            run["errors"] = contract_errors(template["source"], {**run, "status": "completed"})
            run["errors"].extend(str(issue["reason"]) for issue in review["issues"])
            if not review["goal_met"]:
                run["errors"].append("坐席目标未完成：" + review["goal_reason"])
            run["status"] = "failed" if run["errors"] else "completed"
            if run["status"] == "completed":
                scope={**run,"cursor":0}
                apply_review_actions(scope,run["messages"],review.get("action_updates"),len(template["analysis"].get("workflow_steps",[])))
                apply_review_decisions(scope,run["messages"],review.get("decision_updates"))
                rebuild_memories(run)
                run["memory_reflections"] = normalize_reflections(run, [], review.get("memory_reflections"))
            run["review_progress"]["phase"] = "done"
            checkpoint_candidate(api, run, run['messages'], '最终程序检查结束；人工审阅另行记录')
            api.save_run(run)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        run = api.get_run(session_id)
        if run["status"] == "running":
            run["status"] = "failed"
            run["errors"] = [str(exc)[:1000]]
            api.save_run(run)


def contract_errors(source, run):
    # Account coverage is not the content objective; the cast is freely configured.
    errors=[error for error in validate_run(source, run) if not error.endswith("没有发言")]
    errors.extend(screenshot_errors(source, run))
    policy = run.get('config', {}).get('opening_policy')
    if run.get('config', {}).get('dialogue_flow_version', 1) >= 2:
        errors.extend(flow_errors(run.get('messages', []), source_literals(source), run.get('status') == 'completed'))
    if policy and policy.get('version', 1) == 1:
        literals = source_literals(source)
        for message in run.get('messages', []):
            if message['seq'] < policy['main_start'] and any(value in message['content'] for value in literals):
                errors.append(f"消息 {message['seq']} 在寒暄或了解群用途阶段提前公布入口、联系人或固定代码，应移到正题阶段")
    if str(run.get('config',{}).get('language','zh-CN')).startswith('zh'):
        left=set()
        messages=run.get('messages',[])
        for index,message in enumerate(messages):
            text=message['content'];role=message['sender'];seq=message['seq']
            if re.search(r'文本所称|对话中提到|本轮目标|公开说明的动作|不提前虚构',text):
                errors.append(f'消息 {seq} 出现制作/审阅口吻，请改为人物实际说话')
            if role in left and not re.search(r'回来了|又回来|刚回来',text):
                errors.append(f'消息 {seq} 的人物已离开，却没有说明回来就继续发言')
            if re.search(r'我先离开|我先走了|我先下了|先退出讨论',text):left.add(role)
            if re.search(r'回来了|又回来|刚回来',text):left.discard(role)
        for index in range(0,len(messages)-9,5):
            window=messages[index:index+10]
            waiting=[m for m in window if re.search(r'等(?:待)?(?:经理|管理员|回复|确认|处理)|先等回复',m['content'])]
            if len(waiting)>=4:
                errors.append(f"消息 {waiting[0]['seq']} 起十条内反复等待或确认同一结果，应展开不同具体内容")
    return errors


def rebuild_memories(run):
    for state in run["agent_sessions"].values():
        state.update(memory_events=[], turns=0, last_seq=None, memory="")
        state.pop("last_task_state", None)
    for message in run["messages"]:
        record_public_memory(run["agent_sessions"], message, run["messages"][:message["seq"]])
        state = run["agent_sessions"][message["sender"]]
        state.update(memory=message["content"], last_seq=message["seq"], turns=state["turns"]+1)


async def revise(writer_type, chat, api, session_id, template, run, issues, reason):
    """Rewrite an affected passage with ViMax, then validate it as one transaction."""
    messages = run["messages"]
    # Global missing-value errors need correction at the actual explanation, not
    # blindly at line one. The writer receives full chronology and source evidence.
    writer = writer_type(chat_model=RecordedChat(chat, api, session_id, "revision"))
    original = {m["seq"]:m for m in messages}
    request = writing_contract(template, run) + f"\n这是已有{run['config']['target_messages']}条草稿的编辑，保持完整总条数和事件顺序。\n"
    request += "问题：" + json.dumps(issues,ensure_ascii=False) + "\n审阅说明："+reason
    if run['config'].get('dialogue_flow_version', 1) >= 3:
        context = rolling_context(run, messages)
        request += '\n修订时保留分角色私有动机与接话约束：' + json.dumps({
            'private_by_owner': {role: {'personal_now': state['personal_now']}
                                 for role, state in context['private_by_owner'].items()},
            'threads': context['threads'], 'turn_selection': context['turn_selection'],
        }, ensure_ascii=False)
        request += ('这些人物处境、动机和关系设定不是已公开事实，只约束所属人物自己的选择；'
                    '其他人只能依据修订后该条之前已经公开的台词回应，不能读取他人私有动机，'
                    '也不能把原稿后段才出现的消息提前当作已知。')
    request += "\n原对话："+json.dumps([{k:m.get(k) for k in ['seq','sender','reply_to','delay','stage','type','image_code','image_mode','content']} for m in messages],ensure_ascii=False)
    request += '\n' + output_format(run)
    request += "\n在script数组内返回完整修订稿。允许调整问题段的发言人和接话关系；其他有效内容尽量保留。可以补入母本原值，但不能添加未知原值或改变业务含义。连续重复的问题要重写整段，不能只换近义词。修改旧话后须检查后续代词、个人决定、时间安排是否仍有前因；不要只修被点名的一句却留下后续悬空回应。"
    prompt = INDEPENDENT_WRITE_PROMPT if run['config'].get('creation_mode') == 'independent_random' else WRITE_PROMPT
    scenes = await writer.write_script_based_on_story(run.get('vimax_story',''),request,system_prompt=prompt)
    latest=api.get_run(session_id)
    run['vimax_calls']=latest.get('vimax_calls',[])
    run['vimax_raw']=latest.get('vimax_raw',{})
    if run['config'].get('dialogue_flow_version', 1) >= 4:
        run['vimax_samples'] = latest.get('vimax_samples', [])
    updated = decode_scenes(scenes,run)
    checkpoint_candidate(api, run, updated, '完整修订：' + reason)
    if run['config'].get('dialogue_flow_version', 1) >= 2:
        run['schedule'] = [{**slot, 'stage': updated[i]['stage']} for i, slot in enumerate(run['schedule'])]
    errors=contract_errors(template['source'],{**run,'messages':updated,'status':'completed'})
    if errors:
        raise ValueError('修订稿仍未通过：'+'；'.join(errors))
    run['messages']=updated
    return [{'seq':m['seq'],'before':original[m['seq']]['content'],'after':m['content'],
             'before_media': {k: original[m['seq']].get(k) for k in ('type','image_code','image_mode')},
             'after_media': {k: m.get(k) for k in ('type','image_code','image_mode')}}
            for m in updated if any(m.get(k) != original[m['seq']].get(k)
                                    for k in ('content','sender','reply_to','delay','stage','type','image_code','image_mode'))]
