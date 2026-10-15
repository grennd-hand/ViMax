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
                    export_xlsx, customer_count=None, poll_seconds=15, attempt_label=None):
    """Await the actual start_simulation task; no cancel-and-revise shortcut."""
    count = choose_customer_count(api, template['source'], seed, customer_count)
    config = api.SimulationConfig(template_id=template['id'], customer_count=count, target_messages=base.TARGET,
                                  allow_early_end=False, creation_mode='independent_random', variation_seed=seed,
                                  randomize_personas=True, dialogue_model=base.MODEL, reasoning_effort=base.EFFORT,
                                  story_world='', character_ids={}, personas={}, persona_cards={},
                                  persona_sources={}, persona_versions={}, story_notes='')
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
        run['version_record'] = {
            'version': f'V{index:02d}', 'series': SERIES, 'series_date': '20261015',
            'method': 'independent_generation', 'parent_run': None, 'parent_sha': None, 'parent_sha256': None,
            'seed': seed, 'customer_count': count, 'model': base.MODEL, 'reasoning_effort': base.EFFORT,
            'template_sha256': template_sha, 'code': code['commit'], 'provenance': code,
            'corpus': corpus_evidence, 'review_mode': 'full', 'counts_as_new_content': False,
        }
        if attempt_label:
            run['version_record']['revision'] = attempt_label
        run['version_record']['seed_scope'] = 'random_configuration_only_not_model_output_reproduction'
        base.isolated_create(api, run)
        created.append(run['id'])

    async def tracked_review(template_argument, run):
        record = {'at': base.now(), 'messages_sha256': messages_hash(run.get('messages', [])),
                  'message_count': len(run.get('messages', [])), 'model': base.MODEL,
                  'reasoning_effort': base.EFFORT, 'state': 'started', 'evidence': 'api.review_dialogue invocation'}
        records.append(record)
        _persist_reviews(api, out, run, records)
        try:
            result = await original_review(template_argument, run)
            record.update(state='returned', finished_at=base.now(), review=copy.deepcopy(result))
            return result
        except BaseException as exc:
            record.update(state='failed', finished_at=base.now(), error=_private_safe_error(exc))
            raise
        finally:
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
        base.save_json(out / 'input.json', {'series': SERIES, 'index': args.index, 'seed': args.seed,
                                          'attempt_label': attempt_label,
                                          'template_path': str(Path(args.template).resolve()), 'template_sha256': base.sha(payload),
                                          'corpus': corpus_evidence, 'customer_count_override': args.customer_count})
        api.put_template(template['id'], template['filename'], template['source'], template['analysis'])
    except BaseException as exc:
        failure = {'at': base.now(), 'series': SERIES, 'version': f'V{args.index:02d}', 'status': 'failed',
                   'phase': 'setup', 'messages': 0, 'success': False, 'error': _private_safe_error(exc)}
        base.save_json(out / 'failure.json', failure)
        base.save_json(out / 'generation-report.json', failure)
        print(json.dumps(failure, ensure_ascii=True))
        return 1
    return await run_fresh(api, out, template, args.index, args.seed, code, base.sha(payload),
                           corpus_evidence, export_xlsx, args.customer_count, attempt_label=attempt_label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True, type=int, choices=range(1, 41))
    parser.add_argument('--seed', required=True)
    parser.add_argument('--corpus-path', required=True, type=Path)
    parser.add_argument('--template', type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument('--customer-count', type=int, choices=range(1, 21))
    parser.add_argument('--attempt-label', help='同一编号另建从零生成尝试，如V01-R001；不读旧稿，不另计独立编号')
    args = parser.parse_args()
    with base.exclusive_lock(base.ROOT / '.vimax/deploy/independent-locks' / f'V{args.index:02d}.lock'):
        return asyncio.run(execute(args))


if __name__ == '__main__':
    raise SystemExit(main())
