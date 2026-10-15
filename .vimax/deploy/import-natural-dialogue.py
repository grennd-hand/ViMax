"""Idempotently import reviewed runs. Called only with an offline target database."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import uuid

MESSAGE_FIELDS = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type',
                  'image_code', 'image_mode', 'replace_date')
SNAPSHOT_FILES = ('run.json', 'messages.md', 'messages.xlsx', 'manifest.json')
INDEPENDENT_SERIES = '独立随机-20261015'


def is_independent(run):
    record = run.get('version_record') or {}
    return (record.get('method') == 'independent_generation'
            or record.get('series') == INDEPENDENT_SERIES
            or (run.get('independence') or {}).get('method') == 'independent_generation')


def independent_review_state(run, fingerprint=None):
    """Only the last actual returned call can certify this exact full draft."""
    if run.get('program_review_deferred') is True:
        return 'deferred'
    messages = run.get('messages') or []
    if fingerprint is None:
        fingerprint = hashlib.sha256(canonical([{key: message.get(key) for key in MESSAGE_FIELDS}
                                                for message in messages]).encode()).hexdigest()
    history = run.get('program_review_history') or []
    last = history[-1] if history else {}
    if (isinstance(last, dict) and last.get('state') == 'returned'
            and last.get('messages_sha256') == fingerprint
            and last.get('message_count') == len(messages) == 150
            and isinstance(last.get('review'), dict)):
        return 'reviewed'
    return 'not_completed'


def independent_evidence(run, version):
    """Check preserved fresh-run evidence, not a claim of semantic uniqueness."""
    family, revision = version_parts(version)
    assert 1 <= family <= 40, 'Independent series expects family V01..V40'
    record, plan = run.get('version_record', {}), run.get('creative_variation') or {}
    proof, reference = run.get('independence') or {}, run.get('corpus_reference') or {}
    assert record.get('method') == 'independent_generation' and record.get('series') == INDEPENDENT_SERIES
    for item in (record, plan, proof):
        assert item.get('parent_run', 'missing') is None, 'Independent draft cannot inherit a parent run'
        assert item.get('parent_version') in (None, ''), 'Independent draft cannot inherit a parent version'
    assert plan.get('method') == proof.get('method') == 'independent_generation'
    assert proof.get('initial_messages') == 0 and proof.get('initial_story', 'missing') is None
    assert proof.get('fresh_story_world') == run['config'].get('story_world') and proof.get('fresh_story_world')
    assert isinstance(plan.get('style_label'), str) and plan['style_label'].strip(), 'Independent style label required'
    assert plan.get('persona_cards') == run['config'].get('persona_cards') and plan.get('persona_cards'), 'Independent cast differs from saved plan'
    assert isinstance(reference.get('samples'), list) and reference['samples'], 'Actual public corpus samples required'
    assert isinstance(reference.get('sources'), list) and reference['sources'], 'Public corpus source provenance required'
    assert all(source.get('url') and source.get('license') for source in reference['sources'])
    return {'series': INDEPENDENT_SERIES, 'method': 'independent_generation', 'parent_run': None,
            'display_label': f'独立{family:02d}' + ('-' + revision if revision else ''), 'style_label': plan['style_label']}


def version_parts(label):
    match = re.fullmatch(r'V([0-9]{2,})(?:-(R[0-9]{3,}))?', label)
    assert match, 'Expected version label VNN or VNN-RNNN'
    return int(match[1]), match[2]


def correction_metadata(source_directory, version, source_sha256):
    path = source_directory.parent / 'evolution-corrections.json'
    if not path.exists():
        return {}
    corrections = json.loads(path.read_text(encoding='utf-8'))
    assert corrections.get('schema_version') == 1 and isinstance(corrections.get('notes'), list), 'Invalid corrections schema'
    matched = [entry for entry in corrections['notes'] if entry.get('version') == version
               and entry.get('run_sha256') == source_sha256]
    assert len(matched) <= 1, 'Ambiguous correction for this exact original'
    if not matched:
        return {}
    note = matched[0]
    assert isinstance(note.get('display_note'), str) and note['display_note'].strip(), 'Correction display note required'
    result = {'display_note': note['display_note']}
    if note.get('representative_version'):
        assert version_parts(note['representative_version'])[0] == version_parts(version)[0], 'Correction representative must belong to the same family'
        result['representative_version'] = note['representative_version']
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def immutable_run(run):
    return {key: run.get(key) for key in (
        'id', 'template_id', 'vimax_project_id', 'messages', 'status', 'config',
        'errors', 'quality_review', 'candidate_versions', 'publication')}


def publication_metadata(run, source_directory, version, selected, source_sha256, contract_errors):
    """Evolution histories are immutable originals, with separate review facts."""
    record = run.get('version_record', {})
    original_label = record.get('version', '')
    _, embedded_revision = version_parts(original_label)
    assert embedded_revision is None, 'Original family and revision must be separate fields'
    if record.get('revision'):
        original_label += '-' + record['revision']
    version_parts(original_label)
    assert original_label == version, 'Version label differs from original run'
    wording = [{key: message.get(key) for key in MESSAGE_FIELDS} for message in run['messages']]
    fingerprint = hashlib.sha256(canonical(wording).encode()).hexdigest()
    source_directory = source_directory.resolve()
    review_path = source_directory / 'review-index.json'
    review = json.loads(review_path.read_text(encoding='utf-8'))
    hashes = json.loads((source_directory / 'hashes.json').read_text(encoding='utf-8'))
    assert review.get('version') == version and review.get('message_sha256') == fingerprint, 'Human review belongs to another draft'
    assert review.get('verdict') in {'candidate', 'alternative', 'needs_revision'}
    assert str(review.get('summary') or '').strip(), 'Human review summary required'
    assert hashes.get('run.json') == source_sha256, 'Original run checksum mismatch'
    assert hashes.get('review-index.json') == hashlib.sha256(review_path.read_bytes()).hexdigest(), 'Review index checksum mismatch'
    reports = review.get('reports', [])
    assert len(reports) == 2, 'Two complete human review reports required'
    expected = {'R000-primary-review.md', 'R000-subagent-review.md'}
    seen = set()
    report_hashes = {}
    for relative in reports:
        assert isinstance(relative, str) and '\\' not in relative, 'Unsafe human review path'
        path = (source_directory.parent / relative).resolve()
        assert path.is_relative_to(source_directory / 'reviews') and path.name in expected, 'Unsafe human review path'
        assert path.name not in seen, 'Duplicate human reviewer report'
        seen.add(path.name)
        payload = path.read_bytes()
        assert fingerprint in payload.decode('utf-8').lower(), 'Human report is not bound to this full draft'
        digest = hashlib.sha256(payload).hexdigest()
        assert hashes.get(path.relative_to(source_directory).as_posix()) == digest, 'Human report checksum mismatch'
        report_hashes[relative] = digest
    assert seen == expected
    deferred = run.get('program_review_deferred') is True
    assert review.get('program_review_deferred') is deferred, 'Deferred review marker differs from original run'
    history = run.get('program_review_history') or []
    checks = contract_errors
    if run['status'] == 'paused':
        assert deferred and run.get('review_progress', {}).get('phase') == 'awaiting_human_review', 'Paused run is not a deferred complete candidate'
        assert not run.get('errors') and not checks, 'Deferred candidate failed actual full-draft contracts'
    if selected:
        assert review['verdict'] == 'candidate' and not deferred and history, 'Selected evolution needs human acceptance and an actual full model review'
        if is_independent(run):
            assert independent_review_state(run, fingerprint) == 'reviewed', 'Selected independent review is not returned and bound to this full draft'
        last_review = history[-1].get('review', {})
        assert last_review.get('goal_met') is True and not last_review.get('issues'), 'Selected model review did not pass'
        if is_independent(run):
            assert last_review.get('issues') == [], 'Selected independent review must return an explicit empty issues list'
    metadata = {'read_only': True, 'version': version, 'selected': selected,
            'source_run_sha256': source_sha256, 'message_sha256': fingerprint,
            'human_verdict': review['verdict'], 'human_summary': review['summary'],
            'human_report_hashes': report_hashes,
            'program_review_state': 'deferred' if deferred else 'reviewed' if history else 'not_completed',
            'contract_errors': list(checks)}
    metadata.update(correction_metadata(source_directory, version, source_sha256))
    if is_independent(run):
        metadata.update(independent_evidence(run, version))
        metadata['program_review_state'] = independent_review_state(run, fingerprint)
        metadata['review_label'] = {'candidate': '完整双审候选', 'alternative': '表达备选',
                                    'needs_revision': '需修订'}[review['verdict']]
        if run['status'] == 'failed': metadata['review_label'] = '需修订'
        metadata['user_selected'] = bool(selected)
    return metadata


def prepare_snapshots(run, source_directory, data_directory):
    """Read and validate every immutable snapshot before any database write."""
    records = run.get('candidate_versions', [])
    assert isinstance(records, list)
    prepared = []
    seen = set()
    run_id = run['id']
    assert re.fullmatch('[0-9a-f]{32}', run_id), 'Unsafe run ID'
    source_root = (source_directory / 'snapshots').resolve()
    target_root = data_directory.resolve()
    for record in records:
        fingerprint = record['hash']
        assert re.fullmatch('[0-9a-f]{64}', fingerprint)
        relative = record['path']
        pattern = r'versions/' + run_id + r'/R[0-9]{3,}-' + fingerprint[:8]
        assert re.fullmatch(pattern, relative), 'Unsafe snapshot path'
        assert relative not in seen, 'Duplicate snapshot path'
        seen.add(relative)
        folder_name = PurePosixPath(relative).name
        source = (source_root / folder_name).resolve()
        destination = target_root.joinpath(*PurePosixPath(relative).parts).resolve()
        assert source.is_relative_to(source_root) and destination.is_relative_to(target_root), 'Snapshot path escapes root'
        assert set(path.name for path in source.iterdir()) == set(SNAPSHOT_FILES), 'Incomplete/extra snapshot files'
        payloads = {}
        for name in SNAPSHOT_FILES:
            path = (source / name).resolve()
            assert path.parent == source and path.is_file(), 'Snapshot symlink/path escape'
            payloads[name] = path.read_bytes()
        manifest = json.loads(payloads['manifest.json'])
        assert manifest['record'] == record and manifest['hash_fields'] == list(MESSAGE_FIELDS)
        assert set(manifest['files']) == set(SNAPSHOT_FILES) - {'manifest.json'}
        for name, expected in manifest['files'].items():
            assert hashlib.sha256(payloads[name]).hexdigest() == expected, 'Snapshot checksum mismatch: ' + name
        snapshot = json.loads(payloads['run.json'])
        assert snapshot['id'] == run_id
        messages = snapshot['messages']
        assert len(messages) >= run['config']['target_messages']
        assert manifest['message_count'] == len(messages)
        wording = [{key: message.get(key) for key in MESSAGE_FIELDS} for message in messages]
        assert hashlib.sha256(canonical(wording).encode()).hexdigest() == fingerprint, 'Snapshot wording hash mismatch'
        if destination.exists():
            assert set(path.name for path in destination.iterdir()) == set(SNAPSHOT_FILES), 'Existing snapshot files differ'
            for name, payload in payloads.items():
                existing = (destination / name).resolve()
                assert existing.parent == destination and existing.read_bytes() == payload, 'Immutable snapshot conflict: ' + relative
        prepared.append((destination, payloads))
    return prepared


def install_snapshots(prepared):
    for destination, payloads in prepared:
        if destination.exists():
            # Recheck after preparation; never overwrite an existing archive.
            assert set(path.name for path in destination.iterdir()) == set(SNAPSHOT_FILES)
            assert all((destination / name).read_bytes() == payload for name, payload in payloads.items()), 'Immutable snapshot conflict'
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name('.' + destination.name + '.import-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for name, payload in payloads.items():
                atomic_write(staging / name, payload)
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def atomic_write(path, payload):
    temporary = path.with_name(path.name + '.import-tmp')
    with temporary.open('wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--selected', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    sys.path[:0] = [str(root), str(root / 'dialogue/backend')]
    from app.core import export_xlsx
    from app.vimax_engine import contract_errors
    from app.memory_store import bind_characters, sync_accepted_memory
    from agent_runtime.session_index import SessionIndex

    source_bytes = Path(args.run).read_bytes()
    run = json.loads(source_bytes)
    template = json.loads(Path(args.template).read_text(encoding='utf-8'))
    family_number, _ = version_parts(args.version)
    if args.selected:
        assert run['status'] == 'completed' and not run['errors'] and not run['quality_review']
    else:
        assert run['status'] in {'completed', 'failed', 'paused'}
    assert len(run['messages']) == run['config']['target_messages'] == 150
    assert run['config']['dialogue_flow_version'] in {3, 4}
    assert run['config']['dialogue_model'] == 'gpt-6-sol'
    assert run['config']['reasoning_effort'] == 'high'
    independent = is_independent(run)
    evolution = family_number >= 4 or independent
    if independent:
        independent_evidence(run, args.version)
        assert run.get('candidate_versions'), 'Independent complete draft requires archived snapshots'
    if evolution or args.selected:
        checks = contract_errors(template['source'], {**run, 'status': 'completed'})
        if args.selected:
            assert not checks
    if evolution:
        assert [m.get('seq') for m in run['messages']] == list(range(1, 151)), 'Full original must contain sequences 1..150'
        run['publication'] = publication_metadata(run, Path(args.run).parent, args.version, args.selected,
                                                 hashlib.sha256(source_bytes).hexdigest(), checks)
    else:
        assert run['status'] in {'completed', 'failed'}, 'Legacy paused histories require explicit reviewed-candidate evidence'
    snapshots = prepare_snapshots(run, Path(args.run).resolve().parent, Path(args.db).resolve().parent)
    project_id = run.setdefault('vimax_project_id', 'dialogue-' + run['id'])
    assert project_id == 'dialogue-' + run['id']
    assert project_id and all(c.isalnum() or c in '-_' for c in project_id)
    # Export generation finishes before any database/index write.
    artifacts = {
        'messages.json': json.dumps(run['messages'], ensure_ascii=False, indent=2).encode(),
        'messages.md': '\n\n'.join(f"{m['seq']} **{m['sender']}**：{m['content']}" for m in run['messages']).encode(),
        'messages.xlsx': export_xlsx(run),
    }
    if run['config'].get('usage_context') == 'fictional_rehearsal':
        artifacts['messages.md'] = ('# 虚构示例／内部排练稿\n\n人物及行为为虚构情节，不代表真实参与记录。\n\n'.encode('utf-8')
                                   + artifacts['messages.md'])
    assert (root / '.vimax/sessions.json').is_file(), 'Existing session index required'
    assert Path(args.db).is_file(), 'Existing database required'
    index = SessionIndex(root)
    connection = sqlite3.connect(args.db, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN IMMEDIATE')
        old = {row['id']: row['run_json'] for row in connection.execute('SELECT id,run_json FROM sessions')}
        existed = run['id'] in old
        if existed:
            assert canonical(immutable_run(json.loads(old[run['id']]))) == canonical(immutable_run(run)), 'Run ID exists with different content'
        stored = connection.execute('SELECT source_json,analysis_json FROM templates WHERE id=?', (run['template_id'],)).fetchone()
        assert stored and json.loads(stored['source_json']) == template['source']
        assert json.loads(stored['analysis_json']) == template['analysis']
        # SQLite commits first. An identical retry repairs later filesystem failures.
        with index._locked():
            data = index.load()
            sessions = data.setdefault('sessions', {})
            record = sessions.get(project_id)
            assert record is None or existed, 'Index ID belongs to a different/orphan record'
            if record is not None:
                assert record['session_id'] == project_id
                assert record['working_dir'] == f'.working_dir/{project_id}'
            install_snapshots(snapshots)
            folder = root / '.working_dir' / project_id / 'dialogue'
            folder.mkdir(parents=True, exist_ok=True)
            for name, payload in artifacts.items():
                atomic_write(folder / name, payload)
            if not existed:
                bind_characters(connection, run)
                sync_accepted_memory(connection, run)
                connection.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                                   (run['id'], run['template_id'], json.dumps(run, ensure_ascii=False), run['created_at']))
            for record_id, original in old.items():
                assert connection.execute('SELECT run_json FROM sessions WHERE id=?', (record_id,)).fetchone()[0] == original
            connection.commit()
            record = dict(record or {})
            record.update(session_id=project_id,
                          project_name=args.version + (' · 150条已复查候选' if args.selected else ' · 待优化150条'),
                          working_dir=f'.working_dir/{project_id}', idea=template['analysis']['primary_goal'],
                          style='dialogue', stage=run['status'],
                          summary='群聊 150/150 条 · ' + ('双人全文审阅，见版本报告' if args.selected else '人工需调整'))
            if evolution:
                publication = run['publication']
                human = {'candidate': '人工候选', 'alternative': '表达备选', 'needs_revision': '需修订'}[publication['human_verdict']]
                if run['status'] == 'failed':
                    human = '需修订'
                if args.selected:
                    human = '推荐候选'
                if publication.get('representative_version') and publication['representative_version'] != args.version:
                    human = '历史备选，详见校正说明'
                program = {'deferred': '模型待审', 'reviewed': '模型已全文复核', 'not_completed': '模型审阅未完成'}[publication['program_review_state']]
                record.update(project_name=args.version + ' · ' + human + ' · ' + program,
                              summary=publication['human_summary'])
                if independent:
                    record['project_name'] = (publication['display_label'] + ' · ' + publication['style_label']
                                              + ' · ' + ('用户已选' if args.selected else publication['review_label'])
                                              + ' · ' + program)
            record.setdefault('created_at', run['created_at'])
            record.setdefault('updated_at', run.get('updated_at', run['created_at']))
            sessions[project_id] = record
            index.save(data)  # Preserve active_session_id and every unrelated index field.
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(json.dumps({'imported': run['id'], 'idempotent': existed, 'messages': len(run['messages']),
                      'old_records_unchanged': len(old),
                      'sha256': hashlib.sha256(canonical(run['messages']).encode()).hexdigest()}, ensure_ascii=True))


if __name__ == '__main__':
    main()
