import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location('container_entrypoint', Path(__file__).resolve().parents[1] / 'docker-entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class ContainerEntrypointTests(unittest.TestCase):
    def test_default_identity_and_mask(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(entrypoint.identity('PUID', 99), 99)
            self.assertEqual(entrypoint.identity('PGID', 100), 100)
            self.assertEqual(entrypoint.permissions_mask(), 0o022)

    def test_custom_identity_and_mask(self):
        with patch.dict(os.environ, {'PUID': '1001', 'PGID': '1002', 'UMASK': '0002'}, clear=True):
            self.assertEqual(entrypoint.identity('PUID', 99), 1001)
            self.assertEqual(entrypoint.identity('PGID', 100), 1002)
            self.assertEqual(entrypoint.permissions_mask(), 0o002)

    def test_invalid_or_root_ids_rejected(self):
        for value in ('0', '-1', '', 'abc', '1.2', '4294967295'):
            with self.subTest(value=value), patch.dict(os.environ, {'PUID': value}):
                with self.assertRaises(ValueError):
                    entrypoint.identity('PUID', 99)

    def test_invalid_masks_rejected(self):
        for value in ('', '22', '888', '1022', '-022', 'abc'):
            with self.subTest(value=value), patch.dict(os.environ, {'UMASK': value}):
                with self.assertRaises(ValueError):
                    entrypoint.permissions_mask()

    def test_existing_volume_files_change_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / 'nested'
            nested.mkdir()
            file = nested / 'state.txt'
            file.write_text('existing state')
            with patch.object(os, 'geteuid', return_value=0, create=True), patch.object(os, 'chown', create=True) as chown:
                entrypoint.prepare_directory(directory, 1001, 1002)
            chown.assert_any_call(Path(directory), 1001, 1002)
            chown.assert_any_call(nested, 1001, 1002, follow_symlinks=False)
            chown.assert_any_call(file, 1001, 1002, follow_symlinks=False)
            self.assertEqual(file.read_text(), 'existing state')

    def test_drops_privileges_before_executing_command(self):
        actions = Mock()
        with patch.dict(os.environ, {'PUID': '1001', 'PGID': '1002', 'UMASK': '002'}, clear=True), \
                patch.object(os, 'geteuid', return_value=0, create=True), \
                patch.object(entrypoint, 'prepare_directory'), \
                patch.object(os, 'umask', actions.umask), \
                patch.object(os, 'setgroups', actions.setgroups, create=True), \
                patch.object(os, 'setgid', actions.setgid, create=True), \
                patch.object(os, 'setuid', actions.setuid, create=True), \
                patch.object(os, 'execvp', actions.execvp), \
                patch.object(entrypoint.sys, 'argv', ['docker-entrypoint.py', 'python', 'app.py']):
            entrypoint.main()
        self.assertEqual(actions.mock_calls, [
            call.umask(0o002), call.setgroups([]), call.setgid(1002), call.setuid(1001),
            call.execvp('python', ['python', 'app.py']),
        ])

    def test_mismatched_user_override_fails_before_changing_files(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(os, 'geteuid', return_value=1001, create=True), \
                patch.object(os, 'getegid', return_value=1002, create=True), \
                patch.object(entrypoint, 'prepare_directory') as prepare:
            with self.assertRaisesRegex(ValueError, 'must match PUID and PGID'):
                entrypoint.main()
            prepare.assert_not_called()

    def test_symlink_volume_root_is_rejected(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
                entrypoint.prepare_directory('/app/data', 99, 100)


if __name__ == '__main__':
    unittest.main()
