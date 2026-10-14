"""Update handoffs use temporary fake executables and never launch programs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mechahub.core import updates_install as installer
from mechahub.core.updates import ReleaseManifest, UpdateError
from mechahub.core.updates_download import ArtifactDownloadResult


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.application = self.folder / 'Application with spaces'
        self.application.mkdir()
        self.target = self.application / 'MechaHub.exe'
        self.target.write_bytes(b'previous verified application')
        self.download = self.folder / 'download.exe'
        self.payload = b'new verified application payload'
        self.download.write_bytes(self.payload)
        self.stage = self.folder / 'stage'
        self.stage.mkdir()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.context = installer.InstallContext('portable', self.target)
        self.manifest = ReleaseManifest('0.6.0', 'https://example.com/MechaHub.exe',
                                        sha256=self.digest, size=len(self.payload),
                                        installer_download_url='https://example.com/MechaHubSetup.exe',
                                        installer_sha256=self.digest, installer_size=len(self.payload))
        self.artifact = ArtifactDownloadResult(self.download, len(self.payload), self.digest)

    def prepare(self, **kwargs):
        with patch.object(installer, '_stage_root', return_value=self.stage), \
                patch.object(installer, '_parent_creation_time', return_value=123), \
                patch.object(installer, '_bootstrap_parent', return_value=(0, 0)):
            return installer.prepare_installation(kwargs.pop('manifest', self.manifest),
                                                   kwargs.pop('artifact', self.artifact),
                                                   context=kwargs.pop('context', self.context), **kwargs)

    def read_plan(self, prepared):
        return json.loads(prepared.plan_path.read_text(encoding='utf-8'))

    def update_plan(self, prepared, **changes):
        plan = self.read_plan(prepared)
        plan.update(changes)
        prepared.plan_path.write_text(json.dumps(plan), encoding='utf-8')

    def test_registered_folder_only_marks_the_actual_installed_copy(self):
        with patch.object(installer, '_registered_directory', return_value=self.application):
            actual = installer.detect_installation(self.target, frozen=True)
            portable = installer.detect_installation(self.download, frozen=True)
            source = installer.detect_installation(self.target, frozen=False)
        self.assertEqual(actual.mode, 'installed')
        self.assertEqual(actual.install_directory, self.application)
        self.assertEqual(portable.mode, 'portable')
        self.assertEqual(source.mode, 'source')

    def test_registered_folder_does_not_claim_another_named_executable(self):
        another = self.application / 'another.exe'
        another.write_bytes(b'other')
        with patch.object(installer, '_registered_directory', return_value=self.application):
            self.assertEqual(installer.detect_installation(another, frozen=True).mode, 'portable')

    def test_prepare_stages_independent_verified_files_without_execution(self):
        with patch.object(installer, '_popen') as started:
            prepared = self.prepare()
        started.assert_not_called()
        self.assertEqual(prepared.target_path, self.target)
        self.assertEqual(prepared.helper_path.read_bytes(), self.target.read_bytes())
        self.assertEqual((prepared.plan_path.parent / 'payload.exe').read_bytes(), self.payload)
        plan = self.read_plan(prepared)
        self.assertEqual(plan['parent_pid'], os.getpid())
        self.assertEqual(plan['parent_creation'], 123)
        self.assertEqual(plan['bootstrap_pid'], 0)
        self.assertEqual(plan['payload_sha256'], self.digest)

    def test_legacy_portable_manifest_without_size_uses_verified_download_size(self):
        manifest = ReleaseManifest('0.6.0', 'https://example.com/MechaHub.exe', sha256=self.digest)
        prepared = self.prepare(manifest=manifest)
        self.assertEqual(self.read_plan(prepared)['new_target_size'], len(self.payload))

    def test_untrusted_download_result_and_tampered_payload_are_rejected(self):
        cases = [ArtifactDownloadResult(self.download, len(self.payload) + 1, self.digest),
                 ArtifactDownloadResult(self.download, len(self.payload), 'a' * 64)]
        for artifact in cases:
            with self.subTest(artifact=artifact), self.assertRaises(UpdateError):
                self.prepare(artifact=artifact)
        self.download.write_bytes(b'changed')
        with self.assertRaises(UpdateError):
            self.prepare()
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_source_copy_is_not_replaced_or_executed(self):
        with self.assertRaisesRegex(UpdateError, 'Windows EXE'):
            self.prepare(context=installer.InstallContext('source', self.target))
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_non_exe_payload_and_missing_checksum_are_not_installable(self):
        invalid = [ReleaseManifest('0.6.0', 'https://example.com/a.zip', sha256=self.digest),
                   ReleaseManifest('0.6.0', 'https://example.com/a.exe', sha256='')]
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(UpdateError):
                self.prepare(manifest=manifest)

    def test_installed_mode_requires_installer_checksum_and_existing_directory(self):
        context = installer.InstallContext('installed', self.target, self.application)
        portable_only = ReleaseManifest('0.6.0', 'https://example.com/a.exe', sha256=self.digest)
        with self.assertRaises(UpdateError):
            self.prepare(manifest=portable_only, context=context)
        with self.assertRaises(UpdateError):
            self.prepare(context=installer.InstallContext('installed', self.target, self.folder))

    def test_unwritable_directory_is_reported_without_staging(self):
        with patch.object(installer.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(UpdateError, 'прав'):
                self.prepare()
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_copy_failure_removes_only_its_staging_files(self):
        with patch.object(installer.shutil, 'copyfile', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(UpdateError, 'disk full'):
                self.prepare()
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(self.download.read_bytes(), self.payload)

    def test_plan_validation_rejects_unknown_fields_bad_pid_size_and_paths(self):
        changes = [dict(unrecognized=True), dict(parent_pid=True), dict(parent_creation=0),
                   dict(bootstrap_pid=456, bootstrap_creation=0), dict(payload_size=True),
                   dict(payload_sha256='g' * 64), dict(target_path='relative.exe'),
                   dict(helper_path=str(self.target)), dict(mode='delete'), dict(mode=[]), dict(schema=True)]
        for change in changes:
            prepared = self.prepare()
            self.update_plan(prepared, **change)
            with self.subTest(change=change), self.assertRaises(UpdateError):
                installer._load_plan(prepared.plan_path)

    def test_oversized_and_invalid_json_plans_are_rejected(self):
        prepared = self.prepare()
        for content in ('x' * (installer.MAX_PLAN_BYTES + 1), '{broken', '[]'):
            prepared.plan_path.write_text(content, encoding='utf-8')
            with self.subTest(content=content[:20]), self.assertRaises(UpdateError):
                installer._load_plan(prepared.plan_path)

    def test_helper_launch_uses_safe_argument_array(self):
        prepared = self.prepare()
        with patch.object(installer, '_popen', return_value=Mock()) as started:
            installer.start_installation(prepared)
        self.assertEqual(started.call_args.args[0], [str(prepared.helper_path), '--apply-update',
                                                     str(prepared.plan_path)])
        self.assertEqual(started.call_args.kwargs['cwd'], prepared.plan_path.parent)

    def test_helper_tampering_and_start_failure_keep_original_untouched(self):
        prepared = self.prepare()
        with patch.object(installer, '_popen', side_effect=OSError('cannot start')):
            with self.assertRaisesRegex(UpdateError, 'залишиться відкритою'):
                installer.start_installation(prepared)
        prepared.helper_path.write_bytes(b'tampered')
        with patch.object(installer, '_popen') as started:
            with self.assertRaises(UpdateError):
                installer.start_installation(prepared)
            started.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_parent_gui_waits_for_readiness_and_receives_startup_failure(self):
        prepared = self.prepare()
        process = Mock()
        process.poll.return_value = None
        self.assertFalse(installer.installation_ready(prepared, process))
        ready = prepared.plan_path.parent / 'ready.json'
        ready.write_text('{"ready": true}', encoding='utf-8')
        self.assertTrue(installer.installation_ready(prepared, process))
        result = prepared.plan_path.parent / 'result.json'
        result.write_text('{"error": "payload changed"}', encoding='utf-8')
        with self.assertRaisesRegex(UpdateError, 'payload changed'):
            installer.installation_ready(prepared, process)

    def test_a_helper_that_exits_before_ready_does_not_request_gui_shutdown(self):
        prepared = self.prepare()
        process = Mock()
        process.poll.return_value = 1
        with self.assertRaisesRegex(UpdateError, 'залишиться відкритою'):
            installer.installation_ready(prepared, process)

    def test_a_helper_that_crashes_after_ready_still_keeps_parent_open(self):
        prepared = self.prepare()
        (prepared.plan_path.parent / 'ready.json').write_text('{"ready": true}', encoding='utf-8')
        process = Mock()
        process.poll.return_value = 1
        with self.assertRaisesRegex(UpdateError, 'залишиться відкритою'):
            installer.installation_ready(prepared, process)

    def test_discard_preserves_download_and_target_and_rejects_foreign_files(self):
        prepared = self.prepare()
        unexpected = prepared.plan_path.parent / 'important.txt'
        unexpected.write_text('keep', encoding='utf-8')
        with self.assertRaises(UpdateError):
            installer.discard_prepared(prepared)
        self.assertEqual(unexpected.read_text(), 'keep')
        unexpected.unlink()
        installer.discard_prepared(prepared)
        self.assertFalse(prepared.plan_path.parent.exists())
        self.assertEqual(self.download.read_bytes(), self.payload)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_portable_update_replaces_same_executable_and_retains_only_staged_previous_copy(self):
        prepared = self.prepare()
        plan = installer._load_plan(prepared.plan_path)
        with patch.object(installer, '_restart') as restart:
            installer._apply_portable(plan)
        restart.assert_called_once_with(self.target)
        self.assertEqual(self.target.read_bytes(), self.payload)
        backup = list(self.application.glob('.MechaHub.previous-*.exe'))
        self.assertEqual(backup, [])
        self.assertEqual(prepared.helper_path.read_bytes(), b'previous verified application')
        self.assertFalse(list(self.application.glob('.MechaHub-new-*')))

    def test_portable_publication_failure_restores_and_restarts_previous_copy(self):
        prepared = self.prepare()
        plan = installer._load_plan(prepared.plan_path)
        rename = os.rename
        calls = 0

        def fail_new_publication(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError('new executable locked')
            return rename(source, destination)

        with patch.object(installer.os, 'rename', side_effect=fail_new_publication), \
                patch.object(installer, '_restart') as restart:
            with self.assertRaisesRegex(UpdateError, 'Попередню копію збережено'):
                installer._apply_portable(plan)
        restart.assert_called_once_with(self.target)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')
        self.assertFalse(list(self.application.glob('.MechaHub-new-*')))

    def test_portable_new_launch_failure_rolls_back_and_restarts_old_copy(self):
        plan = installer._load_plan(self.prepare().plan_path)
        with patch.object(installer, '_restart', side_effect=[OSError('bad exe'), Mock()]) as restart:
            with self.assertRaisesRegex(UpdateError, 'Попередню версію відновлено'):
                installer._apply_portable(plan)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_corrupt_payload_copy_never_moves_existing_executable(self):
        plan = installer._load_plan(self.prepare().plan_path)
        plan['payload_path'].write_bytes(b'corrupt new payload')
        with patch.object(installer, '_restart') as restart:
            with self.assertRaises(UpdateError):
                installer._apply_portable(plan)
        restart.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_installer_command_updates_exact_directory_and_does_not_use_shell(self):
        context = installer.InstallContext('installed', self.target, self.application)
        prepared = self.prepare(context=context)
        plan = installer._load_plan(prepared.plan_path)
        process = Mock()
        def install_payload():
            self.target.write_bytes(self.payload)
            return 0
        process.wait.side_effect = install_payload
        with patch.object(installer, 'detect_installation', return_value=context), \
                patch.object(installer, '_popen', return_value=process) as started, \
                patch.object(installer, '_restart') as restart:
            installer._apply_installed(plan)
        arguments = started.call_args.args[0]
        self.assertEqual(arguments[0], str(plan['payload_path']))
        self.assertIn(f'/DIR={self.application}', arguments)
        self.assertIn('/SILENT', arguments)
        self.assertIn('/SUPPRESSMSGBOXES', arguments)
        self.assertIn('/NORESTART', arguments)
        self.assertIn('/NOCLOSEAPPLICATIONS', arguments)
        self.assertIn('/NORESTARTAPPLICATIONS', arguments)
        self.assertIn(f'/MECHAHUBUPDATE={os.getpid()}', arguments)
        restart.assert_called_once_with(self.target)
        self.assertFalse(list(self.application.glob('.MechaHub.previous-*.exe')))
        self.assertEqual(prepared.helper_path.read_bytes(), b'previous verified application')

    def test_installer_failure_restarts_unchanged_old_executable(self):
        context = installer.InstallContext('installed', self.target, self.application)
        plan = installer._load_plan(self.prepare(context=context).plan_path)
        process = Mock()
        process.wait.return_value = 5
        with patch.object(installer, 'detect_installation', return_value=context), \
                patch.object(installer, '_popen', return_value=process), \
                patch.object(installer, '_restart') as restart:
            with self.assertRaisesRegex(UpdateError, 'код 5'):
                installer._apply_installed(plan)
        restart.assert_called_once_with(self.target)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')

    def test_partially_applied_installer_restores_verified_old_executable(self):
        context = installer.InstallContext('installed', self.target, self.application)
        plan = installer._load_plan(self.prepare(context=context).plan_path)
        process = Mock()

        def partial_install():
            self.target.write_bytes(b'partially replaced incorrect executable')
            return 5

        process.wait.side_effect = partial_install
        with patch.object(installer, 'detect_installation', return_value=context), \
                patch.object(installer, '_popen', return_value=process), \
                patch.object(installer, '_restart') as restart:
            with self.assertRaisesRegex(UpdateError, 'відновлено'):
                installer._apply_installed(plan)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')
        restart.assert_called_once_with(self.target)
        self.assertEqual(plan['helper_path'].read_bytes(), b'previous verified application')

    def test_rollback_copies_staged_helper_into_target_drive_before_atomic_replace(self):
        plan = installer._load_plan(self.prepare().plan_path)
        self.target.write_bytes(b'partial installer replacement')
        replace = os.replace
        destinations = []

        def sibling_replace(source, destination):
            source = Path(source)
            self.assertEqual(source.parent, self.target.parent)
            self.assertNotEqual(source, plan['helper_path'])
            self.assertEqual(source.read_bytes(), b'previous verified application')
            destinations.append(destination)
            return replace(source, destination)

        with patch.object(installer.os, 'replace', side_effect=sibling_replace):
            installer._restore_old_target(plan)
        self.assertEqual(destinations, [self.target])
        self.assertEqual(self.target.read_bytes(), b'previous verified application')
        self.assertEqual(plan['helper_path'].read_bytes(), b'previous verified application')
        self.assertFalse(list(self.application.glob('.MechaHub-restore-*')))

    def test_installer_success_with_wrong_executable_is_rolled_back_before_launch(self):
        context = installer.InstallContext('installed', self.target, self.application)
        plan = installer._load_plan(self.prepare(context=context).plan_path)
        process = Mock()
        process.wait.return_value = 0
        with patch.object(installer, 'detect_installation', return_value=context), \
                patch.object(installer, '_popen', return_value=process), \
                patch.object(installer, '_restart') as restart:
            with self.assertRaisesRegex(UpdateError, 'відновлено'):
                installer._apply_installed(plan)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')
        restart.assert_called_once_with(self.target)

    def test_changed_registration_or_payload_never_starts_installer(self):
        context = installer.InstallContext('installed', self.target, self.application)
        plan = installer._load_plan(self.prepare(context=context).plan_path)
        with patch.object(installer, 'detect_installation', return_value=self.context), \
                patch.object(installer, '_popen') as started:
            with self.assertRaises(UpdateError):
                installer._apply_installed(plan)
            started.assert_not_called()
        plan['payload_path'].write_bytes(b'wrong')
        with patch.object(installer, 'detect_installation', return_value=context), \
                patch.object(installer, '_popen') as started:
            with self.assertRaises(UpdateError):
                installer._apply_installed(plan)
            started.assert_not_called()

    def test_parent_handle_rejects_reused_pid_and_always_closes_handle(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 999
        with patch.object(installer, '_windows_api', return_value=(kernel, Mock())), \
                patch.object(installer, '_creation_time', return_value=456):
            with self.assertRaisesRegex(UpdateError, 'Процес програми змінився'):
                installer._ParentHandle(123, 789)
        kernel.CloseHandle.assert_called_once_with(999)

    def test_parent_handle_wait_is_bounded_and_timeout_blocks_installation(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 999
        kernel.WaitForSingleObject.return_value = 258
        with patch.object(installer, '_windows_api', return_value=(kernel, Mock())), \
                patch.object(installer, '_creation_time', return_value=456), \
                patch.object(installer.time, 'monotonic', side_effect=[0, installer.PARENT_WAIT_SECONDS + 1]):
            parent = installer._ParentHandle(123, 456)
            with self.assertRaisesRegex(UpdateError, 'не завершилася вчасно'):
                parent.wait()
            parent.close()
        kernel.WaitForSingleObject.assert_called_once_with(999, 200)
        kernel.CloseHandle.assert_called_once_with(999)

    def test_cancelled_handoff_never_waits_for_parent_or_installs_later(self):
        prepared = self.prepare()
        installer.cancel_installation(prepared)
        installer.cancel_installation(prepared)
        kernel = Mock()
        kernel.OpenProcess.return_value = 999
        with patch.object(installer, '_windows_api', return_value=(kernel, Mock())), \
                patch.object(installer, '_creation_time', return_value=456):
            parent = installer._ParentHandle(123, 456)
            with self.assertRaisesRegex(UpdateError, 'скасовано'):
                parent.wait(prepared.plan_path.parent)
            parent.close()
        kernel.WaitForSingleObject.assert_not_called()

    def test_process_spawn_is_detached_without_shell_or_inherited_streams(self):
        with patch.object(installer.subprocess, 'Popen') as started:
            installer._popen(['some executable.exe', '/DIR=C:\\My Folder'], cwd=self.folder)
        self.assertFalse(started.call_args.kwargs['shell'])
        self.assertTrue(started.call_args.kwargs['close_fds'])
        self.assertEqual(started.call_args.args[0], ['some executable.exe', '/DIR=C:\\My Folder'])
        self.assertEqual(started.call_args.kwargs['stdin'], installer.subprocess.DEVNULL)
        self.assertEqual(started.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')

    def complete_stage(self, *, completed=100, failed=False):
        prepared = self.prepare()
        result = prepared.plan_path.parent / 'result.json'
        result.write_text(json.dumps({'error': 'failed'} if failed else {'success': True}), encoding='utf-8')
        os.utime(result, ns=(completed * 1_000_000_000, completed * 1_000_000_000))
        return prepared

    def cleanup_stages(self):
        with patch.object(installer, '_stage_root', return_value=self.stage):
            return installer.cleanup_finished_stages()

    def test_cleanup_deletes_at_most_two_old_successes_and_keeps_newest(self):
        older = [self.complete_stage(completed=index) for index in range(1, 5)]
        newest = self.complete_stage(completed=5)
        self.assertEqual(self.cleanup_stages(), 2)
        self.assertTrue(newest.plan_path.parent.exists())
        self.assertEqual(sum(item.plan_path.parent.exists() for item in older), 2)
        self.assertEqual(self.target.read_bytes(), b'previous verified application')
        self.assertEqual(self.download.read_bytes(), self.payload)

    def test_cleanup_retains_failed_unknown_and_unfinished_stages(self):
        failed = self.complete_stage(completed=1, failed=True)
        unknown = self.complete_stage(completed=2)
        (unknown.plan_path.parent / 'important.txt').write_text('preserve', encoding='utf-8')
        unfinished = self.prepare()
        self.complete_stage(completed=3)
        self.assertEqual(self.cleanup_stages(), 0)
        for prepared in (failed, unknown, unfinished):
            self.assertTrue(prepared.plan_path.parent.exists())

    def test_cleanup_rejects_path_traversal_in_stage_plan(self):
        outside = self.folder / 'payload.exe'
        outside.write_bytes(b'preserve outside')
        older = self.complete_stage(completed=1)
        self.update_plan(older, payload_path=str(outside))
        self.complete_stage(completed=2)
        self.assertEqual(self.cleanup_stages(), 0)
        self.assertEqual(outside.read_bytes(), b'preserve outside')
        self.assertTrue(older.plan_path.parent.exists())

    def test_cleanup_preserves_entire_busy_stage(self):
        older = self.complete_stage(completed=1)
        self.complete_stage(completed=2)
        open_file = os.open

        def busy(path, flags, *args, **kwargs):
            if Path(path) == older.helper_path:
                raise PermissionError('busy helper')
            return open_file(path, flags, *args, **kwargs)

        before = {item.name for item in older.plan_path.parent.iterdir()}
        with patch.object(installer.os, 'open', side_effect=busy):
            self.assertEqual(self.cleanup_stages(), 0)
        self.assertEqual({item.name for item in older.plan_path.parent.iterdir()}, before)

    def test_cleanup_preserves_a_reparse_point_without_following_it(self):
        older = self.complete_stage(completed=1)
        self.complete_stage(completed=2)
        original = Path.lstat

        def reparse(path):
            info = original(path)
            if path == older.plan_path.parent:
                return type('Reparse', (), {'st_mode': info.st_mode, 'st_file_attributes': 0x400})()
            return info

        with patch.object(Path, 'lstat', autospec=True, side_effect=reparse):
            self.assertEqual(self.cleanup_stages(), 0)
        self.assertTrue(older.plan_path.parent.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows-only helper entrypoint')
    def test_helper_acknowledges_before_wait_and_applies_only_after_parent_exits(self):
        prepared = self.prepare()
        parent = Mock()
        seen = []

        def wait(_folder):
            self.assertTrue((prepared.plan_path.parent / 'ready.json').exists())
            seen.append('wait')

        parent.wait.side_effect = wait
        with patch.object(installer.sys, 'frozen', True, create=True), \
                patch.object(installer.sys, 'executable', str(prepared.helper_path)), \
                patch.object(installer, '_ParentHandle', return_value=parent), \
                patch.object(installer, '_apply_portable', side_effect=lambda _: seen.append('apply')):
            code = installer.run_update_helper(prepared.plan_path)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ['wait', 'apply'])
        parent.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows-only helper entrypoint')
    def test_helper_rechecks_payload_after_wait_and_never_executes_changed_file(self):
        prepared = self.prepare()
        payload = prepared.plan_path.parent / 'payload.exe'
        parent = Mock()
        parent.wait.side_effect = lambda _folder: payload.write_bytes(b'changed while waiting')
        with patch.object(installer.sys, 'frozen', True, create=True), \
                patch.object(installer.sys, 'executable', str(prepared.helper_path)), \
                patch.object(installer, '_ParentHandle', return_value=parent), \
                patch.object(installer, '_apply_portable') as applied, \
                patch.object(installer, '_restart'), \
                patch.object(installer, '_notify_error') as notify:
            code = installer.run_update_helper(prepared.plan_path)
        self.assertEqual(code, 1)
        applied.assert_not_called()
        notify.assert_called_once()
        parent.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows-only helper entrypoint')
    def test_helper_waits_for_onefile_bootloader_before_replacing_executable(self):
        prepared = self.prepare()
        self.update_plan(prepared, bootstrap_pid=7654, bootstrap_creation=67890)
        parent, bootloader = Mock(), Mock()
        order = []
        parent.wait.side_effect = lambda _: order.append('parent')
        bootloader.wait.side_effect = lambda _: order.append('bootloader')
        with patch.object(installer.sys, 'frozen', True, create=True), \
                patch.object(installer.sys, 'executable', str(prepared.helper_path)), \
                patch.object(installer, '_ParentHandle', side_effect=[parent, bootloader]), \
                patch.object(installer, '_apply_portable', side_effect=lambda _: order.append('apply')):
            self.assertEqual(installer.run_update_helper(prepared.plan_path), 0)
        self.assertEqual(order, ['parent', 'bootloader', 'apply'])
        parent.close.assert_called_once()
        bootloader.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows-only process identity')
    def test_current_windows_process_handle_has_the_recorded_creation_time(self):
        created = installer._parent_creation_time(os.getpid())
        self.assertGreater(created, 0)
        handle = installer._ParentHandle(os.getpid(), created)
        handle.close()


if __name__ == '__main__':
    unittest.main()
