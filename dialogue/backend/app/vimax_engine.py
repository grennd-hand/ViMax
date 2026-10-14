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

ROOT = Path(__file__).resolve().parents[3]
PLAN_PROMPT = """你是擅长写真实多人群聊的编剧。母本坐席内容是业务事实和引导目标，人物不是客服或审核员。只写精简的可执行事件线，不写电影故事、旁白、抽象主题、人物小传或逐句台词。先列12到18个互不重复的具体交流事件，每个事件写消息区间、谁有实际新问题、当下变化和与下一事件的接续。把人物的动机、个人时间安排、相互接话和实际操作穿插起来；不按人头轮流复述流程。业务信息只能来自母本，不编造页面功能、故障或提交许可。规划一次完整行动在最后10%篇幅才完成，前面围绕不同真实内容推进，不能提前完成后用等待凑数。结尾短，真实目标必须完成。"""
WRITE_PROMPT = """你是群聊剧本写手，写出来的每个人都像普通群友，坐席自然解释自己组织的活动。根据事件线、母本坐席原话和用户要求创作群聊。优先保持母本业务事实和精确原值、当前话题因果顺序、人物实际选择。故事或提示里的拟议事件不是已发生事实。人物只知道公开消息与自己的经历，不能提前知道链接/代码，也不替别人作决定。固定条数通过充分不同的内容和错开人物入场实现，不许反复确认、轮流汇报、提前收尾、空话填数；已解决的问题不再问，确需回忆时一句带过。坐席不讲审阅、公开记录、虚构结果、文本主张等制作口吻，也不连续提醒等待付款。参加者可以谈与参与相关的自己的工作、时间或使用手机习惯，这些个人情况不能变成新业务规则。核心目标在最后10%篇幅由愿意者实际完成，最后一条坐席简短收尾。仅返回下述JSON，script中每个场景字符串只包含NNN|角色|回复编号或0|台词行。原值中的括号保持不变，所有实际链接写完整，不使用图片占位符。\n{format_instructions}"""


def screenwriter_class():
    # Loading this one upstream module avoids importing the video/GPU stack.
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    spec = importlib.util.spec_from_file_location("vimax_dialogue_screenwriter", ROOT / "agents" / "screenwriter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Screenwriter


def decode_scenes(scenes: list[str], run: dict) -> list[dict]:
    messages = []
    roles = set(run["agent_sessions"])
    for scene in scenes:
        for line in scene.splitlines():
            if not re.match(r"^\s*\d{3}\|", line):
                continue
            number, speaker, reply, text = line.strip().split("|", 3)
            seq = int(number)
            reply_to = int(reply) if reply.strip() not in {"0", "-", "null"} else None
            if seq != len(messages) + 1:
                raise ValueError(f"消息编号不连续：期望{len(messages)+1}，实际{seq}")
            if speaker not in roles:
                raise ValueError(f"消息{seq}使用了未设置的人物：{speaker}")
            if reply_to is not None and not 0 < reply_to < seq:
                raise ValueError(f"消息{seq}回复了尚未出现的消息")
            if not text.strip() or len(text.strip()) > message_char_limit(run["config"].get("language"), seat=speaker == "坐席"):
                raise ValueError(f"消息{seq}为空或超出单条长度")
            declined = speaker != "坐席" and explicit_refusal(text, run["config"].get("language")) is True
            messages.append({"seq": seq, "sender": speaker, "content": text.strip(), "reply_to": reply_to,
                             "type": "文本", "delay": "1", "image_code": None, "image_mode": None,
                             "replace_date": None, "source_seq": None, "source_row": None,
                             "source_rows": [], "reply_source_seq": None, "stage": "自然群聊",
                             "reaction": "refuse" if declined else "continue", "task_state": "declined" if declined else "active", "performed_steps": []})
    if len(messages) != run["config"]["target_messages"]:
        raise ValueError(f"需要{run['config']['target_messages']}条消息，实际生成{len(messages)}条")
    if messages[0]["sender"] != "坐席" or messages[-1]["sender"] != "坐席":
        raise ValueError("第一条和最后一条需要由坐席发言")
    return messages


def writing_contract(template: dict, run: dict) -> str:
    target = run["config"]["target_messages"]
    source = template["source"]
    facts = [{"row": m["source_row"], "text": m["content"]} for m in source["messages"] if m["sender"] == "坐席"]
    context = {"goal": template["analysis"], "source_seat_evidence": facts,
               "protected_literals": source_literals(source),
               "roles": run["config"]["persona_cards"], "language": language_context(run["config"].get("language")),
               "past_experience_by_owner": {role:state.get("long_term_memory",[]) for role,state in run["agent_sessions"].items()},
               "additional_plot": run["config"].get("story_notes", "")}
    return (
        "这是基于Excel母本重新创作的群聊，不是影视对白。最高优先级是坐席内容的实际目的和source_seat_evidence。"
        "母本账号身份、数量、台词顺序均不需要沿用；只使用roles中这组新人物。"
        "不要虚构新业务规则、商品名称、页面布局、页面故障、奖励到账或外部审批结果。"
        "奖励、付款处理和到账时长只按活动方原说明转述，例如‘活动方说明是……，实际以处理结果为准’，不能保证到账，也不能把说明当作已验证事实。"
        "原文询问某个条件不等于已经确认该条件；例如有人问是否每天有任务，不能据此宣布每天一定有任务。"
        "坐席解释和引导方式可以完全改写，但不能改坐席说明中的必要事实、时间、顺序、链接、联系人和固定代码。"
        "protected_literals必须逐字复制，括号也是原值的一部分；例如【代码】不能改成不带括号的代码。"
        "入口第一次发布就必须写出完整实际链接，禁止只写活动链接冒号后留空。"
        "固定代码是坐席提供，不是页面生成；不添加联系人通过好友、确认材料后才能发送材料的新条件。"
        "参与者必须等坐席在公开对话中给出原值后再用，不能因为你看到了母本而提前知道。"
        "从知悉任务到自主操作、互动、截图、联系和提交按母本因果顺序发生，不能只说以后准备做。"
        "有人可以不参加，但其余愿意者应完成已确认目标；不能把群聊改成全员等待核实或停止参与。"
        "目标只需要一位愿意者完整走完母本流程。把这个人作为主要行动者；其他人按自己的兴趣接话、质疑、比较个人安排，不要让所有人逐项重演相同流程。"
        "主行动者每个动作只写一次，有真正的新情况才补充；其他愿意者可以一句合并说明，不能逐一排队汇报打开、点赞、截图、提交。"
        "保持人设、个人安排和已做动作一致，做过的不再重复做；质疑得到答案就继续或作决定。"
        "围绕不同的具体问题、动机和体验展开，人物会接话、打岔、犹豫、纠正自己，长短交错。"
        "不要让所有人轮流汇报同一步骤，不要反复复述风险或已解决的问题，不要新增未经母本支持的活动。"
        f"最终目标恰好{target}条群聊消息，第一条和最后一条由坐席说，前面不能提前结束全群。"
        "不要把原始账号人数或逐人发言覆盖当成目标；目标完成和自然接话优先。"
        "不能把主要动作早早做完后用几十条等待或重复确认填满条数；将不同真实话题、选择和行动贯穿全篇。"
        "文字消息不含图片素材，禁止用[已完成截图]之类假图片占位符；可以用文字说明已分享截图。"
        "不要让人物说‘不虚构结果’‘公开说明的动作’‘活动文本提到’等创作/审阅用语，直接像真人接话。"
        "参与者的私有判断只属于本人，其他人只能依据已公开的话回应。不要输出角色内心、制作解释或旁白。"
        "\n" + json.dumps(context, ensure_ascii=False)
    )


class RecordedChat:
    def __init__(self, chat, api, session_id, stage):
        self.chat, self.api, self.session_id, self.stage = chat, api, session_id, stage

    async def ainvoke(self, messages):
        began = time.monotonic()
        response = await self.chat.ainvoke(messages)
        run = self.api.get_run(self.session_id)
        if run["status"] != "running":
            raise asyncio.CancelledError()
        run.setdefault("vimax_calls", []).append({"stage": self.stage, "seconds": round(time.monotonic()-began, 2)})
        run.setdefault("vimax_raw", {})[self.stage] = response.content
        self.api.save_run(run)
        return response


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
        async with httpx.AsyncClient(trust_env=False, timeout=90) as http_client:
            base,key,model_name = api.model_settings()
            chat = ChatOpenAI(model=run["config"].get("dialogue_model") or model_name,
                              api_key=key, base_url=base,
                              streaming=True, max_retries=0, max_tokens=16000, timeout=90,
                              reasoning_effort="low" if re.fullmatch(r"gpt-(?:5|6)(?:\.\d+)?",run["config"].get("dialogue_model") or model_name) else None, http_async_client=http_client)
            writer_type = screenwriter_class()
            contract = writing_contract(template, run)
            if not run.get("vimax_story"):
                run["review_progress"] = {"phase": "planning", "repair_rounds": 0}
                api.save_run(run)
                writer = writer_type(chat_model=RecordedChat(chat, api, session_id, "plan"))
                story = await writer.develop_story("根据坐席母本目的，设计群聊事件线。", contract +
                    "只输出精简事件大纲，不写完整剧本；每个事件提供不同的交流内容、变化与目标贡献；不要按人头轮流报告。",system_prompt=PLAN_PROMPT)
                run = api.get_run(session_id)
                run["vimax_story"] = story
                api.save_run(run)
            if not run.get("vimax_draft"):
                writer = writer_type(chat_model=RecordedChat(chat, api, session_id, "write"))
                run["review_progress"]["phase"] = "writing"
                api.save_run(run)
                scenes = await writer.write_script_based_on_story(run["vimax_story"], contract +
                    "\n输出格式：script数组每个元素是一段群聊字符串，内容只能是编号台词行，"
                    "例如001|坐席|0|大家晚上好。\n002|客户1|1|我想先问一下怎么参加？\n"
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
    request += "\n原对话："+json.dumps([{k:m[k] for k in ['seq','sender','reply_to','content']} for m in messages],ensure_ascii=False)
    request += "\n在script数组内返回完整修订稿，编号格式NNN|人物|回复编号或0|台词。允许调整问题段的发言人和接话关系；其他有效内容尽量保留。可以补入母本原值，但不能添加未知原值或改变业务含义。连续重复的问题要重写整段，不能只换近义词。"
    scenes = await writer.write_script_based_on_story(run.get('vimax_story',''),request,system_prompt=WRITE_PROMPT)
    latest=api.get_run(session_id)
    run['vimax_calls']=latest.get('vimax_calls',[])
    run['vimax_raw']=latest.get('vimax_raw',{})
    updated = decode_scenes(scenes,run)
    errors=contract_errors(template['source'],{**run,'messages':updated,'status':'completed'})
    if errors:
        raise ValueError('修订稿仍未通过：'+'；'.join(errors))
    run['messages']=updated
    return [{'seq':m['seq'],'before':original[m['seq']]['content'],'after':m['content']} for m in updated if m['content']!=original[m['seq']]['content'] or m['sender']!=original[m['seq']]['sender']]
