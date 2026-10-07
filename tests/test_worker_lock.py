"""Exercise the native worker lock across processes on each supported OS."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from jobs import JobQueue
from state_store import StateStore


CHILD = """
import sys
import tests  # Use fake configuration, never local credentials.
from jobs import JobQueue
from state_store import StateStore
try:
    queue = JobQueue(StateStore(sys.argv[1]), None)
except RuntimeError as error:
    print(error, flush=True)
    sys.exit(2)
print('locked', flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
queue.close()
"""


class WorkerLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'state.sqlite3')

    def child(self, path=None):
        return subprocess.run(
            [sys.executable, '-c', CHILD, path or self.path],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )

    def test_exclusion_release_and_independent_databases(self):
        # An old, nonempty file must still lock the same fixed region.
        Path(self.path + '.worker.lock').write_bytes(b'old lock file')
        queue = JobQueue(StateStore(self.path), None)
        try:
            blocked = self.child()
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn('Another poster worker', blocked.stdout)
            other = self.child(os.path.join(self.temp.name, 'other.sqlite3'))
            self.assertEqual(other.returncode, 0, other.stderr)
        finally:
            queue.close()
        released = self.child()
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_process_exit_releases_lock(self):
        process = subprocess.Popen(
            [sys.executable, '-c', CHILD, self.path, 'wait'],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            blocked = self.child()
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
        finally:
            process.kill()
            process.communicate(timeout=20)
        released = self.child()
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_rejected_worker_does_not_interrupt_active_jobs(self):
        store = StateStore(self.path)
        queue = JobQueue(store, None)
        self.addCleanup(queue.close)
        job = {'job_id': 'active', 'status': 'running', 'tasks': []}
        store.save_job(job)
        blocked = self.child()
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(store.get_job('active'), job)

    def test_shutdown_keeps_lock_until_worker_has_stopped(self):
        queue = JobQueue(StateStore(self.path), None)
        self.addCleanup(queue.close)
        # Model an upload still running after the shutdown join timeout.
        worker = Mock()
        worker.is_alive.return_value = True
        queue.worker = worker
        queue.close()
        self.assertTrue(queue.stop.is_set())
        worker.join.assert_called_once_with(timeout=5)
        blocked = self.child()
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        worker.is_alive.return_value = False
        queue.close()
        released = self.child()
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_restart_waits_for_explicit_resume_before_uploading(self):
        store = StateStore(self.path)
        item = {'id': 'movie', 'title': 'Example', 'type': 'Movie'}
        job = {
            'job_id': 'interrupted-upload', 'status': 'running', 'kind': 'manual',
            'prepared': True, 'options': {}, 'total_items': 1,
            'tasks': [{'item': item, 'targets': [
                {'target_id': 'movie', 'title': 'Primary', 'url': 'https://theposterdb.com/api/assets/1',
                 'status': 'uploading'},
            ]}],
        }
        store.save_job(job)
        service = Mock()
        uploaded = threading.Event()

        def upload(target):
            uploaded.set()
            return True

        service.upload.side_effect = upload
        queue = JobQueue(store, service, delay=0)
        self.addCleanup(queue.close)
        recovered = store.get_job(job['job_id'])
        self.assertEqual(recovered['status'], 'interrupted')
        self.assertEqual(recovered['tasks'][0]['targets'][0]['status'], 'pending')
        self.assertIsNone(queue.worker)
        queue.run(job['job_id'])
        service.upload.assert_not_called()
        queue.resume(job['job_id'])
        self.assertTrue(uploaded.wait(timeout=5), 'Explicit resume did not start the upload')
        queue.close()
        service.upload.assert_called_once()
        self.assertEqual(store.get_job(job['job_id'])['tasks'][0]['targets'][0]['status'], 'success')

    @unittest.skipIf(os.name == 'nt', 'POSIX compatibility runs on Linux CI')
    def test_original_posix_lock_blocks_new_worker(self):
        import fcntl

        # Use precisely the original implementation to check mixed versions.
        with open(self.path + '.worker.lock', 'a') as original_lock:
            fcntl.flock(original_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = self.child()
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
        released = self.child()
        self.assertEqual(released.returncode, 0, released.stderr)
