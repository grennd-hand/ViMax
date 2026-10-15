"""Scoped ViMax editing; unchanged parent lines are retained in the full 150-line candidate.

python evolve-natural-patch.py --version 05 --parent .../V04/run.json \
    --issues V05-issues.json --editable-ranges '32-34,55-68'

The same dual-reader issues envelope, isolated storage, model settings, lock,
full program review and --resume-review rules as evolve-natural.py apply.
This helper never claims a complete rewrite: it records scoped_patch, exact
editable sequences, raw patch scenes, actual changed sequences and source files.
--review-mode deferred leaves status=paused awaiting actual dual-reader review;
it runs complete contracts and broadcast but skips the expensive model review.
--resume-review always performs the real full program review. V40 and final
recommendations require full review. Use full when business facts/actions change.
Exit 0 for a valid deferred draft means generation finished awaiting human review;
it never means the model approved the dialogue or the business goal was met.
--branch-from-reviewed-best explicitly allows an earlier genuinely dual-reviewed
parent (e.g. V06 to V07/V08/V09) for independent controlled alternatives. Without
it, the parent must be exactly Vn-1. Branches use separate per-version OS locks;
the caller limits concurrent branches to three. Parent files are read-only and
each version retains its own SQLite, source snapshot and real parent identifiers.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('natural_evolution_base', HERE / 'evolve-natural.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def parse_ranges(value):
    sequences = []
    if not value.strip():
        raise ValueError('修改窗口不能为空')
    for item in value.split(','):
        match = re.fullmatch(r'\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*', item)
        if not match:
            raise ValueError('修改窗口格式应为 32-34,55-68')
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= base.TARGET:
            raise ValueError('修改窗口必须位于1至150，且起点不大于终点')
        sequences.extend(range(first, last + 1))
    if len(sequences) != len(set(sequences)):
        raise ValueError('修改窗口不能重叠或重复')
    if len(sequences) > 40:
        raise ValueError('一次最多允许修改40条')
    return sorted(sequences)


def build_request(engine, template, editing, issues, allowed):
    request = engine.writing_contract(template, editing)
    context = engine.rolling_context(editing, editing['messages'])
    request += '\n父版本完整150条公开对话：' + json.dumps(context['public_history'], ensure_ascii=False)
    request += '\n仅供所属人物使用的私有处境、人格约束与未公开动机：' + json.dumps({
        'private_by_owner': context['private_by_owner'], 'threads': context['threads'],
        'turn_selection': context.get('turn_selection'),
    }, ensure_ascii=False)
    request += '\n双人全文阅读后的具体问题：' + json.dumps(issues, ensure_ascii=False)
    request += '\n本次允许修改的原编号：' + json.dumps(allowed)
    request += ('\n本次是 scoped_patch 局部编辑，不是全文重写。script数组只返回确实需修改的原编号行，'
                '每个编号最多一次，按编号递增；不得返回窗口外的行，不得重新编号、增删消息或交换消息顺序。'
                '允许只改窗口中部分行，不必为用满窗口改写有效内容；未返回的行会逐字保留。'
                '最终合并稿仍是完整150条。不要输出完整150条，不要省略号、说明、Markdown或占位行。'
                '窗口外事实和后续回应不改，因此修改必须与完整原文前后兼容；若需改后文且不在窗口，'
                '不要制造需要窗口外补救的新前因。角色只能依据该编号之前已公开的话回应，'
                '不能把后段信息、别人的私有动机或编剧所见母本当作人物已知。'
                '逐句核对指代、动作对象、比较参照和时间前因，保留正常已知信息的自然省略。'
                '保持母本原值、同人物的截图/提交因果、人格关注点与已作选择；不伪造付款或外部审批。'
                '没有必要修改的行不要返回；不能只调间隔、回复编号或近义替换以冒充解决问题。')
    request += '\n每条修改行格式：' + engine.output_format(editing)
    return request


def decode_patch(engine, scenes, editing, allowed):
    if not isinstance(scenes, list) or not all(isinstance(scene, str) for scene in scenes):
        raise ValueError('ViMax patch 必须是 script 字符串数组')
    replacements, last = {}, 0
    for scene in scenes:
        for raw in scene.splitlines():
            line = raw.strip()
            if not line:
                continue
            match = re.match(r'^(\d{3})\|', line)
            if not match:
                raise ValueError('patch 包含非编号台词行，拒绝忽略或猜测')
            seq = int(match[1])
            if seq not in allowed:
                raise ValueError(f'patch 编号 {seq} 超出允许窗口')
            if seq in replacements:
                raise ValueError(f'patch 编号 {seq} 重复')
            if seq <= last:
                raise ValueError('patch 编号必须严格递增，不能重排消息')
            replacements[seq] = engine.decode_scenes([line], editing, start=seq, expected_count=1)[0]
            last = seq
    if not replacements:
        raise ValueError('模型未返回有效修改行；仅保留尝试，不计新版本')
    return replacements


def apply_delay_policy(replacements, parent_messages, preserve_delay=False):
    changes = []
    if preserve_delay:
        for seq, replacement in replacements.items():
            expected = parent_messages[seq - 1].get('delay')
            if replacement.get('delay') != expected:
                changes.append({'seq': seq, 'field': 'delay', 'raw': replacement.get('delay'), 'retained_parent': expected})
            replacement['delay'] = expected
    return changes


async def patch_once(api, engine, template, run, parent_messages, issues, chat, allowed, preserve_delay=False):
    if len(parent_messages) != base.TARGET or [m.get('seq') for m in parent_messages] != list(range(1, 151)):
        raise ValueError('scoped_patch 只接受编号1至150的完整父稿')
    run['version_record'].update(method='scoped_patch', editable_sequences=list(allowed),
                                 delay_policy='parent' if preserve_delay else 'model',
                                 patch_policy='one ViMax scoped edit, immutable outside lines, one full program review')
    run['scoped_patch'] = {'editable_sequences': list(allowed), 'changed_sequences': [],
                            'parent_content_sha256': base.content_fingerprint({'messages': parent_messages})}
    api.save_run(run)
    editing = copy.deepcopy(run)
    editing['messages'] = copy.deepcopy(parent_messages)
    merged = None
    try:
        writer = engine.screenwriter_class()(chat_model=engine.RecordedChat(chat, api, run['id'], 'scoped_patch'))
        method = writer.write_script_based_on_story
        request = build_request(engine, template, editing, issues, allowed)
        if preserve_delay:
            request += '\n本次显式使用parent-delay策略：最终稿确定性沿用每个原编号的父稿delay，原始模型delay仍归档；不要用间隔变化代替正文收益。'
        # Upstream Screenwriter has a three-attempt tenacity decorator. Bypass
        # only that wrapper so one failed parse/network call stays one attempt.
        unwrapped = getattr(method, '__wrapped__', None)
        if unwrapped is not None:
            scenes = await unwrapped(writer, run.get('vimax_story', ''), request, system_prompt=engine.WRITE_PROMPT)
        else:
            scenes = await method(run.get('vimax_story', ''), request, system_prompt=engine.WRITE_PROMPT)
        latest = api.get_run(run['id'])
        latest['scoped_patch']['raw_patch_scenes'] = copy.deepcopy(scenes)
        api.save_run(latest)
        replacements = decode_patch(engine, scenes, editing, set(allowed))
        retained_delays = apply_delay_policy(replacements, parent_messages, preserve_delay)
        merged = copy.deepcopy(parent_messages)
        for seq, replacement in replacements.items():
            merged[seq - 1] = replacement
        latest = api.get_run(run['id'])
        changed = [seq for seq in replacements if base.dialogue_fingerprint({'messages': [parent_messages[seq - 1]]})
                   != base.dialogue_fingerprint({'messages': [replacements[seq]]})]
        latest['scoped_patch'].update(returned_sequences=list(replacements), changed_sequences=changed,
                                     delay_policy='parent' if preserve_delay else 'model',
                                     retained_parent_delays=retained_delays,
                                     content_changed_sequences=[seq for seq in replacements
                                         if base.content_fingerprint({'messages': [parent_messages[seq - 1]]})
                                         != base.content_fingerprint({'messages': [replacements[seq]]})])
        # create_child builds a fresh schedule; retained parent lines may have
        # different actual opening boundaries. As in vimax_engine.revise, align
        # the schedule with the merged draft before validating the full contract.
        # contract_errors still checks stage progression and opening independently.
        latest['schedule'] = [{**slot, 'stage': merged[i]['stage']} for i, slot in enumerate(latest['schedule'])]
        api.save_run(latest)
        # Capture the actual merged full draft before structural rejection. The
        # existing archive retains raw attempted wording and media in JSON/XLSX/MD.
        engine.checkpoint_candidate(api, latest, merged, 'scoped_patch 完整150条合并候选，尚未合同检查')
        if base.content_fingerprint({'messages': merged}) == base.content_fingerprint({'messages': parent_messages}):
            raise base.NoChangeError('局部响应正文、发言人与媒体未变；仅保留尝试，不计新完整版本')
        errors = engine.contract_errors(template['source'], {**latest, 'messages': merged, 'status': 'completed'})
        if errors:
            raise ValueError('局部修订合并稿仍未通过：' + '；'.join(errors))
        run = api.get_run(run['id'])
        run['messages'] = merged
        run['candidate_origin'] = 'scoped_patch merged with exact unchanged parent lines'
        run['review_revisions'] = [{'seq': seq, 'before': copy.deepcopy(parent_messages[seq - 1]),
                                    'after': copy.deepcopy(replacements[seq])} for seq in changed]
        api.save_run(run)
        await base.broadcast(api, engine, template, run)
        return run
    except BaseException as exc:
        latest = api.get_run(run['id'])
        records = latest.get('candidate_versions', [])
        if records and merged is not None and not isinstance(exc, base.NoChangeError):
            captured = json.loads((api.DATA_DIR / records[-1]['path'] / 'run.json').read_bytes())
            latest['messages'] = captured['messages']
            latest['cursor'] = len(latest['messages'])
            latest['candidate_origin'] = 'actual full scoped_patch merged candidate retained after failure'
        else:
            latest['messages'] = []
            latest['cursor'] = 0
            if isinstance(exc, base.NoChangeError):
                latest['unchanged_parent_candidate'] = True
        api.save_run(latest)
        raise


async def deferred_review(api, engine, template, run):
    if len(run.get('messages', [])) != base.TARGET:
        raise ValueError('延后程序审阅也需要实际完整150条候选')
    run['errors'] = engine.contract_errors(template['source'], {**run, 'status': 'completed'})
    run['program_review_deferred'] = True
    run['version_record']['review_mode'] = 'deferred'
    run['status'] = 'failed' if run['errors'] else 'paused'
    run['review_progress']['phase'] = 'failed' if run['errors'] else 'awaiting_human_review'
    run['naturalness_report'] = engine.naturalness_evidence(run['messages'])
    api.save_run(run)


def install_hooks(out, allowed, review_mode='full', resume_review=False, branch_from_reviewed_best=False, preserve_delay=False):
    original_provenance = base.provenance
    original_review = base.review_only
    original_read_inputs = base.read_inputs
    branch_mode = 'reviewed_best' if branch_from_reviewed_best else 'sequential'

    def provenance():
        result = original_provenance()
        sources = out / 'sources'
        for path in (HERE / 'evolve-natural.py', Path(__file__).resolve()):
            payload = path.read_bytes()
            result['source_hashes'][path.relative_to(base.ROOT).as_posix()] = base.sha(payload)
            destination = sources / path.name
            if destination.exists() and destination.read_bytes() != payload:
                # A resumed program review records current provenance but never
                # overwrites the helper sources that created the original draft.
                destination = sources / f'{path.stem}-{base.sha(payload)[:12]}.py'
            if not destination.exists():
                base.atomic(destination, payload)
        result.update(method='scoped_patch', editable_sequences=list(allowed),
                      delay_policy='parent' if preserve_delay else 'model',
                      review_mode='full' if resume_review else review_mode,
                      branch_mode=branch_mode, target_version=out.name)
        return result

    async def revision_once(api, engine, template, run, parent_messages, issues, chat):
        run['version_record']['review_mode'] = review_mode
        run['version_record'].update(branch_mode=branch_mode, target_version=out.name)
        return await patch_once(api, engine, template, run, parent_messages, issues, chat, allowed, preserve_delay)

    async def review_only(api, engine, template, run):
        if review_mode == 'deferred' and not resume_review:
            return await deferred_review(api, engine, template, run)
        run['version_record']['review_mode'] = 'full'
        if run.get('program_review_deferred'):
            run.setdefault('deferred_review_history', []).append({'at': base.now(), 'resumed': True})
            run['program_review_deferred'] = False
        return await original_review(api, engine, template, run)

    base.provenance = provenance
    base.revision_once = revision_once
    base.review_only = review_only
    if branch_from_reviewed_best:
        def read_inputs(parent_path, issues_path, version, revision_label=None):
            return original_read_inputs(parent_path, issues_path, version,
                                        allow_reviewed_best=True, revision_label=revision_label)
        base.read_inputs = read_inputs


def execution_lock_path(version, branch_from_reviewed_best=False):
    if branch_from_reviewed_best:
        return base.ROOT / '.vimax/deploy/evolution-locks' / f'V{version:02d}.lock'
    return base.ROOT / '.vimax/deploy/evolve-natural.lock'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, type=int, choices=range(4, 41))
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--issues', required=True, type=Path)
    parser.add_argument('--editable-ranges', required=True)
    parser.add_argument('--review-mode', choices=('full', 'deferred'), default='full')
    parser.add_argument('--resume-review', action='store_true')
    parser.add_argument('--revision-label', help='另建修订目录如V40-R001；同family父稿必须较早revision，原稿不覆盖')
    parser.add_argument('--preserve-delay', action='store_true', help='只改正文时，确定性沿用父稿各编号delay；原始模型值和保留策略单独归档')
    parser.add_argument('--branch-from-reviewed-best', action='store_true',
                        help='从编号更早且已双人全文审阅的真实最佳基线创建独立备选；每个V独立锁')
    args = parser.parse_args()
    base.revision_number(args.revision_label)
    allowed = parse_ranges(args.editable_ranges)
    if args.version == 40 and args.review_mode != 'full' and not args.resume_review:
        raise ValueError('V40 必须真实执行 full 全量程序复核')
    label = f'V{args.version:02d}' + ('-' + args.revision_label if args.revision_label else '')
    out = base.ROOT / 'outputs/自然群聊-20261015' / label
    lock = execution_lock_path(args.version, args.branch_from_reviewed_best)
    if args.revision_label:
        lock = lock.with_name(label + '.lock')
    with base.exclusive_lock(lock):
        if args.resume_review:
            stored = json.loads((out / 'run.json').read_bytes())
            args.preserve_delay = stored.get('version_record', {}).get('delay_policy') == 'parent'
            if (stored.get('version_record', {}).get('method') != 'scoped_patch'
                    or stored['version_record'].get('editable_sequences') != allowed):
                raise ValueError('只能恢复同一个 scoped_patch 版本及原修改窗口的程序复核')
        install_hooks(out, allowed, args.review_mode, args.resume_review, args.branch_from_reviewed_best, args.preserve_delay)
        result = asyncio.run(base.execute(args))
        if args.review_mode == 'deferred' and not args.resume_review and result == 1:
            run = json.loads((out / 'run.json').read_bytes())
            if (run.get('status') == 'paused' and not run.get('errors')
                    and len(run.get('messages', [])) == base.TARGET
                    and run.get('program_review_deferred') is True):
                return 0
        return result


if __name__ == '__main__':
    raise SystemExit(main())
