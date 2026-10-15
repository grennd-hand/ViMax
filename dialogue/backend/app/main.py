from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .core import BRACKET_RE, META_RE, URL_RE, analyze, build_schedule, codes_in, contradicts_group_screenshot, export_xlsx, group_screenshot_required, min_total, protected_values, read_source, source_literals, unsupported_code_origin, unsupported_contact_acceptance_wait, unsupported_global_verification_halt, unsupported_page_detail, validate_run
from .dialogue_quality import closes_group, style_findings
from .locale import language_context, message_char_limit, normalize_language
from .persona_library import default_cards, default_entries, persona_library
from .opening import opening_policy, opening_requirement, opening_schedule
from .conversation_flow import naturalness_evidence
from .model_stream import stream_model_content
from .persona_memory import record_public_memory
from .memory_store import SQLiteMemoryStore, bind_characters, init_memory_schema, normalize_reflections, reflection_due, sync_accepted_memory
from .memory_cognition import prepare_memory_context
from .scene_memory import apply_review_actions, apply_review_decisions, apply_review_progress, decision_ledger, scene_candidates, scene_ledger
from .story_arc import fallback_story_arc, normalize_story_arc, normalize_story_topics, source_topic_arc
from .team import _public_role_status, _task_state, generate_team_batch, goal_requires_action

APP_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("SCRIPT_GENERATOR_DATA_DIR", APP_DIR.parents[0] / ".vimax" / "dialogue"))
DB_PATH = DATA_DIR / "app.sqlite3"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="剧本对话模拟器")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
tasks: dict[str, asyncio.Task] = {}
_reasoning_unsupported: set[tuple[str, str]] = set()
_run_model_client: ContextVar[httpx.AsyncClient | None] = ContextVar("run_model_client", default=None)
_run_reasoning_effort: ContextVar[str | None] = ContextVar("run_reasoning_effort", default=None)


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_json TEXT NOT NULL, analysis_json TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, template_id TEXT NOT NULL, run_json TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS exports (session_id TEXT NOT NULL, format TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(session_id, format))")
        connection.execute("CREATE TABLE IF NOT EXISTS custom_personas (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, language TEXT NOT NULL, card_json TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)")
        if "version" not in {row["name"] for row in connection.execute("PRAGMA table_info(custom_personas)")}:
            connection.execute("ALTER TABLE custom_personas ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        init_memory_schema(connection)
        for row in connection.execute("SELECT id, run_json FROM sessions"):
            run = json.loads(row["run_json"])
            changed = "next_reaction" in run
            run.pop("next_reaction", None)
            if run.get("status") == "running":
                run["status"] = "paused"
                run["pause_reason"] = "服务重启，模拟已暂停。检查后可继续。"
                changed = True
            if changed:
                connection.execute("UPDATE sessions SET run_json=? WHERE id=?", (json.dumps(run, ensure_ascii=False), row["id"]))


init_db()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def put_template(template_id: str, filename: str, source: dict, analysis: dict) -> None:
    with db() as connection:
        connection.execute("INSERT INTO templates VALUES (?, ?, ?, ?, ?)",
                           (template_id, filename, json.dumps(source, ensure_ascii=False), json.dumps(analysis, ensure_ascii=False), now()))


def get_template(template_id: str) -> dict:
    with db() as connection:
        row = connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到母本")
    return {"id": row["id"], "filename": row["filename"], "source": json.loads(row["source_json"]),
            "analysis": json.loads(row["analysis_json"])}


def save_analysis(template_id: str, analysis: dict) -> None:
    with db() as connection:
        connection.execute("UPDATE templates SET analysis_json=? WHERE id=?", (json.dumps(analysis, ensure_ascii=False), template_id))


def get_run(session_id: str) -> dict:
    with db() as connection:
        row = connection.execute("SELECT run_json FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到模拟")
    return json.loads(row["run_json"])


def save_run(run: dict) -> None:
    for message in run.get("messages", []):
        evidence = message.get("action_evidence")
        if evidence and evidence.get("quote") != message.get("content"):
            message["performed_steps"] = []
            message.pop("action_evidence", None)
    run["revision"] = run.get("revision", 0) + 1
    with db() as connection:
        sync_accepted_memory(connection, run)
        connection.execute("UPDATE sessions SET run_json=? WHERE id=?", (json.dumps(run, ensure_ascii=False), run["id"]))

    if run.get("vimax_project_id"):
        from agent_runtime.session_index import SessionIndex
        index = SessionIndex(APP_DIR.parent)
        index.update_stage(run["vimax_project_id"], run["status"], f"群聊 {run['cursor']}/{run['config']['target_messages']} 条")
        if run["status"] == "completed":
            folder = index.working_dir(run["vimax_project_id"]) / "dialogue"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "messages.json").write_text(json.dumps(run["messages"], ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "messages.md").write_text("\n\n".join(f"{m['seq']} **{m['sender']}**：{m['content']}" for m in run["messages"]), encoding="utf-8")
            (folder / "messages.xlsx").write_bytes(export_xlsx(run))


def create_run(run: dict) -> None:
    import sys
    root = APP_DIR.parent
    if str(root) not in sys.path: sys.path.insert(0, str(root))
    from agent_runtime.session_index import SessionIndex
    run["vimax_project_id"] = "dialogue-" + run["id"]
    SessionIndex(root).create(session_id=run["vimax_project_id"], project_name="母本群聊", idea=get_template(run["template_id"])["analysis"]["primary_goal"], style="dialogue")
    with db() as connection:
        bind_characters(connection, run)
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                           (run["id"], run["template_id"], json.dumps(run, ensure_ascii=False), now()))


def cached_story_arc(run: dict) -> list[dict] | None:
    """Reuse a source-backed plot only for the same source, cast, language and persona snapshot."""
    keys = ("target_messages", "customer_count", "language", "story_notes", "persona_cards")
    with db() as connection:
        rows = connection.execute("SELECT run_json FROM sessions WHERE template_id=? AND id<>? "
                                  "ORDER BY created_at DESC LIMIT 30", (run["template_id"], run["id"])).fetchall()
    for row in rows:
        previous = json.loads(row["run_json"])
        if (previous.get("story_arc_version", 0) < 3 or previous.get("status") != "completed"
                or previous.get("errors") or previous.get("quality_review") or previous.get("story_extensions")):
            continue
        if previous.get("story_arc_source") not in {"model-topics", "cached-model-topics"}:
            continue
        if previous.get("goal_revision", 0) != run.get("goal_revision", 0):
            continue
        if any(previous.get("config", {}).get(key) != run.get("config", {}).get(key) for key in keys):
            continue
        arc = previous.get("story_arc")
        if isinstance(arc, list) and len(arc) >= 20:
            return arc
    return None


def model_settings() -> tuple[str, str, str]:
    base = os.getenv("MODEL_API_BASE_URL", "").rstrip("/")
    key = os.getenv("MODEL_API_KEY", "")
    name = os.getenv("MODEL_NAME", "")
    config_file = APP_DIR.parent / "configs" / "agent.local.yaml"
    if config_file.is_file():
        import yaml
        local = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        selected = local.get("llm", {})
        configured_base = str(selected.get("base_url") or "").rstrip("/")
        configured_key = str(selected.get("api_key") or "")
        if configured_base:
            if configured_base != base and not configured_key:
                raise RuntimeError("切换模型接口地址时，请在设置中填写该接口的密钥")
            base = configured_base
        key = configured_key or key
        name = str(selected.get("model") or name)
    if not base or not name:
        raise RuntimeError("未配置 MODEL_API_BASE_URL 和 MODEL_NAME；无法运行 Agent 模拟")
    return base, key, name


def dialogue_repair_model(config: dict) -> str | None:
    return (os.getenv("MODEL_REPAIR_NAME") or config.get("dialogue_model")
            or os.getenv("MODEL_DIALOGUE_NAME") or os.getenv("MODEL_NAME") or None)


def model_reasoning_effort(config: dict | None = None) -> str | None:
    if config and config.get("reasoning_effort"):
        return config["reasoning_effort"]
    effort = os.getenv("MODEL_REASONING_EFFORT", "").strip()
    config_file = APP_DIR.parent / "configs" / "agent.local.yaml"
    if config_file.is_file():
        import yaml
        local = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        llm = local.get("llm", {})
        if "reasoning_effort" in llm:
            effort = str(llm.get("reasoning_effort") or "").strip()
    if effort and effort not in {"low", "medium", "high"}:
        raise ValueError("推理强度只能选择低、中或高")
    return effort or None


async def model_json(system: str, payload: dict, *, temperature: float = 0.7, model_name: str | None = None,
                     call_budget_seconds: float | None = None, stream: bool = False) -> dict:
    base, key, name = model_settings()
    name = model_name or name
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    models = [name]
    fallback = os.getenv("MODEL_FALLBACK_NAME", "")
    if fallback and fallback != name:
        models.append(fallback)
    failure = "模型接口没有返回有效结果"
    deadline = time.monotonic() + (call_budget_seconds or float(os.getenv("MODEL_CALL_BUDGET_SECONDS", "90")))
    request_timeout = max(float(os.getenv("MODEL_TIMEOUT_SECONDS", "60")), min(call_budget_seconds or 0, 120))
    client = _run_model_client.get()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "60")), trust_env=False)
    try:
        for attempt in range(2):
            for candidate in models:
                body = {"model": candidate, "temperature": temperature,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
                explicit_effort = _run_reasoning_effort.get() or model_reasoning_effort()
                effort = explicit_effort or os.getenv("MODEL_SCENE_REASONING_EFFORT", "low").strip()
                if explicit_effort or (payload.get("scene_draft") and effort
                        and re.fullmatch(r"gpt-(?:5|6)(?:\.\d+)?", candidate)
                        and (base, candidate) not in _reasoning_unsupported):
                    body["reasoning_effort"] = effort
                remaining = deadline - time.monotonic()
                if remaining <= 0.01:
                    raise RuntimeError(f"模型接口超过总等待上限：{failure}")
                try:
                    if stream:
                        response, content = await stream_model_content(
                            client, f"{base}/chat/completions", headers=headers, body=body, deadline=deadline)
                    else:
                        response = await client.post(f"{base}/chat/completions", headers=headers, json=body,
                                                     timeout=min(request_timeout, remaining))
                    if response.status_code == 400 and "reasoning_effort" in body:
                        try:
                            error = response.json().get("error", {})
                        except (ValueError, AttributeError):
                            error = {}
                        detail = str(error)
                        if "reasoning_effort" in detail and re.search(r"unsupported|not supported|unknown|unrecognized|invalid", detail, re.I):
                            if explicit_effort:
                                raise RuntimeError("模型接口不支持所选推理强度，未自动降低强度")
                            _reasoning_unsupported.add((base, candidate))
                            body.pop("reasoning_effort")
                            if stream:
                                response, content = await stream_model_content(
                                    client, f"{base}/chat/completions", headers=headers, body=body, deadline=deadline)
                            else:
                                response = await client.post(f"{base}/chat/completions", headers=headers, json=body,
                                                             timeout=max(.01, min(request_timeout, deadline - time.monotonic())))
                    response.raise_for_status()
                    if not stream:
                        content = response.json()["choices"][0]["message"]["content"]
                    if isinstance(content, list):
                        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                    content = str(content).strip()
                    if content.startswith("```"):
                        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
                    try:
                        result = json.loads(content)
                    except json.JSONDecodeError:
                        start = content.find("{")
                        if start < 0:
                            raise
                        result = json.JSONDecoder().raw_decode(content[start:])[0]
                    if isinstance(result, list) and (payload.get("story_plan") or payload.get("story_plan_revision")):
                        result = {"beats": result}
                    if isinstance(result, list) and payload.get("scene_draft"):
                        result = {"turns": result}
                    if not isinstance(result, dict):
                        raise ValueError("模型未返回 JSON 对象")
                    return result
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    failure = f"模型 {candidate} 返回 HTTP {status}"
                    if status not in {429, 500, 502, 503, 504}:
                        raise RuntimeError(failure) from exc
                except (httpx.TimeoutException, TimeoutError):
                    failure = f"模型 {candidate} 请求超时"
                except (httpx.TransportError, json.JSONDecodeError, KeyError, IndexError, ValueError):
                    failure = f"模型 {candidate} 连接中断或返回格式无效"
            if attempt == 0 and deadline - time.monotonic() > 1:
                await asyncio.sleep(1)
    finally:
        if owns_client:
            await client.aclose()
    raise RuntimeError(f"模型接口连续失败：{failure}")


async def propose_goal(source: dict) -> dict:
    fallback = analyze(source)
    try:
        sampled = [{"row": m["source_row"], "sender": m["sender"], "content": (m["content"] or "")[:300]}
                   for m in source["messages"] if m["sender"] == "坐席" and m["content"]][:65]
        proposed = await model_json(
            "根据上传母本推断对话的目标。只返回 JSON 对象：goal_type、primary_goal、secondary_goals(字符串数组)、completion_actions(字符串数组)、workflow_steps(按先后顺序的字符串数组)、evidence_rows(Excel 行号整数数组)。引用原文作为依据；把剧本中的奖励和付款说法当作文本主张，不要认定为已验证事实。",
            {"messages": sampled}, temperature=0.2, model_name=os.getenv("MODEL_ANALYSIS_NAME"))
        valid_rows = {m["source_row"] for m in source["messages"]}
        if not isinstance(proposed.get("primary_goal"), str) or not proposed["primary_goal"].strip():
            return fallback
        fallback.update({
            "goal_type": str(proposed.get("goal_type") or fallback["goal_type"]),
            "primary_goal": proposed["primary_goal"].strip(),
            "secondary_goals": [str(x) for x in proposed.get("secondary_goals", []) if str(x).strip()][:8],
            "completion_actions": [str(x) for x in proposed.get("completion_actions", []) if str(x).strip()][:12],
            "workflow_steps": [str(x) for x in proposed.get("workflow_steps", []) if str(x).strip()][:12] or fallback["workflow_steps"],
            "evidence_rows": [int(x) for x in proposed.get("evidence_rows", []) if str(x).isdigit() and int(x) in valid_rows][:20],
        })
        fallback["analysis_method"] = "model"
    except (RuntimeError, ValueError, KeyError, TypeError, httpx.HTTPError, json.JSONDecodeError):
        fallback["analysis_method"] = "rules"
    return fallback


class GoalConfirmation(BaseModel):
    goal_type: str = Field(min_length=1)
    primary_goal: str = Field(min_length=1)
    secondary_goals: list[str] = Field(default_factory=list)
    completion_actions: list[str] = Field(default_factory=list)
    workflow_steps: list[str] | None = None


class PersonaCard(BaseModel):
    identity: str = Field(default="", max_length=180)
    priority: str = Field(default="", max_length=180)
    decision_rule: str = Field(default="", max_length=180)
    voice: str = Field(default="", max_length=180)
    social_style: str = Field(default="", max_length=180)
    speech_habits: str = Field(default="", max_length=180)
    reaction_cues: str = Field(default="", max_length=300)
    speech_avoid: str = Field(default="", max_length=180)


class SavedPersona(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["seat", "participant"]
    language: str = "zh-CN"
    card: PersonaCard

    @field_validator("language")
    @classmethod
    def normalize_saved_language(cls, value: str) -> str:
        return normalize_language(value)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language(value)


class SimulationConfig(BaseModel):
    fast_scene_writer: bool = True
    template_id: str
    customer_count: int = Field(ge=1, le=20)
    target_messages: int = Field(ge=1, le=500)
    allow_early_end: bool = False
    personas: dict[str, str] = Field(default_factory=dict)
    persona_cards: dict[str, PersonaCard] = Field(default_factory=dict)
    persona_sources: dict[str, str] = Field(default_factory=dict)
    persona_versions: dict[str, int] = Field(default_factory=dict)
    story_notes: str = Field(default="", max_length=3000)
    story_world: str = Field(default="", max_length=100)
    memory_enabled: bool = True
    character_ids: dict[str, str] = Field(default_factory=dict)
    persona_strength: float = Field(default=0.85, ge=0, le=1)
    language: str = "zh-CN"
    dialogue_model: Literal["gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol"] | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language(value)


class PersonaRequest(BaseModel):
    customer_count: int = Field(ge=1, le=20)
    language: str = "zh-CN"

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language(value)


@app.get("/api/health")
def health() -> dict:
    try:
        base, key, name = model_settings()
        configured = bool(base and key and name)
    except RuntimeError:
        configured, key, name = False, "", ""
    return {"ok": True,
            "model_configured": configured,
            "api_key_loaded": bool(key),
            "model": name,
            "reasoning_effort": model_reasoning_effort(),
            "memory_backend": "sqlite",
            "embedding_model": os.getenv("MODEL_EMBEDDING_NAME", "text-embedding-3-small")}


@app.get("/api/character-memory")
def list_character_memory(story_world: str) -> dict:
    return {"story_world": story_world, "profiles": SQLiteMemoryStore(DB_PATH).profiles(story_world)}


@app.get("/api/persona-library")
def list_persona_library(language: str = "zh-CN") -> dict:
    try:
        code = normalize_language(language)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with db() as connection:
        saved = connection.execute("SELECT id, name, kind, card_json, version FROM custom_personas WHERE language=? ORDER BY created_at DESC", (code,)).fetchall()
    entries = persona_library(code) + [{"id": row["id"], "name": row["name"], "kind": row["kind"],
                                        "version": row["version"], "card": json.loads(row["card_json"]), "custom": True} for row in saved]
    return {"language": code, "entries": entries}


@app.post("/api/persona-library")
def save_persona(payload: SavedPersona) -> dict:
    if not payload.name.strip() or not payload.card.identity.strip():
        raise HTTPException(422, "请填写人格名称和稳定倾向")
    entry = {"id": "custom-" + uuid.uuid4().hex, "name": payload.name.strip(),
             "kind": payload.kind, "version": 1, "card": payload.card.model_dump(), "custom": True}
    with db() as connection:
        connection.execute("INSERT INTO custom_personas (id, name, kind, language, card_json, created_at, version) VALUES (?, ?, ?, ?, ?, ?, 1)",
                           (entry["id"], entry["name"], entry["kind"], payload.language,
                            json.dumps(entry["card"], ensure_ascii=False), now()))
    return entry


@app.delete("/api/persona-library/{persona_id}")
def delete_persona(persona_id: str) -> dict:
    if not persona_id.startswith("custom-"):
        raise HTTPException(403, "预置人格不能删除")
    with db() as connection:
        deleted = connection.execute("DELETE FROM custom_personas WHERE id=?", (persona_id,)).rowcount
    if not deleted:
        raise HTTPException(404, "找不到这张自定义人格")
    return {"deleted": persona_id}


@app.put("/api/persona-library/{persona_id}")
def update_persona(persona_id: str, payload: SavedPersona) -> dict:
    if not persona_id.startswith("custom-"):
        raise HTTPException(403, "预置人格不能修改")
    if not payload.name.strip() or not payload.card.identity.strip():
        raise HTTPException(422, "请填写人格名称和稳定倾向")
    entry = {"id": persona_id, "name": payload.name.strip(), "kind": payload.kind,
             "version": 1, "card": payload.card.model_dump(), "custom": True}
    with db() as connection:
        updated = connection.execute("UPDATE custom_personas SET name=?, kind=?, language=?, card_json=?, version=version+1 WHERE id=?",
                                     (entry["name"], entry["kind"], payload.language,
                                      json.dumps(entry["card"], ensure_ascii=False), persona_id)).rowcount
        if updated:
            entry["version"] = connection.execute("SELECT version FROM custom_personas WHERE id=?", (persona_id,)).fetchone()["version"]
    if not updated:
        raise HTTPException(404, "找不到这张自定义人格")
    return entry


@app.post("/api/templates/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "只支持 .xlsx 母本")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "母本不能超过 10 MB")
    try:
        source = read_source(data)
        source["original_accounts"] = source["accounts"]
        source["accounts"] = []
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    template_id = uuid.uuid4().hex
    analysis = await propose_goal(source)
    put_template(template_id, file.filename, source, analysis)
    return {"id": template_id, "filename": file.filename, "analysis": analysis,
            "evidence": [{"row": m["source_row"], "sender": m["sender"], "content": m["content"]}
                         for m in source["messages"] if m["source_row"] in analysis["evidence_rows"]],
            "stats": {"messages": len(source["messages"]), "seat_messages": sum(m["sender"] == "坐席" for m in source["messages"]),
                      "accounts": source["accounts"], "image_codes": sum(bool(m["image_code"]) for m in source["messages"])},
            "minimum_for_one_customer": min_total(source, 1),
            "minimum_by_customer_count": {str(count): min_total(source, count) for count in range(1, 21)}}


@app.get("/api/templates/{template_id}")
def template_detail(template_id: str) -> dict:
    template = get_template(template_id)
    return {**template, "minimum_for_one_customer": min_total(template["source"], 1),
            "minimum_by_customer_count": {str(count): min_total(template["source"], count) for count in range(1, 21)}}


@app.put("/api/templates/{template_id}/goal")
def confirm_goal(template_id: str, payload: GoalConfirmation) -> dict:
    template = get_template(template_id)
    analysis = template["analysis"]
    before = {key: analysis.get(key) for key in GoalConfirmation.model_fields}
    analysis.update(payload.model_dump(exclude_none=True))
    if before != {key: analysis.get(key) for key in GoalConfirmation.model_fields}:
        analysis["goal_revision"] = analysis.get("goal_revision", 0) + 1
    analysis["confirmed"] = True
    save_analysis(template_id, analysis)
    return analysis


def default_persona_cards(roles: list[str], language: str = "zh-CN") -> dict[str, dict[str, str]]:
    return default_cards(roles, language)


@app.post("/api/templates/{template_id}/personas")
async def suggest_personas(template_id: str, payload: PersonaRequest) -> dict:
    template = get_template(template_id)
    if not template["analysis"].get("confirmed"):
        raise HTTPException(409, "请先确认母本目标")
    roles = template["source"]["accounts"] + [f"客户{i + 1}" for i in range(payload.customer_count)]
    selected = default_entries(['坐席'] + roles, payload.language)
    cards = {role: dict(entry['card']) for role, entry in selected.items()}
    return {'personas': {role: card['identity'] for role, card in cards.items()},
            'persona_cards': cards, 'source': 'library',
            'persona_sources': {role: entry['id'] for role, entry in selected.items()},
            'persona_names': {role: entry['name'] for role, entry in selected.items()}}


@app.post("/api/simulations")
async def start_simulation(config: SimulationConfig) -> dict:
    template = get_template(config.template_id)
    if not template["analysis"].get("confirmed"):
        raise HTTPException(409, "请先确认母本目标")
    try:
        model_settings()
        schedule = build_schedule(template["source"], config.customer_count, config.target_messages)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    normalized_config = config.model_dump()
    normalized_config['opening_policy'] = opening_policy(config.target_messages)
    normalized_config['dialogue_flow_version'] = 4
    schedule = opening_schedule(schedule, normalized_config['opening_policy'])
    normalized_config["reasoning_effort"] = model_reasoning_effort(normalized_config)
    roles = ["坐席"] + template["source"]["accounts"] + [f"客户{i + 1}" for i in range(config.customer_count)]
    defaults = default_persona_cards(roles, config.language)
    for role, entry in default_entries(roles, config.language).items():
        if role not in config.persona_cards and role not in config.personas:
            normalized_config['persona_sources'][role] = entry['id']
            normalized_config['persona_versions'][role] = entry['version']
    normalized_config["persona_cards"] = {
        role: {field: str(normalized_config["persona_cards"].get(role, {}).get(field)
                          or (config.personas.get(role) if field == "identity" else None)
                          or defaults[role][field])
               for field in defaults[role]}
        for role in roles}
    run = {"id": uuid.uuid4().hex, "template_id": config.template_id, "config": normalized_config,
           "goal_revision": template["analysis"].get("goal_revision", 0),
           "goal_requires_action": goal_requires_action(template),
           "model_routes": {"dialogue": config.dialogue_model or os.getenv("MODEL_DIALOGUE_NAME") or os.getenv("MODEL_NAME"),
                            "director": os.getenv("MODEL_DIRECTOR_NAME") or config.dialogue_model or os.getenv("MODEL_DIALOGUE_NAME") or os.getenv("MODEL_NAME"),
                            "analysis": os.getenv("MODEL_ANALYSIS_NAME") or os.getenv("MODEL_NAME"),
                            "personas": os.getenv("MODEL_PERSONA_NAME") or os.getenv("MODEL_NAME"),
                            "review": os.getenv("MODEL_REVIEW_NAME") or None,
                            "repair": dialogue_repair_model(normalized_config),
                            "batch_review": os.getenv("MODEL_BATCH_REVIEW_NAME") or os.getenv("MODEL_REVIEW_NAME") or None,
                            "fallback": os.getenv("MODEL_FALLBACK_NAME") or None},
           "status": "running", "schedule": schedule, "cursor": 0, "messages": [], "pending": [],
           "entry_pacing_enabled": config.target_messages >= 80,
           "inactive_roles": [],
           "architecture": "agent_team", "runtime": "vimax_screenwriter_group_chat",
           "agent_sessions": {name: {"memory": "", "memory_events": [], "turns": 0, "last_seq": None}
                              for name in ["坐席"] + template["source"]["accounts"] +
                              [f"客户{i + 1}" for i in range(config.customer_count)]},
           "errors": [], "revision": 0, "created_at": now()}
    try:
        create_run(run)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    tasks[run["id"]] = asyncio.create_task(process_run(run["id"]))
    return {"id": run["id"], "status": run["status"]}


@app.get("/api/simulations/{session_id}")
def simulation_detail(session_id: str, view: Literal["full", "workspace"] = "full") -> dict:
    run = get_run(session_id)
    template = get_template(run["template_id"])
    source = template["source"]
    detail = {**run, "public_decisions": decision_ledger(run), "scene_progress": scene_ledger(run),
              "validation": validate_run(source, run) if run["status"] in {"completed", "early_end"} else []}
    if view == "workspace":
        fields = {"id", "template_id", "status", "messages", "config", "revision", "cursor", "errors",
                  "version_record", "publication", "program_review_deferred",
                  "pause_reason", "outcome_review", "review_progress", "agent_sessions", "public_decisions",
                  "scene_progress", "validation", "vimax_events", "story_arc", "story_arc_source",
                  "memory_status", "memory_reflections"}
        detail = {key: value for key, value in detail.items() if key in fields}
        detail["schedule_length"] = len(run.get("schedule", []))
        detail["template"] = {**template, "minimum_for_one_customer": min_total(source, 1),
                              "minimum_by_customer_count": {str(count): min_total(source, count) for count in range(1, 21)}}
    return detail


@app.get("/api/simulations/{session_id}/events")
async def events(session_id: str) -> StreamingResponse:
    get_run(session_id)

    async def stream():
        revision = -1
        while True:
            run = get_run(session_id)
            if run["revision"] != revision:
                revision = run["revision"]
                visible = {**run, "public_decisions": decision_ledger(run), "scene_progress": scene_ledger(run)}
                yield f"data: {json.dumps(visible, ensure_ascii=False)}\n\n"
            if run["status"] in {"completed", "early_end", "failed"}:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/simulations/{session_id}/pause")
async def pause(session_id: str) -> dict:
    run = get_run(session_id)
    if run["status"] != "running":
        raise HTTPException(409, "当前模拟无法暂停")
    run["status"] = "paused"
    save_run(run)
    previous = tasks.get(session_id)
    if previous and not previous.done():
        previous.cancel()
        try:
            await previous
        except asyncio.CancelledError:
            pass
    return {"status": run["status"]}


@app.post("/api/simulations/{session_id}/resume")
async def resume(session_id: str) -> dict:
    run = get_run(session_id)
    if run.get('publication', {}).get('read_only'):
        raise HTTPException(409, '这是已归档版本，仅供回看。请在新版本中修改，历史原稿保留。')
    resumable_failure = run["status"] == "failed"
    if run["status"] != "paused" and not resumable_failure:
        raise HTTPException(409, "当前模拟无法继续")
    run["status"] = "running"
    run['schedule'] = opening_schedule(run['schedule'], run['config'].get('opening_policy'))
    run.setdefault("model_routes", {})["repair"] = dialogue_repair_model(run["config"])
    if resumable_failure:
        run["recovery_guidance"] = "；".join(str(error) for error in run.get("errors", [])[:2])[:600]
        run["errors"] = []
        run["pending"] = []
    run["transition_override"] = bool(run.get("pause_reason"))
    run["pause_reason"] = None
    save_run(run)
    previous = tasks.get(session_id)
    if previous and not previous.done():
        previous.cancel()
    tasks[session_id] = asyncio.create_task(process_run(session_id))
    return {"status": run["status"]}


@app.post("/api/simulations/{session_id}/retry")
async def retry(session_id: str) -> dict:
    run = get_run(session_id)
    config = SimulationConfig(**run["config"])
    return await start_simulation(config)


@app.get("/api/simulations/{session_id}/export/{format}")
def export(session_id: str, format: str) -> Response:
    run = get_run(session_id)
    if run["status"] not in {"completed", "early_end"}:
        raise HTTPException(409, "模拟尚未完成")
    template = get_template(run["template_id"])
    errors = validate_run(template["source"], run)
    if errors:
        raise HTTPException(409, {"validation_errors": errors})
    if format == "xlsx":
        body = export_xlsx(run)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif format == "json":
        body = json.dumps({"template_id": run["template_id"], "goal": template["analysis"], "config": run["config"],
                           "models": run.get("model_routes"), "architecture": run.get("architecture"),
                           "runtime": run.get("runtime"),
                           "agent_sessions": run.get("agent_sessions"), "status": run["status"], "messages": run["messages"],
                           "public_decisions": decision_ledger(run), "scene_progress": scene_ledger(run),
                           "character_bindings": run.get("character_bindings", {}),
                           "memory_reflections": run.get("memory_reflections", []), "memory_status": run.get("memory_status", {}),
                           "review_revisions": run.get("review_revisions", []), "validation": []}, ensure_ascii=False, indent=2).encode("utf-8")
        media = "application/json"
    else:
        raise HTTPException(404, "不支持此导出格式")
    with db() as connection:
        connection.execute("INSERT OR REPLACE INTO exports VALUES (?, ?, ?)", (session_id, format, now()))
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="simulation-{session_id[:8]}.{format}"'})


async def process_run(session_id: str) -> None:
    async with httpx.AsyncClient(timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "60")), trust_env=False) as client:
        token = _run_model_client.set(client)
        effort_token = _run_reasoning_effort.set(model_reasoning_effort(get_run(session_id)["config"]))
        try:
            await _process_run(session_id)
        finally:
            _run_model_client.reset(token)
            _run_reasoning_effort.reset(effort_token)






async def _process_run(session_id: str) -> None:
    import sys
    from .vimax_engine import process
    await process(session_id, sys.modules[__name__])


STAGE_GUIDANCE = {
    "自然群聊": "角色按当前对话自然接话，介绍、给入口、尝试、反馈和答疑可以穿插。坐席在有人需要时给出原值；参与者按各自状态推进。只保留入口先于打开、互动先于截图，以及原表明确规定的提交顺序。",
    "开场": "坐席说明这是哪类群聊和交流目的。参与者自然入群，最多两人提出不同问题；其他人表达自己的关注点或是否愿意听说明。下一位坐席统一回答，不让账号代答，也不提前宣讲操作步骤。",
    "了解任务": "坐席简要说明母本明确给出的任务和参与条件。安排一两个不同的关键问题，其余参与者接话、表达选择或准备下一步；母本没有说明的条件如实说不清楚，不把全群带入等待外部核验。",
    "操作说明": "坐席给出操作顺序、准确链接、联系人和代码的用途；参与者确认自己不明白的步骤并推进到尝试。不要把入口改写为仅供核验的链接，也不要反复宣读完整流程。",
    "尝试与反馈": "角色在本地模拟中尝试已经说明的步骤，描述自己看到了什么、做到了哪一步或遇到什么问题；坐席针对反馈帮助排查。同一注意事项在群里说过后不再重复，优先报告新的实际状态。不要宣称真实付款已经到账。",
    "处理疑问": "围绕尝试过程中留下的具体问题做问答，解决一个问题后推进到下一个；优先处理实际遇到的障碍，不连续引入假设性问题。没有依据时不要保证奖励到账。",
    "收束": "坐席按既定操作顺序简短总结，并对每个人的当前状态给出明确结论：无法完成者本轮到此结束，不提交；已提交者保留记录并自行核对后续回复。不要只说继续等待，也不要再重复所有疑问和风险提醒。",
}

STAGE_FOCUS = {
    "自然群聊": ["回应最近的问题并推进一位角色的实际决定", "按需要提供入口或解释步骤", "让不同角色反馈各自进度，不复述已有答案", "对已提交与未完成的状态分别处理"],
    "开场": ["说明群聊用途，让不同参与者自然进入讨论"],
    "了解任务": ["说清任务是什么和适合谁", "明确参与条件与大家最关心的限制，然后进入操作"],
    "操作说明": ["给出入口并说明如何找到指定任务", "解释如何完成商品互动", "解释如何保存证明材料", "解释联系对象、原代码与提交顺序"],
    "尝试与反馈": ["不同参与者打开入口并尝试找到任务", "有人完成互动并核对页面状态，也有人遇到具体问题", "参与者整理证明材料并按顺序联系指定人员提交", "反馈提交后的状态、待核验事项和下一步，不宣称奖励已到账"],
    "处理疑问": ["回答尝试过程中尚未解决的具体问题", "处理一个不同于前面的问题或异常情况", "确认接下来的可执行动作"],
    "收束": ["按原顺序简短回顾", "对未完成者明确本轮结束及重新尝试的条件，对已提交者明确后续核对动作", "最后由坐席给出具体收束决定，不以空泛等待作结"],
}


async def review_dialogue(template: dict, run: dict) -> dict:
    result = await model_json(
        "审阅一份模拟群聊的对话逻辑与群聊读感。检查说话人身份错乱、答非所问、前后矛盾、严重重复、步骤在解释前就被当作完成、"
        "客户尚未明确退出却被当作退出，以及必要因果关系被颠倒（如入口公布前已打开、点赞前已截图）。一个具体问题在坐席回答后，若后续三条以上只是不同人重复确认同一答案，"
        "也必须标出开始原地打转的消息；除非出现新的事实或新的疑问。多个角色接连附加相同的假设性提醒、没有推进实际状态，也要指出。"
        "固定代码由坐席提供，若对话声称页面生成或显示该代码，也要指出。"
        "若前面的消息提前提到后面才首次发生的补图、代码更正或设备故障，也要指出具体编号。"
        "对照 source_seat_evidence 检查坐席对母本内容的陈述：原文已给出的步骤或说法不能被坐席说成完全未写明；"
        "坐席不能擅自将母本活动时间或备选时段改成另一个时间；参与者的个人安排应与实际活动选项区分。"
        "原文关于奖励或付款的说法只能作为未核实主张转述，不能当作已验证结果。"
        "若 submission_order_policy 要求截图发群，不能说无需在群里提交截图。"
        "还要按 output_language 检查长对话里不同角色是否反复共用同一种汇报句式，是否连续把每句话写成‘已完成、下一步、等待确认’，"
        "以及是否多人轮流重复同一未知条件或风险提醒；保留必要提醒，但不要让它占据整段对话。"
        "以及角色口吻是否逐渐混成同一个人。style_findings 是带原句的候选证据，须结合上下文判断；确实有不同观察或自然回应时不要误报。"
        "逐句检查表意：口语省略不能丢掉理解所需的动作、对象、关系或前因；先/也/又/还要有参照，慢/那个/这样等要能从已公开上下文判断指什么。"
        "若必须替角色补出没说过的一段经历才能读懂，引用具体原句标出；正常省略和通顺口语不要求改成书面解释。"
        "跨全文检查同一生活小事是否被延长为大量道具状态汇报、同一个笑点是否反复解释；"
        "同样检查换话题后的语义重复：反复讲个人边界、如何安排空闲、客气话该问清、一次尝试不等于长期承诺，"
        "即便每人用了不同例子，已经得到同一结论又无新信息仍应标出；不要把群聊写成理解态度的讨论课。"
        "热饭到收拾餐盒、挑照片到擦杯子等主题即使每条不同，也不能仅靠微小动作变化撑篇幅。"
        "结合persona_cards检查不同人物的关注点、判断和接话意愿，不用名人腔或口头禅衡量人格。"
        "检查是否每次提问都被群友夸赞、每句玩笑都获捧场、各人操作汇报可以互换署名；引用两处以上证据再判断模式。"
        "检查故事接龙：前一人说钥匙、后一人接遥控器、再接书架和菜谱，即便每段故事不同，"
        "若只是抓前一句名词轮流提供自己经历、没有回应对方实际需要或判断，也应指出成段编排感。"
        "初入群陌生人的熟悉须由公开互动逐步建立，不应迅速变成人人分享完整私事的闲聊会。"
        "同样检查日常话题的重复闭环：某人已选定后，多个角色仍轮流论证同一选择理由、反复投同一票，"
        "若没有新增实际问题或不同信息，应指明整段重复；不要把一个人的私事变成全群共同筹备项目。"
        "别人未在公开对话提供的社区通知、原表等个人资料，其他角色不能声称自己看过或只漏看后半句。"
        "幽默卡不代表必须持续造梗；连续拟人比喻或固定捧哏须引用多处证据，不因偶尔一句玩笑报错。"
        "检查参加/拒绝声明、今晚安排和告别等是否有当时的前因，不因它能回流业务就放过悬空回应。"
        "opening_contract规定寒暄再了解群用途，转场由实际交流产生，不固定条号。"
        "对新版rolling_conversation检查安排感：多个群友逐项见证同一个人操作、把动作拆成准备做正在做已做、"
        "角色反复解释自主性或互相称赞理性、已经解决的问题无新背景又重问，都应引用具体台词标出。"
        "日常生活同样检查连续性：选了热饭不能未交代就改成煮面或干拌，已经端回座位不能又在原处等着端回；"
        "收拾完的东西不能又变成没收拾。区分短暂回忆与当前动作，后续变化须有公开依据。"
        "日常话题可以与业务交错，人物可以不接话或漏看，不必每句推进任务；任务完成后继续其他话题合法。"
        "不得把原值改掉、伪造外部到账来换取自然；也不能只靠加口头禅或无意义短句凑数。"
        "检查是否第一条就宣讲目的、没有暖场直接操作、所有人轮流报到，或在坐席解释后仍反复问群用途。"
        "正常寒暄、玩笑和人物相互熟悉属于有效交流，不因没有业务动作而报错；不强迫所有人都开口。"
        "public_decisions 是带原话的个人选择。区分活动时段与个人准备时间等不同事项；同一事项前后改变须有具体原因。"
        "坐席在最后一条前告别全群或宣布结束（包括 See you...）也应指出。"
        "普通说明、母本引文和历史经历须转述为 output_language，台词夹入其他语言的完整普通句子应指出；链接、联系人、代码及人物称呼保留原值。"
        "不要评价母本业务主张是否真实，也不要因正常质疑而报错。另判断已确认的 goal 在对话中是否实际达成；"
        "completion_actions 是参考步骤，可能包含外部审批或等待，不能自动把每一项都当作 goal 的必要条件。"
        "若 goal 是完成操作并提交申请，已有参与者实际提交所需材料即可算达到该目标，不要求经理确认或奖励到账；"
        "只有 goal 明确要求外部结果时才要求相应结果。只提及入口、联系人、代码或假设会去做，不算完成；"
        "若所有人明确放弃或坐席最后决定停止，goal_met 为 false。"
        "同时提取本人实际完成的动作action_updates，每项seq、quote（逐字原句）、steps（workflow_steps零基编号）；计划或别人的动作不算。"
        "提取实际个人选择decision_updates，每项role、subject、summary、evidence_seq；必须是本人明确说出的选择。"
        "只返回 JSON：issues 为数组，每项含 seq（消息编号）和 reason（简短中文，引用具体重复处）；"
        "goal_met 为布尔值，goal_reason 为简短中文事实依据；没有明显逻辑或风格问题时 issues 为空数组。"
        "memory_enabled 为 true 且 issues 为空时，顺便从当前最终台词提取 memory_reflections：每项含 role、"
        "kind（preference 或 relationship）、insight、peer（关系对象，否则 null）、evidence_seq（台词编号数组）。"
        "insight 用 output_language 写一句本人实际表达过的偏好或对他人的认识，关系需有具体互动。"
        "不得虚构、改变稳定人格或沿用本轮已提交、已打开、暂停等操作状态；没有依据时为空数组。",
        {"goal": template["analysis"].get("primary_goal"),
         "opening_contract": opening_requirement(run) if run['config'].get('opening_policy') else None,
         "public_decisions": decision_ledger(run),
         "completion_actions": template["analysis"].get("completion_actions"),
         "persona_cards": run["config"].get("persona_cards", {}),
         "private_character_intents": run.get("person_situations", {}) if run['config'].get('dialogue_flow_version', 1) >= 3 else {},
         "rolling_conversation": run["config"].get("dialogue_flow_version", 1) >= 2,
         "naturalness_evidence": naturalness_evidence(run["messages"]),
         "memory_enabled": bool(run["config"].get("memory_enabled") and run.get("character_bindings")),
         "source_seat_evidence": [{"source_row": m["source_row"], "content": str(m["content"])[:240]}
                                  for m in template["source"]["messages"]
                                  if m["sender"] == "坐席"][:64],
         "output_language": language_context(run["config"].get("language")),
         "style_findings": style_findings(run["messages"], run["config"].get("language"))[:12],
         "code_origin_rule": "固定代码由坐席提供，参与者将其与截图发给指定联系人；不要编造页面显示或生成该代码。",
         "submission_order_policy": "截图先发群，再连同代码发给经理" if group_screenshot_required(template["source"]) else "按母本流程提交",
         "media_policy": ("本版支持图片素材占位事件。type、image_code及image_mode为剧本的待补素材元数据，不是真实外部截图。"
                          "母本需截图发群时，要有该参与者自己的图片消息；不能只凭纯文本已发截图认定群内图片事件发生。"
                          "图片事件也必须在公开说明与点赞之后，素材码不能当作新的业务代码。"
                          if run['config'].get('dialogue_flow_version', 1) >= 4 else None),
         "workflow_steps": template["analysis"].get("workflow_steps") or analyze(template["source"])["workflow_steps"],
         "messages": [{"seq": m["seq"], "sender": m["sender"], "stage": m["stage"], "content": m["content"],
                       "reply_to": m.get("reply_to"), "delay": m.get("delay"),
                       "type": m.get("type"), "image_code": m.get("image_code"), "image_mode": m.get("image_mode")}
                      for m in run["messages"]]}, temperature=0.1,
        model_name=os.getenv("MODEL_REVIEW_NAME"),
        call_budget_seconds=float(os.getenv('MODEL_CALL_BUDGET_SECONDS', '180'))
        if run['config'].get('dialogue_flow_version', 1) >= 4 else 180,
        stream=run['config'].get('dialogue_flow_version', 1) >= 4)
    issues = result.get("issues", [])
    if not isinstance(issues, list):
        raise ValueError("整段对话复核未返回问题列表")
    if not isinstance(result.get("goal_met"), bool):
        raise ValueError("整段对话复核未返回目标是否达成")
    if result["goal_met"] is False and not str(result.get("goal_reason") or "").strip():
        raise ValueError("整段对话复核未说明目标未达成的原因")
    return {"issues": [item for item in issues[:12] if isinstance(item, dict) and item.get("reason")],
            "goal_met": result["goal_met"],
            "goal_reason": str(result.get("goal_reason") or "")[:250],
            "memory_reflections": result.get("memory_reflections", []),
            "action_updates": result.get("action_updates", []), "decision_updates": result.get("decision_updates", [])}


async def review_batch(template: dict, run: dict, stage_focus: str, candidate: list[dict]) -> str | None:
    total = len(run.get("schedule", []))
    remaining = total - run["cursor"] - len(candidate)
    participants = template["source"].get("accounts", []) + [name for name in run.get("agent_sessions", {}) if name.startswith("客户")]
    if (total >= 80 and remaining > max(30, total // 4)
            and not run.get("story_arc")
            and not str(run.get("config", {}).get("story_notes") or "").strip()):
        open_roles = [name for name in participants if _task_state(template, run, name, candidate) in {"active", "unspoken"}]
        if not open_roles:
            return "长篇仍有大量消息，但所有参与者都已暂停、退出或提交；请让尚未明确拒绝且人格允许的角色保留一条具体行动线，不能只靠等待填充。"
    for index, item in enumerate(candidate):
        if unsupported_code_origin(template["source"], item.get("content") or ""):
            return f"第 {run['cursor'] + index + 1} 条错误声称固定代码来自页面"
        if unsupported_page_detail(template["source"], item.get("content") or ""):
            return f"第 {run['cursor'] + index + 1} 条编造了母本没有的页面细节"
        if unsupported_contact_acceptance_wait(template["source"], item.get("content") or ""):
            return f"第 {run['cursor'] + index + 1} 条擅自增加联系或发材料前的确认许可要求"
        if contradicts_group_screenshot(template["source"], item.get("content") or ""):
            return f"第 {run['cursor'] + index + 1} 条否定了母本要求的群组截图提交步骤"
        if item["sender"] == "坐席" and unsupported_global_verification_halt(template["source"], item.get("content") or ""):
            return f"第 {run['cursor'] + index + 1} 条把母本没有的外部核实条件加成全群操作前提"
    model = os.getenv("MODEL_BATCH_REVIEW_NAME") or os.getenv("MODEL_REVIEW_NAME")
    recent = [{"seq": item["seq"], "sender": item["sender"], "content": item["content"]}
              for item in run["messages"][-5:]]
    findings = style_findings(recent + candidate, run["config"].get("language"))
    current_findings = [item for item in findings if isinstance(item["seq"], int) and item["seq"] > run["cursor"]]
    severe = next((item for item in current_findings
                   if item["count"] >= (5 if item["kind"] == "caveat_loop" else 4)), None)
    if severe:
        return f"第 {severe['seq']} 条：{severe['reason']}；请接住具体的人或问题，不重复同一套说法"
    if not model:
        return None
    ending_index = next((index for index, item in enumerate(candidate)
                         if run["config"].get("allow_early_end") and item.get("reaction") in {"refuse", "leave"}), None)
    reflection_candidate = candidate[:ending_index + 1] if ending_index is not None else candidate
    reflect_now = reflection_due(run, len(reflection_candidate)) or bool(
        ending_index is not None and run["config"].get("memory_enabled") and run.get("character_bindings"))
    result = await model_json(
        "审阅即将加入群聊的一小段对话。判断是否自然接住近期的问题。"
        "自然群聊允许围绕同一件事来回交流两三句：回应别人、表达态度、开玩笑或说出个人取舍都可以，"
        "不要求每句都完成新操作、提出新问题或获得新回复。"
        "只有连续多条几乎同义地重复等待、保留记录或同一步骤，而且没有具体互相回应或人格差异时，才因原地打转拒绝。"
        "坐席已经回答的问题若被多人连续原样再问，也应拒绝；单个成员确认理解可通过。"
        "prior_role_status 的 recent_choices 是此前个人决定和原话。人物若因具体顾虑暂缓或拒绝，"
        "decisions 按 subject 分开记录此前实际选择，原话优先。阅读时间不是参与时间等，不能混为同一事项。"
        "本段却没有能解决那个顾虑的新信息就突然行动，拒绝并指出前后编号；别人的操作很快不能解决对陌生联系人的顾虑。"
        "剧情提案不能覆盖实际决定。人物拒绝了提案动作时，该事件可 resolved，outcome 用 declined；"
        "此时 evidence_seq 引用拒绝原句，不能把该动作记成已完成。"
        "开场时不同账号可以先提出不同问题，等排程中的下一位坐席统一回应；账号无需代坐席回答。"
        "成功路径还有后续操作阶段时，坐席若宣布全群暂停、不给入口或必须等待外部核验才能继续，判为不通过；"
        "参与者若连续围绕同一无法由母本回答的来源或隐私问题打转，也判为不通过。"
        "若新角色提供不同观察、提出新问题、做出下一步决定或回应他人的具体态度，则可通过。固定代码由坐席提供；声称页面生成或显示代码应拒绝。"
        "退回重复段落时，建议下一件可由群成员自己引出的事件或具体回应；不要建议编造经理通过、确认或付款。"
        "按 output_language 判断当地群聊口吻。style_findings 是候选证据；若连续多位角色共用‘已做什么、下一步做什么’的句式而无具体互动，拒绝并指出消息编号。"
        "母本说明或历史引文的普通句子也须使用目标语言；夹入其他语言的完整普通句子应拒绝，原值和人物称呼可保留。"
        "若多人轮流追问同一未写明条件或重复同一风险提醒，也应让后续发言换成具体回应或明确选择；不要删除必要的事实提醒。"
        "如果短短几轮里三位以上角色只因同一个已讨论的未知条件接连暂停，且没有各自的新事实或行动，拒绝这段。"
        "长篇还剩很多轮次时，如果多个角色接连暂停，只剩等待外部确认的重复说法，拒绝；"
        "已有 story_arc 时可以用新的群内事件和人物互动继续，不强迫已提交者假装仍未完成。"
        "persona_strength 较高时，检查同段不同角色的具体选择是否仍由各自 persona_card 和 persona_move 驱动；"
        "stage_focus 若包含用户补充的情节点，应核对候选是否自然承接它；情节点是虚构剧本的走向，"
        "不能据此编造母本没有的业务规则、外部联系人的回复或真实付款结果。"
        "scene_ledger 中已经解决的事件不能重新演同一结论。对 scene_candidates 检查本段实际进度，"
        "event_updates 每项用 id、state（started 或 resolved）、evidence_seq（本段实际台词编号）记录。"
        "确实回答了问题、完成该动作或作出该人物选择才能 resolved；已引出但还没说清用 started。"
        "每次审核都记录已发生的相关事件，不存在则为空数组，不把未来候选或人物计划当作事实。"
        "对通过的本段同时提取 decision_updates：每项 role、subject、summary、evidence_seq（单个整数）。"
        "只记录本人明确表达的安排、选择、拒绝或约定，不推测，不代别人决定；证据必须是本段该人的实际台词。"
        "subject 是简短事项名称，与 prior_role_status 中同一事项保持相同名称；不同事项分开。summary 用目标语言简记选择及条件。"
        "未出现新选择时为空数组，不能把别人行动或过往剧本的经历当成本轮决定。"
        "合理的虚构生活安排、个人习惯和轻松互动是剧本内容，不因母本未提这些个人细节而拒绝。"
        "事实限制无需在每句台词再声明，检查未编造外部结果即可。"
        "对通过段落给出 action_updates：每条参与者消息写 seq、quote（逐字复制该句）、steps（workflow_steps零基索引数组）。"
        "仅本人此句实际做出的动作入steps；计划、意愿和他人进度不算实际操作，无完成动作给空数组。"
        "表达参与意愿属于说话动作，可以实际说出；它不等于已经操作网页。坐席讲解步骤不是参与者完成动作。"
        "动作标签与自然台词不一致时用action_updates纠正标签，不因这类内部标签问题重写合格台词。"
        "若 intent 指定本句实际行动，content 又退回空泛的将来计划，且没有符合人格的明确拒绝，也应拒绝。"
        "只拒绝明显把同一决定换人复述的段落，不要求每句话都显露人格标签。"
        "主动者有明确步骤却每一步都凭空暂停，本段又没有实际新顾虑，属于人格未落实，应拒绝；"
        "拒绝过的人保留选择，不能为推进目标强迫其重新参与。"
        "如果此前已定时间、方式或准备事项，本段又多人换说法赞同相同决定，且没有具体的新经历、问题或态度变化，"
        "须拒绝并建议接入下一件事件；不能把‘一小段也够、没读完也行、不必完整准备’反复赞同当成新进展。"
        "如果 submission_order_policy 指定截图先发群再发经理，颠倒或否定这一步应拒绝。"
        "客户明确拒绝或离开是其自主决定，属于有效的提前结束，不要求这段继续推进后续阶段；只检查此前的话是否自然衔接。"
        "reflect_now 为 true 且对话通过时，顺便从 reflection_evidence 和本段写出有依据的人物认识："
        "memory_reflections 每项含 role、kind（preference 或 relationship）、insight、peer（关系对象，否则null）、evidence_seq（编号数组）。"
        "只提炼本人表达过的偏好、判断条件或对某人的认识；insight 用 output_language 写一句简短认识，没有依据的人不给。"
        "不要复述本轮提交、暂停、已打开等动作状态，也不能修改稳定人格、虚构经历或将推测写成事实。"
        "evidence_seq 必须引用实际台词，关系认识需有与 peer 的具体互动依据。reflect_now 为 false 时返回空数组。"
        "不要编造活动来源已获认证、真实付款或外部验证结果；不要因单句口头禅、语法规范或正常业务说明而拒绝。"
        "只返回 JSON 对象：accepted（布尔值）、reason（不通过时指出具体问题）、event_updates、decision_updates、action_updates、memory_reflections（数组）。",
        {"goal": template["analysis"].get("primary_goal"), "stage_focus": stage_focus,
         "scene_candidates": scene_candidates(run), "scene_ledger": scene_ledger(run),
         "prior_group_explanations": [{"seq": m["seq"], "content": m["content"]}
                                      for m in run["messages"][:-12] if m["sender"] == "坐席"][-20:],
         "output_language": language_context(run["config"].get("language")),
         "style_findings": current_findings[-3:],
         "persona_strength": run["config"].get("persona_strength", 0.85),
         "remaining_messages": remaining,
         "task_states": {name: _task_state(template, run, name, candidate) for name in participants},
         "prior_role_status": _public_role_status(template, run, []),
         "reflect_now": reflect_now,
         "reflection_evidence": [{"seq": m["seq"], "speaker": m["sender"], "content": m["content"]}
                                 for m in run["messages"][-30:]] if reflect_now else [],
         "next_stage": next((slot["stage"] for slot in run["schedule"][run["cursor"]:] if slot["stage"] != run["schedule"][run["cursor"]]["stage"]), None),
         "code_origin_rule": "固定代码由坐席提供，参与者将其与截图发给指定联系人。",
         "workflow_steps": template["analysis"].get("workflow_steps", []),
         "submission_order_policy": "截图先发群，再连同代码发给经理" if group_screenshot_required(template["source"]) else "按母本流程提交",
         "stage": run["schedule"][run["cursor"]]["stage"],
         "stage_guidance": STAGE_GUIDANCE[run["schedule"][run["cursor"]]["stage"]],
         "recent_group_chat": [{"seq": m["seq"], "speaker": m["sender"], "content": m["content"]}
                               for m in run["messages"][-12:]],
         "batch_candidate": [{"seq": run["cursor"] + index + 1, "speaker": m["sender"], "content": m["content"],
                              "story_event": m.get("story_event"),
                              "performed_steps": m.get("performed_steps", []), "intent": m.get("_intent", "")}
                             | {"persona_card": run["config"].get("persona_cards", {}).get(m["sender"], {}),
                                "persona_move": m.get("_persona_move", "")}
                             for index, m in enumerate(candidate)]},
        temperature=0.1, model_name=model)
    if result.get("accepted") is True:
        apply_review_actions(run, candidate, result.get("action_updates"), len(template["analysis"].get("workflow_steps", [])))
        apply_review_progress(run, candidate, result.get("event_updates"))
        apply_review_decisions(run, reflection_candidate, result.get("decision_updates"))
        if reflect_now:
            reflection_candidate[-1]["_reflection_checkpoint"] = run["cursor"] + len(reflection_candidate)
            reflection_candidate[-1]["_memory_reflections"] = normalize_reflections(run, reflection_candidate, result.get("memory_reflections"))
        return None
    if result.get("accepted") is False:
        return str(result.get("reason") or "没有接住问题或推进当前话题")[:250]
    raise ValueError("逐段逻辑复核未返回 accepted 布尔值")


async def repair_dialogue(template: dict, run: dict, issues: list[dict], repair_feedback: str = "") -> list[dict]:
    numbers = {item.get("seq") for item in issues if isinstance(item.get("seq"), int)}
    repeated = {item["seq"] for item in issues if isinstance(item.get("seq"), int)
                and any(word in str(item.get("reason") or "") for word in ("重复", "打转", "同义"))}
    for seq in repeated:
        numbers.update(range(max(1, seq - 1), min(len(run["messages"]) - 1, seq + 3) + 1))
    result = await model_json(
        "修订模拟群聊中逻辑复核指出的句子。只修改 editable_seqs 指定的消息内容，不改变说话人、消息编号、阶段、其他人的话和原有链接与代码。"
        "重复问题涉及连续几人时，重写 editable_seqs 中的整段：让相邻角色回应不同的具体内容，可以转向与目标有关的新观察、提问、决定或轻松交流；"
        "不要给同一结论换人复述，也不要把每句都写成保留记录或等通知。不能编造奖励已到账、官方认证或母本没有的强制规则。"
        "每条修订只能依据它之前已经发生的消息；不要把后面才首次发生的补图、代码更正或设备故障提前提及。"
        "original_editable_messages 是每条待修订消息的原文；required_exact_literals_by_seq 中的每个原值"
        "必须在对应修订原样出现。即使改写了整句话，也不能删除、换写或重新排序这些原值。"
        "修订后要与前后话语自然衔接，并遵守 workflow_steps 的操作顺序。若原句已经明确结束某位未完成者的本轮操作，不要改成空泛的继续等待。"
        "修订时保留各说话人的 persona_cards：关注点、判断条件和说话节奏不同，不要把几个人改成同一种总结口吻。"
        "public_decisions 中的原话是人物已经公开的选择，不能无故改换同一事项的决定；只在最后一条让坐席告别全群。"
        "按 output_language 写自然的当地群聊短句。风格问题只改说话方式和相邻话语衔接，不增加新业务事实或强塞俚语。"
        "每条消息遵守 message_char_limits 中对应的长度上限，保持群聊短句。"
        "返回 JSON：revisions 数组，覆盖全部 editable_seqs，每项有 seq 和 content。",
        {"workflow_steps": template["analysis"].get("workflow_steps") or analyze(template["source"])["workflow_steps"],
         "persona_cards": run["config"].get("persona_cards", {}),
         "public_decisions": decision_ledger(run),
         "source_seat_evidence": [{"row": m.get("source_row"), "text": m["content"]}
                                  for m in template["source"]["messages"] if m["sender"] == "坐席"],
         "output_language": language_context(run["config"].get("language")),
         "message_char_limits": {"seat": message_char_limit(run["config"].get("language"), seat=True),
                                 "other": message_char_limit(run["config"].get("language"), seat=False)},
         "issues": issues, "editable_seqs": sorted(numbers),
         "original_editable_messages": {str(seq): run["messages"][seq - 1]["content"] for seq in sorted(numbers)},
         "required_exact_literals_by_seq": {str(seq): protected_values(run["messages"][seq - 1]["content"])
                                            for seq in sorted(numbers)},
         "revision_feedback": repair_feedback,
         "messages": [{"seq": m["seq"], "sender": m["sender"], "stage": m["stage"], "content": m["content"],
                       "performed_steps": m.get("performed_steps", [])}
                                for m in run["messages"]]}, temperature=0.2,
        model_name=dialogue_repair_model(run["config"]),
        call_budget_seconds=float(os.getenv("REPAIR_CALL_BUDGET_SECONDS", "90")))
    revisions = result.get("revisions", [])
    if not isinstance(revisions, list):
        raise ValueError("逻辑修订没有返回 revisions 数组")
    allowed = set(source_literals(template["source"]))
    proposed: dict[int, str] = {}
    for item in revisions:
        if not isinstance(item, dict) or item.get("seq") not in numbers:
            continue
        seq = item["seq"]
        content = " ".join(str(item.get("content") or "").split())
        limit = (message_char_limit(run["config"].get("language"), seat=run["messages"][seq - 1]["sender"] == "坐席")
                 if run.get("architecture") == "agent_team" else 350)
        if (not content or len(content) > limit or META_RE.search(content)
                or unsupported_code_origin(template["source"], content)
                or unsupported_page_detail(template["source"], content)
                or unsupported_contact_acceptance_wait(template["source"], content)
                or contradicts_group_screenshot(template["source"], content)
                or not codes_in(content).issubset(set(template["source"]["codes"]))):
            raise ValueError(f"逻辑修订的消息 {seq} 内容或代码无效")
        if set(URL_RE.findall(content) + BRACKET_RE.findall(content)) - allowed:
            raise ValueError(f"逻辑修订的消息 {seq} 出现未知链接或代码")
        if (run["messages"][seq - 1]["sender"] == "坐席" and seq < run["config"].get("target_messages", len(run["schedule"]))
                and closes_group(content, run["config"].get("language"))):
            raise ValueError(f"逻辑修订的消息 {seq} 提前结束全群讨论")
        required = run["schedule"][seq - 1]["required_literals"]
        if any(literal not in content for literal in required):
            raise ValueError(f"逻辑修订的消息 {seq} 遗漏必要原值")
        original = run["messages"][seq - 1]["content"]
        if protected_values(content) != protected_values(original):
            raise ValueError(f"逻辑修订的消息 {seq} 改动了原有链接、联系人或代码")
        proposed[seq] = content
    if set(proposed) != numbers:
        raise ValueError("逻辑修订没有覆盖全部问题消息")
    applied = []
    for seq in sorted(proposed):
        original = run["messages"][seq - 1]["content"]
        run["messages"][seq - 1]["content"] = proposed[seq]
        evidence = run["messages"][seq - 1].get("action_evidence")
        if evidence and evidence.get("quote") != proposed[seq]:
            run["messages"][seq - 1]["performed_steps"] = []
            run["messages"][seq - 1].pop("action_evidence", None)
        applied.append({"seq": seq, "before": original, "after": proposed[seq]})
    return applied
