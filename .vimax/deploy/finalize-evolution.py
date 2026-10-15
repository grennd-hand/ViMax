"""Verify and summarize the preserved V01-V40 content families, without rewriting originals."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/自然群聊-20261015'
FIELDS = ('sender', 'content', 'type', 'image_code', 'image_mode')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selected', required=True)
    args = parser.parse_args()
    versions = json.loads((OUT / 'versions.json').read_text(encoding='utf-8'))
    by_label = {v['version']: v for v in versions}
    corrections = json.loads((OUT / 'evolution-corrections.json').read_text(encoding='utf-8'))
    replacements = {n['version']: n['representative_version'] for n in corrections['notes']
                    if n.get('representative_version') and sha(OUT / n['version'] / 'run.json') == n['run_sha256']}
    evidence = []
    fingerprints = set()
    for number in range(1, 41):
        family = f'V{number:02d}'
        label = replacements.get(family, family)
        entry = by_label[label]
        folder = OUT / label
        run = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
        assert len(run['messages']) == 150 and [m['seq'] for m in run['messages']] == list(range(1, 151))
        body = json.dumps([{k: m.get(k) for k in FIELDS} for m in run['messages']],
                          ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        fingerprint = hashlib.sha256(body).hexdigest()
        assert fingerprint not in fingerprints, f'Duplicate representative body: {label}'
        fingerprints.add(fingerprint)
        if number >= 4:
            assert sha(folder / 'run.json') == entry['run_sha256']
            checks = json.loads((folder / 'hashes.json').read_text(encoding='utf-8'))
            for name, digest in checks.items():
                assert sha(folder / name) == digest, f'Changed archive: {label}/{name}'
        evidence.append({'family': family, 'representative': label, 'run_id': run['id'],
                         'body_sha256': fingerprint, 'run_sha256': sha(folder / 'run.json'),
                         'message_count': 150, 'status': run['status'],
                         'program_review_deferred': bool(run.get('program_review_deferred'))})
    selected = by_label[args.selected]
    run = json.loads((OUT / args.selected / 'run.json').read_text(encoding='utf-8'))
    assert selected['human_verdict'] == 'candidate' and run['status'] == 'completed'
    assert not run['errors'] and not run['quality_review'] and not run.get('program_review_deferred')
    last = run['program_review_history'][-1]['review']
    assert last['goal_met'] is True and not last['issues']
    archived_bodies = set()
    for item in versions:
        if int(item['version'][1:3]) < 4:
            continue
        original = json.loads((OUT / item['version'] / 'run.json').read_text(encoding='utf-8'))
        archived_bodies.add(hashlib.sha256(json.dumps([{k:m.get(k) for k in FIELDS} for m in original['messages']],
                            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    report = {'last_family': 'V40', 'families': 40, 'distinct_representative_bodies': len(fingerprints),
              'new_representative_bodies': 37, 'distinct_archived_new_bodies': len(archived_bodies),
              'selected': args.selected, 'selected_run_id': run['id'],
              'review_policy': 'Intermediate human coverage and actual model review are recorded separately.',
              'evidence': evidence}
    (OUT / '最终版本核验.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = [f'# 版本选择与恢复\n\n本轮已继续至V40。推荐比较：**{args.selected}**，150条，GPT-6 Sol / high。'
             '推荐稿已执行真实全文模型复核，主Agent与独立审阅者另从头读完150条；这是本次验收结果，不保证所有文风偏好都一致。\n\n'
             'V01–V40每个编号都有150条完整原文，40个正式代表正文不同。V32原稿与V29正文相同，只改4处间隔，原稿照常保留为时间配置备选；'
             'V32-R001另为32号正式内容代表。空响应和失败的零条尝试不计完整稿。\n\n'
             'V40原稿的最终模型复核发现077“手指够不到”与082“伸手碰了碰”矛盾，主审和两位独立审阅者初读漏查；'
             '原稿、failed状态和初读报告全部保留。V40-R001先区分无法夹取与无法触及，完整复核又发现床底前因与纸卡尺寸依据问题；'
             f'后续修订继续保留失败原文，最终40号正式代表为{args.selected}。\n\n'
             '本轮以ViMax局部修订合成完整150条，未把它表述为每版从零重写。各版本真实父稿、修改范围、原始响应、源码和哈希均有归档。'
             '部分后续稿是表达或结构比较，不是版本越高越好。\n\n'
             '中间稿主审采用已读父稿逐条核对150对象、阅读所有变化及上下文的全量覆盖；独立subagent各版从头全文阅读。'
             '延后的模型全文审查标为待审，failed原状态和错误保留，均不伪装模型通过。\n\n'
             '| 标记 | 父稿 | 人工判断 / 模型状态 | 说明 |\n| --- | --- | --- | --- |']
    names = {'candidate': '候选', 'alternative': '备选', 'needs_revision': '需调整'}
    ordered = sorted((v for v in versions if v['version'] != 'V00'), key=lambda v: (int(v['version'][1:3]), v['version']))
    for item in ordered:
        model = '模型待审' if item.get('program_review_deferred') else {'completed': '程序已完成', 'failed': '程序未通过', 'paused': '暂停'}.get(item.get('status'), '见历史记录')
        summary = str(item.get('review') or '').replace('|', '／').replace('\n', ' ')
        lines.append(f"| {item['version']} | {item.get('parent', '历史稿')} | {names.get(item.get('human_verdict'), '见历史报告')} / {model} | {summary} |")
    lines.append('\n左侧选择编号即可回看该份原文；历史内容只读，后续修改另建新稿。需要恢复时给出完整编号，'
                 '按对应run与快照恢复内容，保留所有其他历史；生成器代码按已记录标签恢复，不以旧数据库覆盖现有记录。'
                 '重新生成具有随机性，不能代替恢复原文。\n\n'
                 '每份目录包含群聊150条.md、Excel、原始JSON、快照、源码来源和审阅报告。H01–H08及V00基线继续保留，见历史版本对照.md。'
                 '两张图片仍为待补素材占位，没有执行真实外部任务、提交或付款。\n')
    (OUT / '版本选择说明.md').write_text('\n'.join(lines), encoding='utf-8')
    plan_path = OUT / 'evolution-plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan.update(status='complete', final_acceptance='passed', selected=args.selected,
                completed_new_representatives=37, distinct_archived_new_bodies=len(archived_bodies),
                current_version=args.selected, current_versions=[args.selected])
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    with zipfile.ZipFile(OUT / 'V01-V40版本导出.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in ordered:
            folder = OUT / item['version']
            for path in folder.iterdir():
                if path.is_file() and (path.suffix in {'.xlsx', '.md'} or path.name == 'review-index.json'):
                    archive.write(path, path.relative_to(OUT).as_posix())
        for name in ('versions.json', '最终版本核验.json', '版本选择说明.md', 'evolution-corrections.json'):
            archive.write(OUT / name, name)
    print(json.dumps({k: report[k] for k in ('families', 'distinct_representative_bodies', 'new_representative_bodies', 'distinct_archived_new_bodies', 'selected')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
