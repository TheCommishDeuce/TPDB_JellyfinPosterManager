"""One local worker for all uploads; checkpoints precede and follow each target."""
import fcntl
import threading
import uuid

import poster_scraper as scraper
from state_store import timestamp


class JobQueue:
    def __init__(self, store, service, delay=1.5, start_worker=True):
        self.store = store
        self.service = service
        self.delay = delay
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = None
        self.start_worker = start_worker
        self.worker_lock = None
        if start_worker:
            self.worker_lock = open(store.path + '.worker.lock', 'a')
            try:
                fcntl.flock(self.worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.worker_lock.close()
                raise RuntimeError('Another poster worker is using this database. Run one web process.')
        for job in self.store.jobs():
            if job['status'] in ('queued', 'running', 'cancelling'):
                job.update(status='interrupted', phase='interrupted', done=True, success=False,
                           message='Interrupted by restart. Resume explicitly to continue unfinished uploads.')
                for task in job.get('tasks', []):
                    for target in task.get('targets', []):
                        if target['status'] == 'uploading':
                            target['status'] = 'pending'
                self.store.save_job(job)

    def submit(self, kind, tasks=None, options=None):
        if kind not in ('auto', 'manual', 'retry'):
            raise ValueError('Unknown job kind')
        tasks = tasks if tasks is not None else []
        for task in tasks:
            if 'targets' not in task:
                task['targets'] = self.service.targets(task['item'], task['selection'])
        job = {
            'job_id': str(uuid.uuid4()), 'kind': kind, 'options': options or {},
            'tasks': tasks, 'prepared': kind != 'auto' or bool(tasks), 'status': 'queued',
            'phase': 'queued', 'done': False, 'success': None, 'cancel_requested': False,
            'message': 'Waiting for the poster worker', 'created_at': timestamp(),
            'results': [], 'total_items': len(tasks), 'processed': 0, 'remaining': len(tasks),
            'successful': 0, 'failed': 0, 'skipped': 0,
        }
        with self.lock:
            self.store.save_job(job, planned=True)
            self._start()
        return job

    def _start(self):
        if self.start_worker and (not self.worker or not self.worker.is_alive()):
            self.worker = threading.Thread(target=self._work, name='poster-worker', daemon=True)
            self.worker.start()
        self.wake.set()

    def _work(self):
        while not self.stop.is_set():
            self.wake.wait(1)
            self.wake.clear()
            for job in reversed(self.store.jobs()):
                if self.stop.is_set():
                    return
                if job['status'] == 'queued':
                    self.run(job['job_id'])

    def _save(self, job, outcome=None, planned=False):
        with self.lock:
            current = self.store.get_job(job['job_id'])
            if current and current.get('cancel_requested'):
                job['cancel_requested'] = True
            self.store.save_job(job, outcome, planned=planned)

    def cancel(self, job_id):
        with self.lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job['status'] in ('queued', 'interrupted'):
                job.update(status='cancelled', phase='cancelled', done=True, success=False,
                           cancel_requested=True, message='Cancelled before the next upload.')
            elif not job['done']:
                job.update(cancel_requested=True, status='cancelling', message='Stopping before the next upload.')
            self.store.save_job(job)
            return job

    def resume(self, job_id):
        with self.lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job['status'] not in ('interrupted', 'cancelled', 'failed'):
                raise ValueError('Only interrupted, cancelled or stopped jobs can be resumed')
            if job.get('prepared') and not any(
                target['status'] == 'pending' for task in job['tasks'] for target in task.get('targets', [])
            ) and all('targets' in task for task in job['tasks']):
                raise ValueError('No unfinished targets. Use Retry for failed targets.')
            job.update(status='queued', phase='queued', done=False, success=None,
                       cancel_requested=False, error=None, message='Waiting to resume unfinished uploads')
            self.store.save_job(job)
            self._start()
            return job

    def _cancelled(self, job):
        return self.stop.is_set() or self.store.get_job(job['job_id']).get('cancel_requested', False)

    def _prepare(self, job):
        options = job['options']
        items = scraper.get_jellyfin_items()
        target_filter = options.get('filter', 'no-poster')
        ids = set(options.get('item_ids', []))
        processed = {entry['item_id'] for entry in self.store.processed_items(limit=1000000)} if options.get('skip_processed') else set()
        protected = self.store.values('protected')
        job['tasks'] = [
            {'item': item} for item in items
            if item['id'] not in protected and item['id'] not in processed
            and (not options.get('library_id') or item.get('library_id') == options['library_id'])
            and (target_filter == 'all' or (target_filter == 'queued' and item['id'] in ids)
                 or (target_filter == 'no-poster' and not item.get('thumbnail_url'))
                 or (target_filter == 'movies' and item.get('type') == 'Movie')
                 or (target_filter == 'series' and item.get('type') == 'Series')
                 or (target_filter == 'collections' and item.get('type') == 'BoxSet'))
        ]
        job['prepared'] = True
        job['total_items'] = len(job['tasks'])
        self._save(job)

    def _summarize(self, job):
        results = []
        finished = 0
        for task in job['tasks']:
            targets = task.get('targets', [])
            if not targets or all(target['status'] == 'pending' for target in targets):
                continue
            item = task['item']
            success = all(target['status'] == 'success' for target in targets)
            pending = any(target['status'] in ('pending', 'uploading') for target in targets)
            skipped = all(target['status'] == 'skipped' for target in targets)
            if not pending:
                finished += 1
            has_failures = any(target['status'] in ('failed', 'review') for target in targets)
            uploaded_any = any(target['status'] == 'success' for target in targets)
            results.append({
                'has_failures': has_failures,
                'skipped': not has_failures and not pending and any(target['status'] == 'skipped' for target in targets),
                'item_id': item['id'], 'item_title': item.get('title', item['id']),
                'success': success, 'uploaded_any': uploaded_any,
                'status': 'success' if success else 'skipped' if skipped else 'partial' if pending or uploaded_any else 'failed',
                'error': '; '.join(target.get('error') or '' for target in targets if target.get('error')) or ('Unfinished targets remain' if pending else None),
                'poster_url': next((target.get('url') for target in targets if target['target_id'] == item['id'] and target['status'] == 'success'), None),
                'primary_uploaded': any(target['target_id'] == item['id'] and target['status'] == 'success' for target in targets),
                'season_results': [dict(season_id=target['target_id'], season_title=target['title'],
                                        success=target['status'] == 'success', error=target.get('error'))
                                   for target in targets if target['target_id'] != item['id']],
                'operation': job['kind'],
            })
        targets = [target for task in job['tasks'] for target in task.get('targets', [])]
        job.update(results=results, processed=finished, remaining=job['total_items'] - finished,
                   completed_targets=sum(target['status'] not in ('pending', 'uploading') for target in targets),
                   total_targets=len(targets), successful=sum(result['success'] for result in results),
                   failed=sum(result['has_failures'] for result in results),
                   skipped=sum(result['skipped'] for result in results))

    def run(self, job_id):
        with self.run_lock:
            job = self.store.get_job(job_id)
            if not job or job['status'] != 'queued':
                return
            job.update(status='running', phase='preparing', message='Preparing poster job')
            self._save(job)
            try:
                if not job['prepared']:
                    self._prepare(job)
                for task in job['tasks']:
                    if self._cancelled(job):
                        break
                    item = task['item']
                    if task.get('targets') and all(target['status'] in ('success', 'failed', 'skipped') or
                                                   (target['status'] == 'review' and target.get('recorded')) for target in task['targets']):
                        continue
                    job.update(current_item=item.get('title'), current_item_id=item['id'],
                               old_poster_url=item.get('thumbnail_url'), phase='searching',
                               message='Preparing posters for ' + (item.get('title') or item['id']))
                    self._save(job)
                    protected = item['id'] in self.store.values('protected')
                    if protected and not (job['kind'] == 'manual' and item['id'] in job['options'].get('confirmed_protected_ids', [])):
                        if 'targets' not in task:
                            task['targets'] = [dict(target_id=item['id'], title='Primary', status='skipped', error='Protected item')]
                        else:
                            for target in task['targets']:
                                if target['status'] == 'pending':
                                    target.update(status='skipped', error='Protected item')
                    if 'targets' not in task:
                        try:
                            task['targets'] = self.service.discover(item, job['options'])
                        except scraper.TPDBRateLimited:
                            raise
                        except Exception as error:
                            task['targets'] = [dict(target_id=item['id'], title='Primary', url=None,
                                                    status='review', error=str(error))]
                        self._save(job, planned=True)
                    for target in task['targets']:
                        if target['status'] == 'review' and not target.get('recorded'):
                            target['recorded'] = True
                            self._summarize(job)
                            self._save(job, (item, target, job['kind']))
                        if target['status'] != 'pending':
                            continue
                        if self._cancelled(job):
                            break
                        # Protection can change after matching or while another target uploads.
                        if item['id'] in self.store.values('protected') and not (
                            job['kind'] == 'manual' and item['id'] in job['options'].get('confirmed_protected_ids', [])
                        ):
                            target.update(status='skipped', error='Protected item')
                            continue
                        target['status'] = 'uploading'
                        job.update(phase='applying', message=f"Uploading {target['title']} for {item.get('title', item['id'])}")
                        self._save(job)
                        try:
                            success = self.service.upload(target)
                            target.update(status='success' if success else 'failed', error=None if success else 'Jellyfin rejected the upload')
                        except Exception as error:
                            target.update(status='failed', error=str(error))
                        # Commit the outcome even if cancellation arrived during the upload.
                        self._summarize(job)
                        self._save(job, (item, target, job['kind']))
                    if task.get('selection') and all(target['status'] == 'success' for target in task['targets']):
                        self.store.clear_selection_if_unchanged(item['id'], task['selection'])
                    self._summarize(job)
                    self._save(job)
                    if self._cancelled(job):
                        break
                    self.stop.wait(self.delay)
                self._summarize(job)
                cancelled = self._cancelled(job)
                stopped = 'interrupted' if self.stop.is_set() else 'cancelled'
                job.update(status=stopped if cancelled else 'completed', phase=stopped if cancelled else 'completed',
                           done=True, success=not cancelled and job['failed'] == 0,
                           current_item=None, message='Cancelled; completed uploads are recorded.' if cancelled else
                           f"Finished: {job['successful']} successful, {job['failed']} failed/review, {job['skipped']} skipped.")
                self._save(job)
            except Exception as error:
                self._summarize(job)
                job.update(status='failed', phase='failed', done=True, success=False, error=str(error), message=str(error))
                self._save(job)

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=5)
        if self.worker_lock and (not self.worker or not self.worker.is_alive()):
            self.worker_lock.close()
