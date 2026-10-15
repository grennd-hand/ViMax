"""One local ViMax revision per invocation. Never call the automatic repair loop.

Usage: python evolve-natural.py --version 04 --parent .../V03/run.json --issues issues.json
issues.json: {"issues": [...], "reviews": {"main": {...}, "subagent": {...}}}
Each review requires full_read=true, message_count=150, run_sha256=<parent file hash>,
and a nonempty summary. A failed version is a valid next-version parent after both
readers have reviewed its full actual candidate. --resume-review repeats only the
program review of a failed/interrupted actual candidate; it never calls revise.
All data and SQLite memory are isolated below the new version directory.
An explicit --revision-label R001 may use the same family as its parent only
when its revision is strictly newer; an absent parent revision means R000.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
MODEL = 'gpt-6-sol'
EFFORT = 'high'
TARGET = 150


class NoChangeError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value):
    atomic(path, json_bytes(value))


@contextmanager
def exclusive_lock(path):
    """Nonblocking OS lock: all real invocations share one repo-local lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('另一个真实迭代驱动正在运行') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('另一个真实迭代驱动正在运行') from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def revision_number(label):
    if label is None:
        return 0
    if not isinstance(label, str) or not re.fullmatch(r'R[0-9]{3}', label):
        raise ValueError('修订标记必须如R001')
    return int(label[1:])


def validate_run_directory(directory, run):
    record = run.get('version_record', {})
    family = record.get('version')
    revision = record.get('revision')
    if (not isinstance(family, str) or not re.fullmatch(r'V[0-9]{2}', family)
            or not 1 <= int(family[1:]) <= 40):
        raise ValueError('真实父版本family必须为V01至V40')
    revision_number(revision)
    expected = family + ('-' + revision if revision is not None else '')
    if Path(directory).name != expected:
        raise ValueError('目录与embedded family/revision不一致')
    return int(family[1:]), revision_number(revision)


def verify_review_reports(parent_path, previous, evidence):
    # Reports bind the canonical message projection; envelope hashes bind exact run bytes.
    keys = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type',
            'image_code', 'image_mode', 'replace_date')
    digest = sha(json.dumps([{k: m.get(k) for k in keys} for m in previous['messages']],
                            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    folder = parent_path.parent.resolve()
    for role, name in (('main', 'R000-primary-review.md'), ('subagent', 'R000-subagent-review.md')):
        reference = evidence['reviews'][role].get('report')
        report = ((folder.parent / reference) if reference else (folder / 'reviews' / name)).resolve()
        if (not report.is_relative_to(folder) or not report.is_file()
                or digest not in report.read_text(encoding='utf-8').lower()):
            raise ValueError(f'{role} 缺少绑定真实父稿消息哈希的审阅报告')


def read_inputs(parent_path, issues_path, version, allow_reviewed_best=False, revision_label=None):
    revision_number(revision_label)
    parent_bytes = parent_path.read_bytes()
    previous = json.loads(parent_bytes)
    template = json.loads((parent_path.parent / 'template.json').read_bytes())
    evidence = json.loads(issues_path.read_bytes())
    if len(previous.get('messages', [])) != TARGET:
        raise ValueError('父版本必须有实际完整150条，空稿和半稿不能作为下一版本')
    expected = f'V{version - 1:02d}'
    parent_version = previous.get('version_record', {}).get('version')
    # Validate the embedded family before directory checks to retain ordinary rejection reasons.
    if (not isinstance(parent_version, str) or not re.fullmatch(r'V[0-9]{2}', parent_version)
            or not 1 <= int(parent_version[1:]) <= 40):
        raise ValueError('最佳基线分支必须使用编号严格早于目标V的真实父版本')
    parent_family = int(parent_version[1:])
    same_family_revision = revision_label is not None and parent_family == version
    if same_family_revision:
        parent_revision = revision_number(previous.get('version_record', {}).get('revision'))
        if revision_number(revision_label) <= parent_revision:
            raise ValueError('同family目标revision必须严格大于父revision')
    elif allow_reviewed_best:
        if (not isinstance(parent_version, str) or len(parent_version) != 3
                or not parent_version.startswith('V') or not parent_version[1:].isdigit()
                or not 1 <= int(parent_version[1:]) < version):
            raise ValueError('最佳基线分支必须使用编号严格早于目标V的真实父版本')
    elif parent_version != expected:
        raise ValueError(f'必须顺序迭代：当前版本的父版本应为 {expected}')
    validate_run_directory(parent_path.parent, previous)
    if previous.get('template_id') != template.get('id'):
        raise ValueError('父版本与母本不匹配')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('issues'), list):
        raise ValueError('issues JSON 必须包含 issues 数组和双人全文审阅 reviews')
    for role in ('main', 'subagent'):
        review = evidence.get('reviews', {}).get(role, {})
        covered = (role == 'main' and review.get('full_read') is False
                   and review.get('coverage_mode') == 'verified_parent_and_all_changes'
                   and review.get('all_changes_read') is True
                   and review.get('parent_full_read_before') is True
                   and review.get('comparison_total') == TARGET)
        if ((review.get('full_read') is not True and not covered) or review.get('message_count') != TARGET
                or review.get('run_sha256') != sha(parent_bytes)
                or not str(review.get('summary', '')).strip()):
            raise ValueError(f'{role} 尚未提交对应父稿哈希的150条全文审阅')
    if revision_label is not None:
        verify_review_reports(parent_path, previous, evidence)
    return previous, template, evidence, sha(parent_bytes)


def provenance():
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)
    files = [Path(__file__), *sorted((ROOT / 'dialogue/backend/app').glob('*.py'))]
    return {'commit': commit, 'working_tree_status': status,
            'source_hashes': {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in files},
            'model': MODEL, 'reasoning_effort': EFFORT, 'streaming': True, 'max_retries': 0}


def configure_environment(out):
    # Must happen before importing app.main: its module import initializes SQLite.
    if 'app.main' in sys.modules:
        raise RuntimeError('拒绝复用已导入的 app.main；请在独立 Python 进程运行')
    os.environ['SCRIPT_GENERATOR_DATA_DIR'] = str((out / '.runtime').resolve())
    if not os.environ.get('MODEL_API_BASE_URL') or not os.environ.get('MODEL_API_KEY'):
        import yaml
        private = yaml.safe_load((ROOT / '.vimax/deploy/runtime-model.yaml').read_text(encoding='utf-8'))['llm']
        os.environ.setdefault('MODEL_API_BASE_URL', private['base_url'])
        os.environ.setdefault('MODEL_API_KEY', private['api_key'])
    os.environ.update(MODEL_NAME=MODEL, MODEL_DIALOGUE_NAME=MODEL, MODEL_REVIEW_NAME=MODEL,
                      MODEL_REASONING_EFFORT=EFFORT, MODEL_TIMEOUT_SECONDS='300', MODEL_CALL_BUDGET_SECONDS='360')
    sys.path[:0] = [str(ROOT), str(ROOT / 'dialogue/backend')]


def isolated_create(api, run):
    """Equivalent create_run persistence, without the global SessionIndex side effect."""
    with api.db() as connection:
        api.bind_characters(connection, run)
        connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)',
                           (run['id'], run['template_id'], json.dumps(run, ensure_ascii=False), api.now()))


async def create_child(api, previous, template, version, parent_hash, evidence, code):
    config = {k: copy.deepcopy(v) for k, v in previous['config'].items() if k in api.SimulationConfig.model_fields}
    config.update(template_id=template['id'], target_messages=TARGET, dialogue_model=MODEL,
                  reasoning_effort=EFFORT, creation_mode='fixed', character_ids={}, story_world=f'natural-20261015-v{version:02d}-{uuid.uuid4().hex[:12]}')
    original_create = api.create_run
    api.create_run = lambda run: isolated_create(api, run)
    try:
        result = await api.start_simulation(api.SimulationConfig(**config))
        # start_simulation currently contains no internal await. Cancel before the
        # event loop gets a chance to start process_run/fresh planning.
        task = api.tasks.pop(result['id'])
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        api.create_run = original_create
    run = api.get_run(result['id'])
    # Preserve the exact parent persona snapshot (including source/version metadata).
    for key in ('personas', 'persona_cards', 'persona_sources', 'persona_versions'):
        if key in previous['config']:
            run['config'][key] = copy.deepcopy(previous['config'][key])
    for key in ('vimax_story', 'person_situations', 'conversation_threads'):
        if key in previous:
            run[key] = copy.deepcopy(previous[key])
    run['messages'] = copy.deepcopy(previous['messages'])  # editing input only
    run['review_progress'] = {'phase': 'human_revision', 'repair_rounds': 0}
    run['version_record'] = {'version': f'V{version:02d}', 'parent_run': previous['id'],
                             'parent_version': previous.get('version_record', {}).get('version', '') +
                                 ('-' + previous['version_record']['revision'] if previous.get('version_record', {}).get('revision') else ''),
                             'parent_sha256': parent_hash, 'code': code['commit'],
                             'provenance': code, 'model': MODEL, 'reasoning_effort': EFFORT,
                             'method': 'one ViMax full revision, one program review; no automatic repair',
                             'human_reviews': copy.deepcopy(evidence['reviews'])}
    # Bind again after restoring exact persona fields; no accepted parent memories.
    run['messages'] = []
    with api.db() as connection:
        api.bind_characters(connection, run)
    api.save_run(run)
    return run


def capture_artifacts(api, out, run, export_xlsx):
    for record in run.get('candidate_versions', []):
        source = (api.DATA_DIR / record['path']).resolve()
        if not source.is_relative_to(api.DATA_DIR.resolve() / 'versions' / run['id']):
            raise ValueError('候选快照路径不属于当前独立任务')
        destination = out / 'snapshots' / source.name
        manifest = json.loads((source / 'manifest.json').read_bytes())
        if not destination.exists():
            destination.parent.mkdir(exist_ok=True)
            staging = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex)
            try:
                shutil.copytree(source, staging)
                os.rename(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        for name, digest in manifest['files'].items():
            if sha((destination / name).read_bytes()) != digest:
                raise ValueError('候选快照哈希校验失败：' + name)
        if sha((destination / 'manifest.json').read_bytes()) != sha((source / 'manifest.json').read_bytes()):
            raise ValueError('候选快照清单哈希不一致')
    save_json(out / 'run.json', run)
    progress = {k: run.get(k) for k in ('id', 'status', 'errors', 'review_progress', 'candidate_versions', 'outcome_review')}
    progress.update(actual_message_count=len(run.get('messages', [])), updated_at=now())
    save_json(out / 'progress.json', progress)
    attempts = out / 'attempts'
    attempts.mkdir(exist_ok=True)
    for index, sample in enumerate(run.get('vimax_samples', []), 1):
        path = attempts / f'call-{index:03d}.json'
        payload = json_bytes(sample)
        if path.exists() and path.read_bytes() != payload:
            raise ValueError('原始调用记录不可覆盖')
        if not path.exists():
            atomic(path, payload)
    if len(run.get('messages', [])) == TARGET:
        atomic(out / '群聊150条.xlsx', export_xlsx(run))
        lines = []
        for message in run['messages']:
            lines.append(f"{message['seq']:03d} **{message['sender']}**：{message['content']}")
            if message.get('image_code'):
                lines.append('[待补截图素材：' + message['image_code'] + ']')
        atomic(out / '群聊150条.md', '\n\n'.join(lines).encode('utf-8'))
    files = [p for p in out.rglob('*') if p.is_file() and '.runtime' not in p.parts and p.name != 'hashes.json']
    save_json(out / 'hashes.json', {p.relative_to(out).as_posix(): sha(p.read_bytes()) for p in sorted(files)})


async def broadcast(api, engine, template, run):
    """Same run_group_chat and public-memory implementation as vimax_engine.process."""
    run['vimax_draft'] = copy.deepcopy(run['messages'])
    run['messages'] = []
    run['cursor'] = 0
    engine.rebuild_memories(run)
    api.save_run(run)
    while run['cursor'] < len(run['vimax_draft']):
        remaining = run['vimax_draft'][run['cursor']:run['cursor'] + 12]
        async def own_turn(name, index, public, session):
            return dict(remaining[index])
        accepted = await engine.run_group_chat(run, [{'speaker': m['sender']} for m in remaining], own_turn)
        if not accepted:
            raise ValueError('ViMax 播送未接受任何消息')
        for message in accepted:
            message['source_rows'] = template['analysis'].get('evidence_rows', []) if message['sender'] == '坐席' else []
            run['messages'].append(message)
            engine.record_public_memory(run['agent_sessions'], message, run['messages'])
            own = run['agent_sessions'][message['sender']]
            own.update(memory=message['content'], last_seq=message['seq'], turns=own['turns'] + 1)
            run['cursor'] += 1
        run['review_progress']['phase'] = 'broadcast'
        api.save_run(run)


async def review_only(api, engine, template, run):
    if len(run.get('messages', [])) != TARGET:
        raise ValueError('程序复核只接受实际完整150条候选')
    before = sha(json_bytes(run['messages']))
    run['status'] = 'running'
    run['review_progress']['phase'] = 'review'
    api.save_run(run)
    token = api._run_reasoning_effort.set(EFFORT)
    try:
        review = await api.review_dialogue(template, run)
    finally:
        api._run_reasoning_effort.reset(token)
    run.setdefault('program_review_history', []).append({'at': now(), 'messages_sha256': before,
                                                        'model': MODEL, 'reasoning_effort': EFFORT, 'review': copy.deepcopy(review)})
    run['quality_review'] = review['issues']
    run['outcome_review'] = {'met': review['goal_met'], 'reason': review['goal_reason']}
    run['errors'] = engine.contract_errors(template['source'], {**run, 'status': 'completed'})
    run['errors'].extend(str(issue['reason']) for issue in review['issues'])
    if not review['goal_met']:
        run['errors'].append('坐席目标未完成：' + review['goal_reason'])
    run['status'] = 'failed' if run['errors'] else 'completed'
    run['naturalness_report'] = engine.naturalness_evidence(run['messages'])
    if run['status'] == 'completed':
        scope = {**run, 'cursor': 0}
        engine.apply_review_actions(scope, run['messages'], review.get('action_updates'), len(template['analysis'].get('workflow_steps', [])))
        engine.apply_review_decisions(scope, run['messages'], review.get('decision_updates'))
        engine.rebuild_memories(run)
        run['memory_reflections'] = engine.normalize_reflections(run, [], review.get('memory_reflections'))
    run['review_progress']['phase'] = 'done'
    api.save_run(run)


async def revision_once(api, engine, template, run, parent_messages, issues, chat):
    editing = copy.deepcopy(run)
    editing['messages'] = copy.deepcopy(parent_messages)
    try:
        changes = await engine.revise(engine.screenwriter_class(), chat, api, run['id'], template, editing, issues,
                                      '主 Agent 与独立 subagent 对父版本150条全文审阅后的下一版修订。')
        if content_fingerprint(editing) == content_fingerprint({'messages': parent_messages}):
            raise NoChangeError('模型完整响应与父台词正文、发言人和媒体相同；仅保留尝试，不计为新完整版本')
        editing['review_revisions'] = changes
        # revise checkpoints before validation; merge raw calls captured in the DB.
        latest = api.get_run(run['id'])
        for key in ('vimax_calls', 'vimax_raw', 'vimax_samples', 'candidate_versions'):
            editing[key] = copy.deepcopy(latest.get(key, {} if key == 'vimax_raw' else []))
        run = editing
        api.save_run(run)
        await broadcast(api, engine, template, run)
        return run
    except BaseException as exc:
        latest = api.get_run(run['id'])
        records = latest.get('candidate_versions', [])
        if records and not isinstance(exc, NoChangeError):
            # Rejecting a complete draft never falls back to the parent's wording.
            captured = json.loads((api.DATA_DIR / records[-1]['path'] / 'run.json').read_bytes())
            latest['messages'] = captured['messages']
            latest['cursor'] = len(latest['messages'])
            latest['candidate_origin'] = 'new ViMax revision captured before rejection'
        else:
            latest['messages'] = []
            latest['cursor'] = 0
            if isinstance(exc, NoChangeError):
                latest['unchanged_parent_candidate'] = True
        api.save_run(latest)
        raise


def dialogue_fingerprint(run):
    keys = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type', 'image_code', 'image_mode', 'replace_date')
    return sha(json_bytes([{k: message.get(k) for k in keys} for message in run['messages']]))


def content_fingerprint(run):
    keys = ('sender', 'content', 'type', 'image_code', 'image_mode')
    return sha(json_bytes([{k: message.get(k) for k in keys} for message in run['messages']]))


async def execute(args):
    version = args.version
    revision_label = getattr(args, 'revision_label', None)
    label = f'V{version:02d}' + ('-' + revision_label if revision_label else '')
    out = ROOT / 'outputs/自然群聊-20261015' / label
    parent = args.parent.resolve()
    previous, template, evidence, parent_hash = read_inputs(
        parent, args.issues, version, revision_label=revision_label)
    if args.resume_review:
        run = json.loads((out / 'run.json').read_bytes())
        if run['status'] == 'completed' or len(run.get('messages', [])) != TARGET:
            raise ValueError('只能复核未终态或失败的同一完整候选；完整成功版本不可覆盖')
        if run['version_record']['parent_sha256'] != parent_hash:
            raise ValueError('恢复复核的父版本哈希不匹配')
        if run.get('version_record', {}).get('version') != f'V{version:02d}':
            raise ValueError('恢复复核版本不匹配')
        if run.get('version_record', {}).get('revision') != revision_label:
            raise ValueError('恢复复核修订标记不匹配')
        validate_run_directory(out, run)
    else:
        out.mkdir(parents=True, exist_ok=False)
        save_json(out / 'template.json', template)
        save_json(out / 'input-issues.json', evidence)
        save_json(out / 'provenance.json', provenance())
    configure_environment(out)
    api = importlib.import_module('app.main')
    engine = importlib.import_module('app.vimax_engine')
    from app.core import export_xlsx
    if not args.resume_review:
        api.put_template(template['id'], template['filename'], template['source'], template['analysis'])
        run = await create_child(api, previous, template, version, parent_hash, evidence,
                                 json.loads((out / 'provenance.json').read_bytes()))
        if revision_label:
            run['version_record']['revision'] = revision_label
            api.save_run(run)
        validate_run_directory(out, run)
        atomic(out / 'run-id.txt', run['id'].encode('ascii'))
    else:
        run.setdefault('resume_review_history', []).append({'at': now(), 'previous_status': run['status'],
                                                           'previous_errors': copy.deepcopy(run.get('errors', [])),
                                                           'messages_sha256': dialogue_fingerprint(run),
                                                           'provenance': provenance()})
        api.save_run(run)
    capture_artifacts(api, out, run, export_xlsx)

    async def work():
        nonlocal run
        if not args.resume_review:
            import httpx
            from langchain_openai import ChatOpenAI
            async with httpx.AsyncClient(trust_env=False, timeout=300) as client:
                base, key, _ = api.model_settings()
                chat = ChatOpenAI(model=MODEL, api_key=key, base_url=base, streaming=True, max_retries=0,
                                  max_tokens=16000, timeout=300, reasoning_effort=EFFORT, http_async_client=client)
                run = await revision_once(api, engine, template, run, previous['messages'], evidence['issues'], chat)
        before = dialogue_fingerprint(run)
        await review_only(api, engine, template, run)
        if before != dialogue_fingerprint(run):
            raise ValueError('复核改变了台词或媒体契约，拒绝完成')
        engine.checkpoint_candidate(api, run, run['messages'], '本版程序全文复核结束；双人全文审阅须另行记录')

    task = asyncio.create_task(work())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=15)
            capture_artifacts(api, out, api.get_run(run['id']), export_xlsx)
        await task
    except BaseException as exc:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        run = api.get_run(run['id'])
        # Avoid ever exposing credentials in errors or saved artifacts.
        reason = str(exc)[:1200]
        for value in (os.environ.get('MODEL_API_KEY'), os.environ.get('MODEL_API_BASE_URL')):
            if value:
                reason = reason.replace(value, '[private model setting]')
        run['status'] = 'failed'
        run.setdefault('errors', []).append(type(exc).__name__ + ': ' + reason)
        run['review_progress']['phase'] = 'failed'
        api.save_run(run)
    finally:
        run = api.get_run(run['id'])
        capture_artifacts(api, out, run, export_xlsx)
        history = out / 'attempts.jsonl'
        entry = {'at': now(), 'status': run['status'], 'messages': len(run['messages']),
                 'resume_review': args.resume_review, 'errors': run.get('errors', []),
                 'candidate_versions': run.get('candidate_versions', [])}
        atomic(history, (history.read_bytes() if history.exists() else b'') + json.dumps(entry, ensure_ascii=False).encode('utf-8') + b'\n')
        capture_artifacts(api, out, run, export_xlsx)
    print(json.dumps({'version': f'V{version:02d}', 'id': run['id'], 'status': run['status'],
                      'messages': len(run['messages']), 'snapshots': len(run.get('candidate_versions', []))}, ensure_ascii=True))
    return 0 if run['status'] == 'completed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, type=int, choices=range(4, 41))
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--issues', required=True, type=Path)
    parser.add_argument('--resume-review', action='store_true')
    parser.add_argument('--revision-label', help='另建同family的更高修订如V40-R001；普通调用保持更早family规则')
    args = parser.parse_args()
    revision_number(args.revision_label)
    with exclusive_lock(ROOT / '.vimax/deploy/evolve-natural.lock'):
        return asyncio.run(execute(args))


if __name__ == '__main__':
    raise SystemExit(main())
