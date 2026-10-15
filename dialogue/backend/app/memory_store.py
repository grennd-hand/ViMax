"""SQLite character memory with source quotes, versioning and run boundaries.

The store owns public evidence only. Private agent state stays in the run.
Embeddings and reflections are derived data; accepted dialogue is authoritative.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
from typing import Protocol
import uuid

from .persona_memory import _terms


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_version(card: dict) -> str:
    stable = {key: card.get(key, "") for key in ("identity", "priority", "decision_rule")}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def init_memory_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS character_profiles (
            id TEXT PRIMARY KEY, world_id TEXT NOT NULL, role_name TEXT NOT NULL,
            kind TEXT NOT NULL, current_version TEXT NOT NULL, card_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS character_versions (
            character_id TEXT NOT NULL, version TEXT NOT NULL, card_json TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(character_id, version));
        CREATE TABLE IF NOT EXISTS public_episodes (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, world_id TEXT NOT NULL,
            character_id TEXT NOT NULL, profile_version TEXT NOT NULL, role_name TEXT NOT NULL,
            language TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, seq));
        CREATE TABLE IF NOT EXISTS character_reflections (
            id TEXT PRIMARY KEY, world_id TEXT NOT NULL, character_id TEXT NOT NULL,
            profile_version TEXT NOT NULL, run_id TEXT NOT NULL, through_seq INTEGER NOT NULL,
            kind TEXT NOT NULL, peer_id TEXT, insight TEXT NOT NULL, evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1,
            published_at TEXT, vector_json TEXT, embedding_model TEXT);
        CREATE INDEX IF NOT EXISTS reflection_scope ON character_reflections
            (world_id, character_id, profile_version, published_at);
        CREATE INDEX IF NOT EXISTS episode_scope ON public_episodes (world_id, character_id);
        CREATE TABLE IF NOT EXISTS memory_embedding_cache (
            cache_key TEXT PRIMARY KEY, model TEXT NOT NULL, vector_json TEXT NOT NULL,
            created_at TEXT NOT NULL);
    """)


def bind_characters(connection: sqlite3.Connection, run: dict) -> None:
    if not run.get("config", {}).get("memory_enabled"):
        return
    config = run["config"]
    world = config.get("story_world", "").strip() or run["template_id"]
    config["story_world"] = world
    requested = config.get("character_ids") or {}
    bindings = {}
    for role in run["agent_sessions"]:
        character_id = requested.get(role) or uuid.uuid5(uuid.NAMESPACE_URL, f"script-character:{world}:{role}").hex
        existing = connection.execute("SELECT world_id, kind FROM character_profiles WHERE id=?", (character_id,)).fetchone()
        kind = "seat" if role == "坐席" else "participant"
        if existing and (existing["world_id"] != world or existing["kind"] != kind):
            raise ValueError(f"{role}的人物身份不属于当前经历分组或角色类型")
        card = config.get("persona_cards", {}).get(role, {})
        version = profile_version(card)
        moment = timestamp()
        card_json = json.dumps(card, ensure_ascii=False)
        connection.execute("""INSERT INTO character_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET current_version=excluded.current_version,
            card_json=excluded.card_json,role_name=excluded.role_name,updated_at=excluded.updated_at""",
            (character_id, world, role, kind, version, card_json, moment, moment))
        connection.execute("INSERT OR IGNORE INTO character_versions VALUES (?, ?, ?, ?)",
                           (character_id, version, card_json, moment))
        bindings[role] = {"character_id": character_id, "version": version}
    if len({binding["character_id"] for binding in bindings.values()}) != len(bindings):
        raise ValueError("不同角色需要分别绑定人物身份")
    config["character_ids"] = {role: binding["character_id"] for role, binding in bindings.items()}
    run["character_bindings"] = bindings
    run["memory_snapshot_at"] = timestamp()
    run["memory_schema_version"] = 1


def sync_accepted_memory(connection: sqlite3.Connection, run: dict) -> None:
    """Called in the same transaction as the run checkpoint; pending drafts never enter."""
    bindings = run.get("character_bindings") or {}
    if not bindings or not run.get("config", {}).get("memory_enabled"):
        return
    world = run["config"]["story_world"]
    language = run["config"].get("language", "zh-CN")
    evidence = {message["seq"]: message for message in run.get("messages", [])}
    stored = {row["seq"]: row["content"] for row in connection.execute(
        "SELECT seq,content FROM public_episodes WHERE run_id=?", (run["id"],))}
    for message in evidence.values():
        if stored.get(message["seq"]) == message.get("content", ""):
            continue
        binding = bindings.get(message["sender"])
        if not binding:
            continue
        connection.execute("""INSERT INTO public_episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, seq) DO UPDATE SET content=excluded.content""",
            (run["id"], message["seq"], world, binding["character_id"], binding["version"],
             message["sender"], language, message.get("content", ""), run["created_at"]))
    # A later full-text repair invalidates thoughts based on changed quotes.
    publishable = (run.get("status") in {"completed", "early_end"}
                   and not run.get("errors") and not run.get("quality_review"))
    for reflection in run.get("memory_reflections", []):
        binding = bindings.get(reflection.get("role"))
        if not binding:
            continue
        quotes = reflection.get("evidence", [])
        valid = bool(quotes) and all(
            quote.get("seq") in evidence
            and evidence[quote["seq"]].get("content") == quote.get("quote")
            and evidence[quote["seq"]].get("sender") == quote.get("speaker") for quote in quotes)
        published = timestamp() if valid and publishable else None
        reflection["valid"] = valid
        reflection["published"] = bool(published)
        connection.execute("""INSERT INTO character_reflections
            (id,world_id,character_id,profile_version,run_id,through_seq,kind,peer_id,
             insight,evidence_json,created_at,valid,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET valid=excluded.valid,
            published_at=CASE WHEN excluded.published_at IS NULL THEN NULL
                              ELSE COALESCE(character_reflections.published_at,excluded.published_at) END""",
            (reflection["id"], world, binding["character_id"], binding["version"], run["id"],
             reflection["through_seq"], reflection["kind"], reflection.get("peer_id"), reflection["insight"],
             json.dumps(quotes, ensure_ascii=False), reflection["created_at"], int(valid), published))
    invalid_ids = {item["id"] for item in run.get("memory_reflections", []) if not item.get("valid", True)}
    for session in run.get("agent_sessions", {}).values():
        session["long_term_memory"] = [item for item in session.get("long_term_memory", [])
                                       if item.get("id") not in invalid_ids]
    if "memory_status" in run:
        run["memory_status"]["recalled"] = sum(len(session.get("long_term_memory", []))
                                               for session in run.get("agent_sessions", {}).values())


def reflection_due(run: dict, candidate_count: int) -> bool:
    if not run.get("config", {}).get("memory_enabled") or not run.get("character_bindings"):
        return False
    end = run["cursor"] + candidate_count
    return candidate_count > 0 and (end >= len(run["schedule"])
                                   or end - run.get("reflection_checkpoint", 0) >= 24)


def normalize_reflections(run: dict, candidate: list[dict], proposals: object) -> list[dict]:
    if not isinstance(proposals, list):
        return []
    public = {item["seq"]: item for item in run.get("messages", [])}
    public.update({run["cursor"] + index + 1: item for index, item in enumerate(candidate)})
    bindings = run.get("character_bindings") or {}
    accepted, seen = [], set()
    for proposal in proposals[:len(bindings) * 2]:
        if not isinstance(proposal, dict):
            continue
        role, kind = proposal.get("role"), proposal.get("kind")
        insight = proposal.get("insight", "")
        insight = insight.strip() if isinstance(insight, str) else ""
        seqs = proposal.get("evidence_seq")
        seqs = seqs if isinstance(seqs, list) else [seqs]
        if (not isinstance(role, str) or role not in bindings or not isinstance(kind, str) or kind not in {"preference", "relationship"}
                or not 4 <= len(insight) <= 180 or not seqs or len(seqs) > 6
                or any(type(seq) is not int or seq not in public for seq in seqs)):
            continue
        # A person must actually have expressed something; observing strangers
        # does not establish their own preference or private relationship.
        if not any(public[seq].get("sender") == role for seq in seqs):
            continue
        if re.search(r"(?:本轮|这轮|目前|现在).{0,12}(?:已|已经|完成|提交|暂停|退出)|^(?:我|他|她|自己)?(?:已提交|已经提交|已打开|已经打开|已截图|已暂停)", insight):
            continue
        peer = proposal.get("peer")
        if kind == "relationship" and (not isinstance(peer, str) or peer not in bindings or peer == role):
            continue
        if kind == "relationship" and not any(
            (public[seq]["sender"] == role and (peer in public[seq]["content"]
             or public.get(public[seq].get("reply_to"), {}).get("sender") == peer))
            or (public[seq]["sender"] == peer and (role in public[seq]["content"]
                or public.get(public[seq].get("reply_to"), {}).get("sender") == role)) for seq in seqs):
            continue
        key = (role, kind, insight)
        if key in seen:
            continue
        seen.add(key)
        end = run["cursor"] + len(candidate)
        proof = [{"seq": seq, "speaker": public[seq]["sender"], "quote": public[seq]["content"]}
                 for seq in dict.fromkeys(seqs)]
        fingerprint = json.dumps(proof, ensure_ascii=False, sort_keys=True)
        reflection_id = hashlib.sha256(f"{run['id']}:{end}:{role}:{kind}:{insight}:{fingerprint}".encode()).hexdigest()
        accepted.append({"id": reflection_id, "role": role, "kind": kind, "insight": insight,
                         "peer_id": bindings[peer]["character_id"] if kind == "relationship" else None,
                         "through_seq": end, "created_at": timestamp(),
                         "evidence": proof})
    return accepted


class MemoryStore(Protocol):
    def search(self, run: dict, role: str, query: str, vector: list[float] | None,
               embedding_model: str, limit: int = 6) -> list[dict]: ...


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}:512:{text}".encode()).hexdigest()


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(a*b for a, b in zip(left, right)) / norm if norm else 0.


class SQLiteMemoryStore:
    def __init__(self, path: Path | str):
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cached_vectors(self, model: str, texts: list[str]) -> dict[str, list[float]]:
        result = {}
        with self.connection() as connection:
            for text in texts:
                row = connection.execute("SELECT vector_json FROM memory_embedding_cache WHERE cache_key=?",
                                         (cache_key(model, text),)).fetchone()
                if row:
                    result[text] = json.loads(row["vector_json"])
        return result

    def save_vectors(self, model: str, vectors: dict[str, list[float]]) -> None:
        with self.connection() as connection:
            connection.executemany("INSERT OR REPLACE INTO memory_embedding_cache VALUES (?, ?, ?, ?)",
                [(cache_key(model, text), model, json.dumps(vector), timestamp()) for text, vector in vectors.items()])

    def index_candidates(self, run: dict, model: str, limit: int = 64) -> list[dict]:
        bindings = run.get("character_bindings") or {}
        results = []
        with self.connection() as connection:
            for binding in bindings.values():
                rows = connection.execute("""SELECT id,insight FROM character_reflections
                    WHERE world_id=? AND character_id=? AND profile_version=? AND valid=1
                    AND (published_at<=? OR run_id=?)
                    AND (vector_json IS NULL OR embedding_model<>?) ORDER BY created_at DESC LIMIT ?""",
                    (run["config"]["story_world"], binding["character_id"], binding["version"],
                     run["memory_snapshot_at"], run["id"], model, limit)).fetchall()
                results.extend(dict(row) for row in rows)
        return results[:limit]

    def apply_vectors(self, rows: list[dict], vectors: dict[str, list[float]], model: str) -> None:
        with self.connection() as connection:
            for row in rows:
                if row["insight"] in vectors:
                    connection.execute("UPDATE character_reflections SET vector_json=?,embedding_model=? WHERE id=?",
                                       (json.dumps(vectors[row["insight"]]), model, row["id"]))

    def search(self, run: dict, role: str, query: str, vector: list[float] | None,
               embedding_model: str, limit: int = 6) -> list[dict]:
        binding = run.get("character_bindings", {}).get(role)
        if not binding or limit <= 0:
            return []
        with self.connection() as connection:
            rows = connection.execute("""SELECT * FROM character_reflections
                WHERE world_id=? AND character_id=? AND profile_version=? AND valid=1
                AND (published_at<=? OR run_id=?) ORDER BY created_at DESC LIMIT 500""",
                (run["config"]["story_world"], binding["character_id"], binding["version"],
                 run["memory_snapshot_at"], run["id"])).fetchall()
        query_terms = _terms(query)
        ranked = []
        for index, row in enumerate(rows):
            words = _terms(row["insight"])
            lexical = len(query_terms & words) / max(1, len(query_terms | words))
            stored = json.loads(row["vector_json"]) if row["vector_json"] and row["embedding_model"] == embedding_model else None
            semantic = cosine(vector, stored) if vector is not None and stored is not None else None
            score = (.8 * semantic + .1 * lexical if semantic is not None else lexical) + .1 * .97**index
            ranked.append((score, {"id": row["id"], "kind": row["kind"], "insight": row["insight"],
                                   "peer_id": row["peer_id"], "peer_role": next((name for name, other in run["character_bindings"].items()
                                        if other["character_id"] == row["peer_id"]), None), "source_run_id": row["run_id"],
                                   "from_current_run": row["run_id"] == run["id"],
                                   "evidence": json.loads(row["evidence_json"]),
                                   "retrieval": "semantic" if semantic is not None else "lexical"}))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]

    def profiles(self, world: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute("""SELECT p.*,
                (SELECT COUNT(*) FROM public_episodes e WHERE e.character_id=p.id) AS episode_count,
                (SELECT COUNT(*) FROM character_reflections r WHERE r.character_id=p.id
                 AND r.profile_version=p.current_version AND r.valid=1 AND r.published_at IS NOT NULL) AS reflection_count
                FROM character_profiles p WHERE world_id=? ORDER BY role_name""", (world,)).fetchall()
        return [{"id": row["id"], "role_name": row["role_name"], "kind": row["kind"],
                 "version": row["current_version"], "card": json.loads(row["card_json"]),
                 "episode_count": row["episode_count"], "reflection_count": row["reflection_count"]} for row in rows]
