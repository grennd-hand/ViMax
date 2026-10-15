"""Generate one fresh 150-message dialogue from the mother template only.

No parent run, old messages, old story or patch input is accepted. Every index
owns a new output directory, SQLite runtime, random cast and fresh story world.
This command never resumes or overwrites an existing attempt.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import random


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('independent_evolution_utilities', HERE / 'evolve-natural.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
OUT_ROOT = base.ROOT / 'outputs/独立随机-20261015'
DEFAULT_TEMPLATE = base.ROOT / 'outputs/自然群聊-20261015/V03/template.json'
SERIES = '独立随机-20261015'
MESSAGE_FIELDS = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type',
                  'image_code', 'image_mode', 'replace_date')


def messages_hash(messages):
    return base.sha(json.dumps([{key: message.get(key) for key in MESSAGE_FIELDS} for message in messages],
                               ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def read_template(path):
    path = Path(path).resolve()
    if path.suffix.lower() != '.json' or path.name.lower() in {'run.json', 'messages.json', 'story.json'}:
        raise ValueError('只能读取母本template快照，不接受旧run、messages或story文件')
    try:
        payload = path.read_bytes()
        template = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('母本快照无法读取或JSON格式错误') from exc
    if (not isinstance(template, dict) or not all(template.get(key) for key in ('id', 'filename', 'source', 'analysis'))
            or any(key in template for key in ('messages', 'config', 'vimax_story', 'agent_sessions', 'schedule', 'pending'))
            or not isinstance(template['source'], dict) or not isinstance(template['source'].get('messages'), list)
            or not isinstance(template['analysis'], dict) or not template['analysis'].get('confirmed')):
        raise ValueError('需要已确认目标的母本template快照，不能使用旧稿上下文')
    return template, payload


def read_material_notes(path, index, seed, corpus, corpus_sha256):
    """Validate agent annotations against this fresh run's actual corpus sample."""
    from app.dialogue_corpus import sample_reference
    path = Path(path).resolve()
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('选材注释无法读取或JSON格式错误') from exc
    if (not isinstance(document, dict) or document.get('annotation_kind') != 'agent_creative_inspiration'
            or document.get('not_original_dialogue') is not True):
        raise ValueError('选材文件必须明确为Agent创作启发，不能冒充原始对话')
    if document.get('corpus_sha256') != corpus_sha256:
        raise ValueError('选材注释绑定的语料SHA与实际语料文件不一致')
    entries = document.get('entries')
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError('选材注释缺少entries对象列表')
    label = f'V{index:02d}'
    matches = [entry for entry in entries if entry.get('version') == label or entry.get('seed') == seed]
    if (len(matches) != 1 or matches[0].get('version') != label or matches[0].get('seed') != seed
            or matches[0].get('index', index) != index):
        raise ValueError('选材注释的版本和seed必须唯一且同时匹配本次新稿')
    entry = matches[0]
    if entry.get('annotation_kind') != 'agent_creative_inspiration' or entry.get('corpus_sha256') != corpus_sha256:
        raise ValueError('所选条目缺少创作启发标记或语料SHA错绑')
    samples = sample_reference(corpus, seed, count=4, max_turns=4)['samples']
    identifiers = [sample['id'] for sample in samples]
    if len(identifiers) != 4 or entry.get('conversation_ids') != identifiers:
        raise ValueError('选材条目的4个会话ID与本次实际抽样不完全匹配')
    bindings = entry.get('reference_bindings')
    if not isinstance(bindings, list) or len(bindings) != 4:
        raise ValueError('选材条目缺少4个完整语料来源绑定')
    sources = {source['source_id']: source for source in corpus['sources']}
    for sample, binding in zip(samples, bindings):
        expected_sources = [{'source_id': attribution['source_id'],
                             'source_record_index': attribution['source_record_index'],
                             'source_sha256': sources[attribution['source_id']]['source_sha256']}
                            for attribution in sample['attributions']]
        if (not isinstance(binding, dict) or binding.get('conversation_id') != sample['id']
                or binding.get('conversation_content_sha256') != sample['content_sha256']
                or binding.get('excerpt_start') != sample['excerpt_start']
                or binding.get('source_turn_indexes') != [turn['source_turn_index'] for turn in sample['turns']]
                or binding.get('source_sha256_bindings') != expected_sources):
            raise ValueError('选材条目的会话、回合位置或来源SHA绑定错误')
    hints = entry.get('material_hints')
    if not isinstance(hints, list) or not 2 <= len(hints) <= 3:
        raise ValueError('所选创作启发条目需要2至3项话头')
    for hint in hints:
        if (not isinstance(hint, dict)
                or any(not isinstance(hint.get(key), str) or not hint[key].strip()
                       for key in ('topic_hint', 'creative_prompt', 'expression_impulse'))
                or not isinstance(hint.get('inspired_by_conversation_ids'), list)
                or not hint['inspired_by_conversation_ids']
                or any(identifier not in identifiers for identifier in hint['inspired_by_conversation_ids'])):
            raise ValueError('创作话头缺少内容、表达欲或实际来源会话ID')
    snapshot = {'annotation_kind': 'agent_creative_inspiration', 'not_original_dialogue': True,
                'source_path': str(path), 'source_file_sha256': base.sha(payload),
                'selected_entry_sha256': base.sha(json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                                           separators=(',', ':')).encode()),
                'selected_entry': copy.deepcopy(entry)}
    material_story_notes(snapshot)  # Validate the model input length before starting any task.
    return snapshot


def material_story_notes(snapshot):
    if snapshot is None:
        return ''
    hints = [{key: hint[key] for key in ('topic_hint', 'creative_prompt', 'expression_impulse')}
             for hint in snapshot['selected_entry']['material_hints']]
    text = ('Agent创作启发，非原语料/当前人物事实。以下仅是本份可选的话头与表达欲，可以转化、择取或不用。'
            '它们不是逐句指令，不规定事件顺序、操作者、发言人、篇幅或业务比例；'
            '不复制原故事、原人物身份或关系，不把它们当作当前群友已经公开的经历。'
            '业务事实和行动前因仍以母本及本份公开聊天为准。\n' + json.dumps(hints, ensure_ascii=False))
    if len(text) > 3000:
        raise ValueError('选材启发超出补充素材长度，请精简至3000字以内')
    return text


def material_provenance(snapshot):
    return {key: copy.deepcopy(snapshot[key]) for key in
            ('annotation_kind', 'not_original_dialogue', 'source_path', 'source_file_sha256', 'selected_entry_sha256')} | {
                'archive_path': 'material-notes.json', 'seed': snapshot['selected_entry']['seed'],
                'version': snapshot['selected_entry']['version'],
                'conversation_ids': copy.deepcopy(snapshot['selected_entry']['conversation_ids'])}


def choose_customer_count(api, source, seed, requested=None):
    def feasible(count):
        try:
            if api.min_total(source, count) > base.TARGET:
                return False
            api.build_schedule(source, count, base.TARGET)
            return True
        except ValueError:
            return False
    if requested is not None:
        if type(requested) is not int or not 1 <= requested <= 20 or not feasible(requested):
            raise ValueError('指定客户人数不能在150条内满足母本，请使用允许的1至20人')
        return requested
    candidates = [count for count in range(3, 13) if feasible(count)]
    if not candidates:
        candidates = [count for count in range(1, 21) if feasible(count)]
    if not candidates:
        raise ValueError('母本没有能在150条内完成的客户人数')
    return random.Random('independent-customer-count:' + str(seed)).choice(candidates)


def archive_sources(out):
    code = base.provenance()
    code['source_hashes'][Path(__file__).relative_to(base.ROOT).as_posix()] = base.sha(Path(__file__).read_bytes())
    source_root = (out / 'sources').resolve()
    for relative, digest in code['source_hashes'].items():
        reference = Path(relative)
        source = (base.ROOT / reference).resolve()
        target = (source_root / reference).resolve()
        if (reference.is_absolute() or not source.is_relative_to(base.ROOT.resolve())
                or not target.is_relative_to(source_root)):
            raise ValueError('源码归档路径越出仓库或本版sources目录')
        payload = source.read_bytes()
        if base.sha(payload) != digest:
            raise ValueError('源码在归档前变化，请冻结源码后再生成')
        base.atomic(target, payload)
    base.save_json(out / 'provenance.json', code)
    return code


def _private_safe_error(exc):
    reason = str(exc)[:1200]
    for value in (os.environ.get('MODEL_API_KEY'), os.environ.get('MODEL_API_BASE_URL')):
        if value:
            reason = reason.replace(value, '[private model setting]')
    return type(exc).__name__ + ': ' + reason


def _persist_reviews(api, out, run, records):
    run['program_review_history'] = copy.deepcopy(records)
    api.save_run(run)
    base.save_json(out / 'review-calls.json', records)


def _successful_review(run, records):
    if not records or records[-1].get('state') != 'returned':
        return False
    last = records[-1]
    review = last.get('review', {})
    return (last['messages_sha256'] == messages_hash(run.get('messages', []))
            and last['message_count'] == base.TARGET and review.get('issues') == []
            and review.get('goal_met') is True)


async def run_fresh(api, out, template, index, seed, code, template_sha, corpus_evidence,
                    export_xlsx, customer_count=None, poll_seconds=15, attempt_label=None, material_notes=None,
                    allow_review_done_without_stop=False):
    """Await the actual start_simulation task; no cancel-and-revise shortcut."""
    count = choose_customer_count(api, template['source'], seed, customer_count)
    config = api.SimulationConfig(template_id=template['id'], customer_count=count, target_messages=base.TARGET,
                                  allow_early_end=False, creation_mode='independent_random', variation_seed=seed,
                                  randomize_personas=True, dialogue_model=base.MODEL, reasoning_effort=base.EFFORT,
                                  story_world='', character_ids={}, personas={}, persona_cards={},
                                  persona_sources={}, persona_versions={}, story_notes=material_story_notes(material_notes))
    records, created = [], []
    original_create, original_review = api.create_run, api.review_dialogue

    def isolated_create(run):
        if (run.get('messages') or run.get('pending') or run.get('vimax_story')
                or run.get('vimax_draft') or not run.get('config', {}).get('story_world')
                or run.get('config', {}).get('character_ids')
                or any(session.get('memory') or session.get('turns') for session in run.get('agent_sessions', {}).values())):
            raise ValueError('独立生成必须以空对话、空故事、空记忆和新world开始')
        if not run.get('corpus_reference'):
            raise ValueError('独立生成必须实际加载公开语料参考')
        if material_notes is not None:
            actual_ids = [sample['id'] for sample in run['corpus_reference'].get('samples', [])]
            if actual_ids != material_notes['selected_entry']['conversation_ids']:
                raise ValueError('新任务实际语料抽样与选材注释不一致')
        run['version_record'] = {
            'version': f'V{index:02d}', 'series': SERIES, 'series_date': '20261015',
            'method': 'independent_generation', 'parent_run': None, 'parent_sha': None, 'parent_sha256': None,
            'seed': seed, 'customer_count': count, 'model': base.MODEL, 'reasoning_effort': base.EFFORT,
            'template_sha256': template_sha, 'code': code['commit'], 'provenance': code,
            'corpus': corpus_evidence, 'review_mode': 'full', 'counts_as_new_content': False,
            'review_stream_policy': {'allow_done_without_stop_strict_json': allow_review_done_without_stop},
        }
        if attempt_label:
            run['version_record']['revision'] = attempt_label
        if material_notes is not None:
            run['version_record']['material_notes'] = material_provenance(material_notes)
        run['version_record']['seed_scope'] = 'random_configuration_only_not_model_output_reproduction'
        base.isolated_create(api, run)
        created.append(run['id'])

    async def tracked_review(template_argument, run):
        record = {'at': base.now(), 'messages_sha256': messages_hash(run.get('messages', [])),
                  'message_count': len(run.get('messages', [])), 'model': base.MODEL,
                  'reasoning_effort': base.EFFORT, 'state': 'started', 'evidence': 'api.review_dialogue invocation',
                  'stream_policy': {'allow_done_without_stop_strict_json': allow_review_done_without_stop}}
        records.append(record)
        _persist_reviews(api, out, run, records)
        protocols = []
        policy_token = api._run_allow_done_without_stop.set(allow_review_done_without_stop)
        protocol_token = api._run_stream_protocol_records.set(protocols)
        try:
            result = await original_review(template_argument, run)
            record.update(state='returned', finished_at=base.now(), review=copy.deepcopy(result))
            return result
        except BaseException as exc:
            record.update(state='failed', finished_at=base.now(), error=_private_safe_error(exc))
            raise
        finally:
            api._run_stream_protocol_records.reset(protocol_token)
            api._run_allow_done_without_stop.reset(policy_token)
            record['stream_protocol'] = copy.deepcopy(protocols)
            _persist_reviews(api, out, run, records)

    api.create_run, api.review_dialogue = isolated_create, tracked_review
    task, run = None, None
    try:
        result = await api.start_simulation(config)
        run = api.get_run(result['id'])
        base.atomic(out / 'run-id.txt', run['id'].encode('ascii'))
        task = api.tasks.get(run['id'])
        if task is None:
            raise ValueError('新任务没有实际后台生成任务，拒绝视为完成')
        base.capture_artifacts(api, out, run, export_xlsx)
        while not task.done():
            await asyncio.wait({task}, timeout=poll_seconds)
            base.capture_artifacts(api, out, api.get_run(run['id']), export_xlsx)
        await task
    except BaseException as exc:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if run is None and created:
            run = api.get_run(created[-1])
        if run is None:
            base.save_json(out / 'failure.json', {'at': base.now(), 'status': 'failed', 'messages': 0,
                                                'error': _private_safe_error(exc), 'counts_as_new_content': False})
            return 1
        run = api.get_run(run['id'])
        run['status'] = 'failed'
        run.setdefault('errors', []).append(_private_safe_error(exc))
        api.save_run(run)
    finally:
        api.create_run, api.review_dialogue = original_create, original_review
    run = api.get_run(run['id'])
    run['program_review_history'] = copy.deepcopy(records)
    successful = (run.get('status') == 'completed' and len(run.get('messages', [])) == base.TARGET
                  and not run.get('errors') and _successful_review(run, records))
    if run.get('status') == 'completed' and not successful:
        run['status'] = 'failed'
        run.setdefault('errors', []).append('不能计为成功：需要实际完整150条及绑定本稿哈希的真实全文复核通过记录')
    run['version_record']['counts_as_new_content'] = successful
    api.save_run(run)
    base.save_json(out / 'review-calls.json', records)
    base.capture_artifacts(api, out, run, export_xlsx)
    summary = {'at': base.now(), 'series': SERIES, 'version': f'V{index:02d}', 'id': run['id'],
               'attempt_label': attempt_label,
               'status': run['status'], 'messages': len(run.get('messages', [])),
               'messages_sha256': messages_hash(run.get('messages', [])),
               'actual_full_review_passed': _successful_review(run, records), 'success': successful,
               'errors': run.get('errors', [])}
    base.save_json(out / 'generation-report.json', summary)
    # Re-capture so the manifest also covers the final report.
    base.capture_artifacts(api, out, run, export_xlsx)
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if successful else 1


async def execute(args):
    if (type(args.index) is not int or not 1 <= args.index <= 40 or not isinstance(args.seed, str)
            or not args.seed.strip() or not 1 <= len(args.seed) <= 80):
        raise ValueError('独立版本编号需1至40，seed需1至80字符')
    template, payload = read_template(args.template)
    corpus_path = Path(args.corpus_path).resolve()
    if not corpus_path.is_file():
        raise ValueError('公开对话语料库不存在，请先准备本地LCCC语料')
    corpus_bytes = corpus_path.read_bytes()
    attempt_label = getattr(args, 'attempt_label', None)
    if attempt_label is not None and base.revision_number(attempt_label) < 1:
        raise ValueError('独立新尝试标记必须为R001至R999')
    label = f'V{args.index:02d}' + ('-' + attempt_label if attempt_label else '')
    out = OUT_ROOT / label
    out.mkdir(parents=True, exist_ok=False)
    try:
        base.atomic(out / 'template.json', payload)
        code = archive_sources(out)
        base.configure_environment(out)
        os.environ['DIALOGUE_CORPUS_FILE'] = str(corpus_path)
        # These calls retain the chosen high-strength route even if the invoking
        # shell has unrelated product fallback or director overrides.
        os.environ.update({key: base.MODEL for key in ('MODEL_DIRECTOR_NAME', 'MODEL_ANALYSIS_NAME',
                           'MODEL_PERSONA_NAME', 'MODEL_REPAIR_NAME', 'MODEL_BATCH_REVIEW_NAME')})
        os.environ.pop('MODEL_FALLBACK_NAME', None)
        api = importlib.import_module('app.main')
        if api.DATA_DIR.resolve() != (out / '.runtime').resolve() or api.CORPUS_PATH.resolve() != corpus_path:
            raise ValueError('运行目录或语料目录未隔离，拒绝生成')
        from app.core import export_xlsx
        from app.dialogue_corpus import load_corpus
        corpus = load_corpus(corpus_path)
        corpus_evidence = {'path': str(corpus_path), 'file_sha256': base.sha(corpus_bytes),
                           'conversations': len(corpus['conversations']), 'sources': corpus['sources']}
        material_notes = None
        if getattr(args, 'material_notes', None) is not None:
            if base.sha(corpus_path.read_bytes()) != corpus_evidence['file_sha256']:
                raise ValueError('语料文件在选材校验前发生变化，拒绝错绑')
            material_notes = read_material_notes(args.material_notes, args.index, args.seed, corpus,
                                                 corpus_evidence['file_sha256'])
            base.save_json(out / 'material-notes.json', material_notes)
        inputs = {'series': SERIES, 'index': args.index, 'seed': args.seed,
                  'attempt_label': attempt_label,
                  'template_path': str(Path(args.template).resolve()), 'template_sha256': base.sha(payload),
                  'corpus': corpus_evidence, 'customer_count_override': args.customer_count,
                  'review_stream_policy': {'allow_done_without_stop_strict_json':
                                           getattr(args, 'allow_review_done_without_stop', False)}}
        if material_notes is not None:
            inputs['material_notes'] = material_provenance(material_notes)
        base.save_json(out / 'input.json', inputs)
        api.put_template(template['id'], template['filename'], template['source'], template['analysis'])
    except BaseException as exc:
        failure = {'at': base.now(), 'series': SERIES, 'version': f'V{args.index:02d}', 'status': 'failed',
                   'phase': 'setup', 'messages': 0, 'success': False, 'error': _private_safe_error(exc)}
        base.save_json(out / 'failure.json', failure)
        base.save_json(out / 'generation-report.json', failure)
        print(json.dumps(failure, ensure_ascii=True))
        return 1
    return await run_fresh(api, out, template, args.index, args.seed, code, base.sha(payload),
                           corpus_evidence, export_xlsx, args.customer_count, attempt_label=attempt_label,
                            material_notes=material_notes,
                            allow_review_done_without_stop=getattr(args, 'allow_review_done_without_stop', False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True, type=int, choices=range(1, 41))
    parser.add_argument('--seed', required=True)
    parser.add_argument('--corpus-path', required=True, type=Path)
    parser.add_argument('--template', type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument('--customer-count', type=int, choices=range(1, 21))
    parser.add_argument('--material-notes', type=Path, help='绑定本次seed与实际语料抽样的Agent选材注释JSON')
    parser.add_argument('--attempt-label', help='同一编号另建从零生成尝试，如V01-R001；不读旧稿，不另计独立编号')
    parser.add_argument('--allow-review-done-without-stop', action='store_true',
                        help='仅本次全文模型复核兼容已确认缺stop的网关；仍须DONE及严格完整JSON，默认关闭')
    args = parser.parse_args()
    with base.exclusive_lock(base.ROOT / '.vimax/deploy/independent-locks' / f'V{args.index:02d}.lock'):
        return asyncio.run(execute(args))


if __name__ == '__main__':
    raise SystemExit(main())
