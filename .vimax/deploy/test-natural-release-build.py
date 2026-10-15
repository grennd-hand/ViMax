"""Isolated git/source-bundle tests and fake SSH only; never opens a network connection."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('release_builder', HERE / 'build-natural-release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        validation = HERE.parents[0] / 'validation/evolution-build-tests'
        validation.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='fixture-', dir=validation)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_git('init', '-q')
        self.run_git('config', 'user.name', 'local-fixture')
        self.run_git('config', 'user.email', 'fixture@invalid')
        self.write('.gitignore', '.vimax/\nweb/dist/\n')
        for name in ('dialogue/backend/requirements.txt', 'agents/screenwriter.py',
                     'interfaces/a.py', 'utils/a.py', 'agent_runtime/a.py',
                     'web/package.json', 'web/package-lock.json', 'deploy/Dockerfile.web'):
            self.write(name, 'fixture\n')
        self.write('dialogue/backend/app/main.py', 'BASE=True\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'base')
        self.previous = '20261015-' + self.run_git('rev-parse', '--short=7', 'HEAD')
        self.write('dialogue/backend/app/main.py', 'NEW=True\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'new backend')
        self.revision = self.run_git('rev-parse', '--short=7', 'HEAD')
        self.write('web/dist/index.html', '<html>built current fixture</html>')
        self.write('.vimax/deploy/runtime-model.yaml', 'LOCAL-SECRET-FIXTURE')

    def run_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT).decode().strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return builder.prepare(self.root, self.revision, self.previous)

    def test_bundle_uses_exact_commit_plus_untracked_dist_and_excludes_private_runtime(self):
        folder = self.prepare()
        with tarfile.open(folder / 'source.tar.gz', 'r:gz') as archive:
            names = archive.getnames()
            self.assertIn('web/dist/index.html', names)
            committed = subprocess.check_output(['git', '-C', str(self.root), 'show', 'HEAD:dialogue/backend/app/main.py'])
            # Git for Windows exports core.autocrlf as CRLF; compare the source
            # text and Git's archive commit marker without changing that policy.
            self.assertEqual(archive.extractfile('dialogue/backend/app/main.py').read().replace(b'\r\n', b'\n'), committed.replace(b'\r\n', b'\n'))
            self.assertEqual(archive.pax_headers['comment'], self.run_git('rev-parse', 'HEAD'))
            self.assertEqual(archive.extractfile('web/dist/index.html').read(), b'<html>built current fixture</html>')
            self.assertFalse(any('runtime-model.yaml' in name for name in names))
        record = builder.load_prepared(self.root, self.revision, self.previous)[1]
        self.assertEqual(record['commit'], self.run_git('rev-parse', 'HEAD'))
        self.assertEqual(record['backend_base_image'], 'vimax-dialogue-backend:' + self.previous)

    def test_existing_local_bundle_cannot_be_overwritten(self):
        folder = self.prepare()
        original = (folder / 'source.tar.gz').read_bytes()
        with self.assertRaises(FileExistsError): self.prepare()
        self.assertEqual((folder / 'source.tar.gz').read_bytes(), original)

    def test_rejects_dirty_tracked_source_and_non_head_revision(self):
        self.write('dialogue/backend/app/main.py', 'DIRTY=True\n')
        with self.assertRaisesRegex(ValueError, 'clean'): self.prepare()
        self.run_git('checkout', '--', 'dialogue/backend/app/main.py')
        with self.assertRaisesRegex(ValueError, 'current committed HEAD'):
            builder.prepare(self.root, self.previous[-7:], self.previous)

    def test_rejects_dependency_changes_when_reusing_old_image(self):
        self.write('dialogue/backend/requirements.txt', 'new-dependency\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'changed dependency')
        self.revision = self.run_git('rev-parse', '--short=7', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'reuse is invalid'): self.prepare()

    def test_changed_dist_or_tar_is_rejected_before_ssh(self):
        folder = self.prepare()
        self.write('web/dist/index.html', 'different dist')
        with self.assertRaisesRegex(ValueError, 'dist changed'):
            builder.load_prepared(self.root, self.revision, self.previous)
        self.write('web/dist/index.html', '<html>built current fixture</html>')
        with (folder / 'source.tar.gz').open('ab') as stream: stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'Bundle hash changed'):
            builder.load_prepared(self.root, self.revision, self.previous)

    def test_tracked_secret_and_archive_links_are_rejected(self):
        self.run_git('add', '-f', '.vimax/deploy/runtime-model.yaml')
        self.run_git('commit', '-qm', 'invalid private fixture')
        self.revision = self.run_git('rev-parse', '--short=7', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'Private/runtime file'): self.prepare()
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as archive:
            member = tarfile.TarInfo('link'); member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'
            archive.addfile(member)
        payload.seek(0)
        with tarfile.open(fileobj=payload) as archive:
            with self.assertRaisesRegex(ValueError, 'links/devices'): builder.validate_members(archive)

    def fake_ssh(self, preflight_code=0):
        calls, uploads = [], []
        class Channel:
            def set_combine_stderr(inner, value): pass
            def recv_exit_status(inner): return preflight_code if len(calls) == 1 else 0
        class Output(io.StringIO):
            def __init__(inner):
                super().__init__('local-test-password\n'); inner.channel = Channel()
        class SFTP:
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def put(inner, local, remote): uploads.append((local, remote))
            def chmod(inner, path, mode): self.assertEqual(mode, 0o600)
            def file(inner, path, mode): return io.BytesIO(('sha256:' + 'a' * 64).encode())
        class Client:
            def load_system_host_keys(inner): pass
            def set_missing_host_key_policy(inner, policy): pass
            def connect(inner, *args, **kwargs):
                self.assertEqual(kwargs['password'], 'local-test-password')
                self.assertFalse(kwargs['look_for_keys'])
            def exec_command(inner, script, **kwargs):
                calls.append(script)
                return None, Output(), None
            def open_sftp(inner): return SFTP()
            def close(inner): pass
        return types.SimpleNamespace(SSHClient=Client, RejectPolicy=lambda: object()), calls, uploads

    def test_previous_or_existing_release_preflight_failure_blocks_all_uploads(self):
        self.prepare()
        fake, calls, uploads = self.fake_ssh(preflight_code=1)
        with patch.dict(sys.modules, {'paramiko': fake}), patch.dict(os.environ, {'VIMAX_TASK_SSH_PASSWORD': 'local-test-password'}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'Remote step failed'):
                builder.build(self.root, self.revision, self.previous, 'fixture.invalid', 22, 'fixture')
        self.assertEqual(len(calls), 1)
        self.assertFalse(uploads)
        script = calls[0]
        self.assertLess(script.index('readlink -f /opt/vimax/current'), script.index('mkdir -m 700'))
        self.assertIn('test ! -e', script)

    def test_two_images_backend_test_and_receipt_without_service_start_or_secret_logging(self):
        folder = self.prepare()
        fake, calls, uploads = self.fake_ssh()
        output = io.StringIO()
        with patch.dict(sys.modules, {'paramiko': fake}), patch.dict(os.environ, {'VIMAX_TASK_SSH_PASSWORD': 'local-test-password'}), contextlib.redirect_stdout(output):
            builder.build(self.root, self.revision, self.previous, 'fixture.invalid', 22, 'fixture')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(uploads), 2)
        self.assertNotIn('local-test-password', output.getvalue())
        self.assertTrue(all(remote.startswith('/opt/vimax/releases/20261015-' + self.revision + '/') for _, remote in uploads))
        script = builder.build_script(builder.load_prepared(self.root, self.revision, self.previous)[1])
        self.assertEqual(script.count('docker build -t '), 2)
        self.assertIn('python -m unittest discover', script)
        for prohibited in (' up ', 'docker stop', 'ln -s', 'import-run.py', '/shared/state'):
            self.assertNotIn(prohibited, script)
        receipt = json.loads((folder / 'build-receipt.json').read_bytes())
        self.assertFalse(receipt['service_started'])
        self.assertEqual(set(receipt['image_ids']), {'backend', 'web'})

    def test_generated_shell_syntax_and_wrong_previous_stop_before_docker(self):
        shell = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
        if not shell.is_file(): self.skipTest('Git Bash syntax checker is unavailable')
        folder = self.prepare()
        record = builder.load_prepared(self.root, self.revision, self.previous)[1]
        for script in (builder.preflight_script(record), builder.build_script(record)):
            result = subprocess.run([str(shell), '--noprofile', '--norc', '-n'],
                                    input=script.encode(), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        injected = "readlink() { printf '%s' /opt/vimax/releases/wrong; }; docker() { echo DOCKER_REACHED >&2; return 91; };\n"
        result = subprocess.run([str(shell), '--noprofile', '--norc'],
                                input=(injected + builder.preflight_script(record)).encode(), capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'DOCKER_REACHED', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
