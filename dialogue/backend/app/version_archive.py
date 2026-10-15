"""Immutable full-draft snapshots. This module never saves runs or writes the DB."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .core import export_xlsx


MESSAGE_FIELDS = (
    "seq", "sender", "content", "reply_to", "delay", "stage", "type",
    "image_code", "image_mode", "replace_date",
)
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REVISION_DIR = re.compile(r"R([0-9]{3,})-([0-9a-f]{8})\Z")
_ARTIFACTS = ("run.json", "messages.md", "messages.xlsx")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _inside(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
        raise ValueError("版本归档路径超出指定目录")
    return resolved


@contextmanager
def _locked(directory: Path):
    # An OS lock survives neither a crash nor process termination; there is no
    # stale exclusive-create marker to block a later retry.
    with (directory / ".archive.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _markdown(record: dict, messages: list[dict]) -> bytes:
    lines = [f"# {record['version']} · 捕获状态：{record['capture_status']}", ""]
    for message in messages:
        reply = message.get("reply_to")
        suffix = f"（回复第 {reply} 条）" if reply is not None else ""
        lines.extend([f"{message.get('seq', '')} **{message.get('sender', '')}**{suffix}：", ""])
        if message.get("content"):
            lines.extend([str(message["content"]), ""])
        if "图片" in str(message.get("type", "")) or message.get("image_code"):
            lines.extend([f"[待补素材码：{message.get('image_code') or '未填写图片码'}]", ""])
    return "\n".join(lines).encode("utf-8")


def _committed(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name in _ARTIFACTS:
        payload = (directory / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest["files"][name]:
            raise ValueError(f"历史版本文件校验失败：{directory.name}/{name}")
    return manifest["record"]


def archive_candidate(data_dir: Path, run: dict, messages: list[dict], reason: str) -> dict | None:
    """Capture each complete wording once; caller persists candidate_versions.

    None means the draft has not reached its configured length. Errors propagate
    without changing the working run, so callers can report/retry archival errors
    independently from generation. Lowercase UUID hex IDs are required.
    """
    target = run.get("config", {}).get("target_messages")
    if type(target) is not int or target <= 0:
        raise ValueError("版本归档需要有效的目标消息条数")
    if len(messages) < target:
        return None
    run_id = run.get("id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("版本归档的运行编号必须是32位小写十六进制")
    if not all(isinstance(message, dict) for message in messages):
        raise ValueError("版本归档的每条消息必须是对象")
    records = run.get("candidate_versions", [])
    if not isinstance(records, list):
        raise ValueError("版本归档索引必须是列表")
    wording = [{key: message.get(key) for key in MESSAGE_FIELDS} for message in messages]
    fingerprint = hashlib.sha256(json.dumps(
        wording, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    root = Path(data_dir).resolve()
    versions = _inside(root / "versions", root)
    directory = _inside(versions / run_id, versions)
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(directory):
        records = deepcopy(run.get("candidate_versions", []))
        if not isinstance(records, list):
            raise ValueError("版本归档索引必须是列表")
        committed = []
        for path in sorted(directory.iterdir()):
            match = _REVISION_DIR.fullmatch(path.name)
            if match and path.is_dir():
                safe_path = _inside(path, directory)
                record = _committed(safe_path)
                if record["version"] != "R" + match[1] or record["hash"][:8] != match[2]:
                    raise ValueError("历史版本目录与清单不一致")
                committed.append(record)
        recovered = False
        for record in committed:
            if not any(item.get("hash") == record["hash"] for item in records):
                records.append(deepcopy(record))
                recovered = True
        for record in committed:
            if record["hash"] == fingerprint:
                # Recover publication that succeeded just before a caller crash
                # or save_run failure. Never rewrite captured status or files.
                if recovered:
                    run["candidate_versions"] = deepcopy(records)
                return deepcopy(record)
        if any(item.get("hash") == fingerprint for item in records):
            raise ValueError("版本索引指向的历史稿文件不存在，拒绝覆盖或伪造恢复")
        number = max((item["number"] for item in committed + records), default=-1) + 1
        version = f"R{number:03d}"
        destination = _inside(directory / f"{version}-{fingerprint[:8]}", directory)
        version_record = deepcopy(run.get("version_record", {}))
        record = {
            "number": number, "version": version, "hash": fingerprint,
            "reason": str(reason), "path": destination.relative_to(root).as_posix(),
            "capture_status": run.get("status", ""),
            "code": version_record.get("code") if isinstance(version_record, dict) else None,
            "version_record": version_record,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        captured = deepcopy(run)
        captured["messages"] = deepcopy(messages)
        captured["candidate_versions"] = deepcopy(records) + [deepcopy(record)]
        # Preserve malformed complete drafts in run.json too. Missing required
        # table fields become empty cells in the export, never invented dialogue.
        table = {**captured, "messages": [
            {**message, **{key: message.get(key) for key in ("seq", "sender", "type")}}
            for message in captured["messages"]]}
        artifacts = {
            "run.json": _json_bytes(captured),
            "messages.md": _markdown(record, captured["messages"]),
            "messages.xlsx": export_xlsx(table),
        }
        manifest = {"schema_version": 1, "record": record, "message_count": len(messages),
                    "hash_fields": list(MESSAGE_FIELDS),
                    "files": {name: hashlib.sha256(payload).hexdigest() for name, payload in artifacts.items()}}
        staging = directory / f".{destination.name}.tmp-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            for name, payload in artifacts.items():
                _write(staging / name, payload)
            _write(staging / "manifest.json", _json_bytes(manifest))
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        run["candidate_versions"] = deepcopy(records) + [deepcopy(record)]
        return deepcopy(record)
