"""Register local independent attempts and explicit full human review evidence.

No model, network, application database or old evolution registry is accessed.
Unreviewed attempts may be registered, including empty/partial failed attempts.
Candidate/alternative/needs_revision requires reviews/review-evidence.json:
{series,version,run_sha256,message_sha256,reviews:{main:{reviewer_id,full_read:true,
 message_count:150,report:'reviews/R000-primary-review.md'},subagent:{...}}}.
Each reviewer also declares reviewer_kind='agent'. Reports must contain the
exact canonical message hash. These agent reviews do not imply user approval.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('independent_registration_utilities', HERE / 'evolve-natural.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
OUT = base.ROOT / 'outputs/独立随机-20261015'
SERIES = '独立随机-20261015'
FIELDS = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type',
          'image_code', 'image_mode', 'replace_date')
REPORTS = {'main': 'R000-primary-review.md', 'subagent': 'R000-subagent-review.md'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(path):
    return base.sha(path.read_bytes())


def canonical_hash(messages, fields=FIELDS):
    return base.sha(json.dumps([{key: item.get(key) for key in fields} for item in messages],
                               ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def safe_file(root, relative):
    require(isinstance(relative, str) and '\\' not in relative, '归档文件路径必须是安全的相对路径')
    parts = PurePosixPath(relative)
    require(not parts.is_absolute() and '..' not in parts.parts and ':' not in relative,
            '归档文件路径不能越出本版目录')
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()) and path.is_file(), '归档文件缺失或路径越出本版目录：' + relative)
    return path


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('归档JSON无法读取：' + str(path)) from exc


def label_for(index, attempt_label=None):
    require(type(index) is int and 1 <= index <= 40, '独立编号必须在1至40')
    if attempt_label is not None:
        require(isinstance(attempt_label, str) and bool(re.fullmatch(r'R[0-9]{3}', attempt_label))
                and int(attempt_label[1:]) > 0, '新尝试标记必须为R001至R999')
    return f'V{index:02d}' + ('-' + attempt_label if attempt_label else '')


def validate_artifacts(folder, index, attempt_label):
    label = label_for(index, attempt_label)
    run = read_json(folder / 'run.json')
    record = run.get('version_record', {})
    require(isinstance(run.get('id'), str) and bool(re.fullmatch(r'[0-9a-f]{32}', run['id'])), '独立run编号必须为32位小写十六进制')
    require(folder.name == label and record.get('version') == f'V{index:02d}'
            and record.get('revision') == attempt_label, '目录编号与run内独立编号或attempt不一致')
    require(record.get('series') == SERIES and record.get('method') == 'independent_generation', '不是本独立随机系列')
    require(all(record.get(key) is None for key in ('parent_run', 'parent_version', 'parent_sha', 'parent_sha256')),
            '独立生成不能存在父稿引用')
    require(run.get('status') in {'completed', 'failed', 'paused'}, '运行中的稿件不能冻结登记')
    messages = run.get('messages', [])
    require(isinstance(messages, list) and len(messages) <= 150 and all(isinstance(item, dict) for item in messages),
            '实际消息必须是最多150条对象')
    require([item.get('seq') for item in messages] == list(range(1, len(messages) + 1)), '实际消息编号不连续')
    require(run.get('config', {}).get('target_messages') == 150, '独立稿目标必须为150条')
    proof, variation = run.get('independence', {}), run.get('creative_variation', {})
    require(proof.get('initial_messages') == 0 and proof.get('initial_story', 'missing') is None
            and proof.get('parent_run', 'missing') is None and variation.get('parent_run', 'missing') is None,
            '缺少空上下文独立创建证据')
    require(proof.get('method') == variation.get('method') == 'independent_generation'
            and variation.get('style_label') and run['config'].get('creation_mode') == 'independent_random'
            and run['config'].get('dialogue_model') == 'gpt-6-sol' and run['config'].get('reasoning_effort') == 'high',
            '独立随机配置或模型强度证据不完整')
    require(proof.get('fresh_story_world') == run['config'].get('story_world') and proof.get('fresh_story_world'),
            '缺少本稿新world证据')
    require(record.get('seed') == run['config'].get('variation_seed') == variation.get('seed'), '独立seed不一致')
    require(variation.get('persona_cards') == run['config'].get('persona_cards') and variation.get('persona_cards'),
            '随机人物配置与本稿不一致')
    require(run.get('corpus_reference', {}).get('samples') and run['corpus_reference'].get('sources'), '缺少实际语料参考')
    hashes = read_json(folder / 'hashes.json')
    require(isinstance(hashes, dict), '文件哈希清单结构错误')
    for relative, expected in hashes.items():
        require(digest(safe_file(folder, relative)) == expected, '原始归档文件SHA不匹配：' + relative)
    critical = ('run.json', 'generation-report.json', 'review-calls.json', 'provenance.json', 'template.json', 'input.json')
    require(all(name in hashes for name in critical), '生成证据未完整绑定原始哈希清单')
    message_hash = canonical_hash(messages)
    report = read_json(folder / 'generation-report.json')
    require(report.get('series') == SERIES and report.get('version') == f'V{index:02d}'
            and report.get('attempt_label') == attempt_label and report.get('id') == run.get('id')
            and report.get('status') == run['status'] and report.get('messages') == len(messages)
            and report.get('messages_sha256') == message_hash, 'generation-report与实际终稿不一致')
    history = run.get('program_review_history', [])
    require(read_json(folder / 'review-calls.json') == history, '真实审查调用记录与run不一致')
    for entry in history:
        require(entry.get('model') == 'gpt-6-sol' and entry.get('reasoning_effort') == 'high'
                and re.fullmatch(r'[0-9a-f]{64}', entry.get('messages_sha256', ''))
                and entry.get('state') in {'started', 'returned', 'failed'}, '真实模型审查记录缺少必要证据')
    last = history[-1] if history else {}
    reviewed = (last.get('state') == 'returned' and last.get('messages_sha256') == message_hash
                and last.get('message_count') == 150 and last.get('review', {}).get('issues') == []
                and last.get('review', {}).get('goal_met') is True)
    model_passed = bool(run['status'] == 'completed' and len(messages) == 150 and not run.get('errors')
                        and run.get('quality_review') == [] and reviewed)
    require(report.get('actual_full_review_passed') is bool(reviewed) and report.get('success') is model_passed,
            '生成成功结论与真实全文审查证据不一致')
    require(record.get('counts_as_new_content') is model_passed, '本稿成功计数与生成证据不一致')
    code = read_json(folder / 'provenance.json')
    require(code == record.get('provenance') and code.get('commit') == record.get('code'), '源码来源记录与run不一致')
    require(isinstance(code.get('source_hashes'), dict) and code['source_hashes'], '缺少归档源码哈希')
    for relative, expected in code['source_hashes'].items():
        path = safe_file(folder, 'sources/' + relative)
        require(digest(path) == expected and hashes.get('sources/' + relative) == expected, '归档源码SHA不一致：' + relative)
    require(digest(folder / 'template.json') == record.get('template_sha256'), '母本快照SHA不一致')
    inputs = read_json(folder / 'input.json')
    require(inputs.get('series') == SERIES and inputs.get('index') == index
            and inputs.get('attempt_label') == attempt_label and inputs.get('seed') == record.get('seed')
            and inputs.get('template_sha256') == record.get('template_sha256')
            and inputs.get('corpus') == record.get('corpus'), '独立生成输入与母本、seed或语料来源记录不一致')
    corpus = record.get('corpus', {})
    require(isinstance(corpus.get('file_sha256'), str) and bool(re.fullmatch(r'[0-9a-f]{64}', corpus['file_sha256']))
            and isinstance(corpus.get('conversations'), int) and corpus['conversations'] > 0
            and corpus.get('sources'), '公开语料完整文件SHA或来源记录缺失')
    for source in run['corpus_reference']['sources']:
        require(source in corpus['sources'], '实际引用来源不属于本稿绑定的公开语料')
    samples = run.get('vimax_samples', [])
    require(isinstance(samples, list), '原始模型响应记录必须为数组')
    for number, sample in enumerate(samples, 1):
        relative = f'attempts/call-{number:03d}.json'
        require(relative in hashes and read_json(safe_file(folder, relative)) == sample, '原始响应记录与run不一致：' + relative)
    raw_files = list((folder / 'attempts').glob('call-*.json')) if (folder / 'attempts').is_dir() else []
    require(len(raw_files) == len(samples), '原始响应文件数量与run不一致')
    require(not model_passed or samples, '成功稿没有实际模型原始响应')
    # Every full draft, including the first rejected draft, remains independently verified.
    for snapshot in run.get('candidate_versions', []):
        relative = snapshot.get('path', '')
        prefix = f"versions/{run['id']}/"
        require(relative.startswith(prefix) and '/' not in relative[len(prefix):], '候选快照路径不属于本独立run')
        directory = 'snapshots/' + relative[len(prefix):]
        require(all(directory + '/' + name in hashes for name in ('manifest.json', 'run.json', 'messages.md', 'messages.xlsx')),
                '候选快照未绑定完整原始文件SHA')
        manifest = read_json(safe_file(folder, directory + '/manifest.json'))
        require(manifest.get('record') == snapshot and manifest.get('message_count') == 150, '候选快照身份与run不一致')
        for name, expected in manifest.get('files', {}).items():
            require(digest(safe_file(folder, directory + '/' + name)) == expected, '候选快照文件SHA不一致')
        require(set(manifest.get('files', {})) == {'run.json', 'messages.md', 'messages.xlsx'}, '候选快照文件不完整')
        captured = read_json(safe_file(folder, directory + '/run.json'))
        require(captured.get('id') == run['id'] and canonical_hash(captured.get('messages', [])) == snapshot.get('hash'),
                '候选快照正文哈希不一致')
    if model_passed:
        require(any(item.get('hash') == message_hash for item in run.get('candidate_versions', [])), '成功稿没有本稿完整快照')
    return run, message_hash, hashes, model_passed


def human_evidence(folder, run, message_hash):
    evidence_path = folder / 'reviews/review-evidence.json'
    evidence = read_json(evidence_path)
    label = folder.name
    require(len(run['messages']) == 150 and evidence.get('series') == SERIES and evidence.get('version') == label
            and evidence.get('run_sha256') == digest(folder / 'run.json')
            and evidence.get('message_sha256') == message_hash, '人工阅读证明未绑定当前完整150条run和消息SHA')
    identities, reports = set(), []
    for role, filename in REPORTS.items():
        review = evidence.get('reviews', {}).get(role, {})
        require(review.get('full_read') is True and review.get('message_count') == 150
                and review.get('reviewer_kind') == 'agent'
                and isinstance(review.get('reviewer_id'), str) and review['reviewer_id'].strip(),
                role + '未声明真实完整150条阅读及审阅者身份')
        require(review.get('report') == 'reviews/' + filename, '人工报告必须位于本版reviews固定文件')
        require(review['reviewer_id'] not in identities, '两份独立报告不能使用同一审阅者身份')
        identities.add(review['reviewer_id'])
        path = safe_file(folder, review['report'])
        require(message_hash in path.read_text(encoding='utf-8').lower(), '人工报告正文没有当前消息SHA')
        if review.get('report_sha256') is not None:
            require(review['report_sha256'] == digest(path), '人工报告SHA与阅读证明不一致')
        reports.append(label + '/' + review['report'])
    return evidence, reports


def count_indices(entries):
    def indices(predicate):
        return sorted({entry['index'] for entry in entries if predicate(entry)})
    values = {'registered_indices': indices(lambda entry: True),
              'full_payload_indices': indices(lambda entry: entry['messages'] == 150),
              'model_passed_indices': indices(lambda entry: entry['model_passed']),
              'dual_reviewed_indices': indices(lambda entry: entry['dual_full_read']),
              'human_candidate_indices': indices(lambda entry: entry['human_verdict'] == 'candidate')}
    return {**{key: len(value) for key, value in values.items()}, 'indices': values,
            'registered_attempts': len(entries), 'requested_indices': 40,
            'all_40_model_passed': len(values['model_passed_indices']) == 40,
            'recommended_indices': 0}


def validate_registry(entries):
    require(isinstance(entries, list), '独立注册表必须是数组')
    labels, run_ids = set(), set()
    for entry in entries:
        require(isinstance(entry, dict) and entry.get('series') == SERIES, '独立注册表不能混入其他系列')
        match = re.fullmatch(r'V([0-9]{2})(?:-(R[0-9]{3}))?', entry.get('version', ''))
        require(match is not None and entry.get('index') == int(match[1])
                and label_for(entry['index'], match[2]) == entry['version'], '独立注册表编号与attempt不一致')
        require(entry['version'] not in labels and entry.get('run_id') not in run_ids, '独立注册表不能重复label或复用旧run')
        labels.add(entry['version'])
        run_ids.add(entry.get('run_id'))


def evidence_inventory(folder):
    """Freeze all existing safe evidence, including reports added after capture.

    Only top-level registration derivatives, immutable registration events and
    the isolated runtime are excluded. Capture-time hashes are not an inventory
    of files that may have been added between capture and registration.
    """
    excluded = {'hashes.json', 'review-index.json', 'registration.json'}
    result = {}
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        if (not path.is_file() or '.runtime' in relative.parts or relative.parts[0] == 'registrations'
                or relative.as_posix() in excluded):
            continue
        result[relative.as_posix()] = digest(safe_file(folder, relative.as_posix()))
    return result


def register(index, attempt_label=None, verdict='unreviewed', summary='', reassess=False, root=None):
    root = Path(root or OUT).resolve()
    label = label_for(index, attempt_label)
    require(verdict in {'unreviewed', 'candidate', 'alternative', 'needs_revision'} and str(summary).strip(), '需要有效人工分类及实质说明')
    folder = root / label
    require(folder.resolve().is_relative_to(root) and folder.is_dir() and not folder.is_symlink(), '本版目录缺失或不安全')
    # One lock protects all forty indices and explicit reassessments.
    with base.exclusive_lock(root / '.register.lock'):
        run, message_hash, original_hashes, model_passed = validate_artifacts(folder, index, attempt_label)
        entries = read_json(root / 'versions.json') if (root / 'versions.json').is_file() else []
        validate_registry(entries)
        previous = next((entry for entry in entries if entry.get('version') == label), None)
        require(previous is not None or all(item.get('run_id') != run['id'] for item in entries),
                '新独立attempt不能复用已经登记的run')
        if previous:
            require(previous['run_sha256'] == digest(folder / 'run.json'), '已登记真实run不可覆盖')
            for relative, expected in previous['evidence_files'].items():
                require(digest(safe_file(folder, relative)) == expected, '已冻结的生成或人工证据发生变化：' + relative)
        evidence, reports = (None, []) if verdict == 'unreviewed' else human_evidence(folder, run, message_hash)
        human_index = None
        if evidence is not None:
            human_index = {'version': label, 'series': SERIES, 'message_sha256': message_hash,
                           'run_sha256': digest(folder / 'run.json'), 'verdict': verdict, 'summary': summary,
                           'program_review_deferred': bool(run.get('program_review_deferred')), 'reports': reports,
                           'dual_full_read': True, 'review_evidence_sha256': digest(folder / 'reviews/review-evidence.json')}
            human_index.update(reviewer_kind='agent', user_approval='unset')
        inventory = evidence_inventory(folder)
        if previous and not reassess:
            # Old registrations may omit a report that the final hashes.json
            # already bound. Validate that legacy manifest-bound file above,
            # but never rewrite its old event by expanding the frozen set.
            unexpected = set(inventory) - set(previous['evidence_files']) - set(original_hashes)
            require(not unexpected, '发现新增未冻结证据；必须显式reassess追加登记：' + ','.join(sorted(unexpected)))
            evidence_files = copy.deepcopy(previous['evidence_files'])
        else:
            evidence_files = inventory
        if evidence is not None:
            for relative in ['reviews/review-evidence.json'] + [path.split('/', 1)[1] for path in reports]:
                evidence_files[relative] = digest(safe_file(folder, relative))
        entry = {'index': index, 'version': label, 'series': SERIES, 'method': 'independent_generation',
                 'parent_run': None, 'run_id': run['id'], 'run_sha256': digest(folder / 'run.json'),
                 'message_sha256': message_hash, 'messages': len(run['messages']), 'status': run['status'],
                 'model_passed': model_passed, 'program_review_executed': bool(run.get('program_review_history')),
                 'dual_full_read': evidence is not None, 'human_verdict': verdict, 'review': summary,
                 'reviewer_kind': 'agent', 'agent_verdict': verdict, 'user_approval': 'unset',
                 'selected': False, 'recommended': False, 'publication_eligible': evidence is not None,
                 'generation_code': run['version_record']['code'], 'seed': run['version_record']['seed'],
                 'artifacts': label + '/', 'review_reports': reports, 'evidence_files': evidence_files}
        if previous:
            if previous == entry:
                return previous
            require(reassess, '分类或说明已登记；必须显式reassess追加重评，不覆盖旧证据')
        event = {'schema_version': 1, 'entry': entry, 'human_evidence': evidence, 'publication_review': human_index}
        event_sha = base.sha(base.json_bytes(event))
        event_path = folder / 'registrations' / (event_sha + '.json')
        if event_path.exists():
            require(read_json(event_path) == event, '注册历史事件不可覆盖')
        else:
            base.save_json(event_path, event)
        if human_index is not None:
            base.save_json(folder / 'review-index.json', human_index)
        base.save_json(folder / 'registration.json', {'event': event_path.relative_to(folder).as_posix(), 'event_sha256': event_sha})
        checks = {path.relative_to(folder).as_posix(): digest(safe_file(folder, path.relative_to(folder).as_posix())) for path in folder.rglob('*')
                  if path.is_file() and '.runtime' not in path.relative_to(folder).parts and path.name != 'hashes.json'}
        base.save_json(folder / 'hashes.json', checks)
        entries = [entry if item.get('version') == label else item for item in entries]
        if previous is None:
            entries.append(entry)
        validate_registry(entries)
        entries.sort(key=lambda item: (item['index'], item['version']))
        base.save_json(root / 'versions.json', entries)
        base.save_json(root / 'independent-registry.json', {'schema_version': 1, 'series': SERIES,
                                                          'counts': count_indices(entries), 'attempts': entries})
        return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=int, choices=range(1, 41), required=True)
    parser.add_argument('--attempt-label')
    parser.add_argument('--verdict', choices=('unreviewed', 'candidate', 'alternative', 'needs_revision'), default='unreviewed')
    parser.add_argument('--summary', required=True)
    parser.add_argument('--reassess', action='store_true')
    args = parser.parse_args()
    entry = register(args.index, args.attempt_label, args.verdict, args.summary, args.reassess)
    print(json.dumps({key: entry[key] for key in ('version', 'status', 'messages', 'model_passed', 'human_verdict',
                                                  'dual_full_read', 'recommended', 'message_sha256')}, ensure_ascii=True))


if __name__ == '__main__':
    raise SystemExit(main())
