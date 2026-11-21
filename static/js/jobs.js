// All upload modes use the same durable queue and progress UI.
let jobQueueLoading = false;
const displayedJobUpdates = new Map();
const checkedJobResults = new Map(); // job_id -> how many of its results were already checked for new artwork
const refreshedArtwork = new Set();
let jobQueueInitialized = false;

async function restoreSelections() {
    const response = await fetch('/selections');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not restore selections');
    const previousIds = Object.keys(selectedPosters);
    selectedPosters = data.selections || {};
    manualQueueIds = new Set(data.queued_item_ids || []);
    manualQueueSelectionIds = new Set(Object.keys(selectedPosters));
    previousIds.filter(id => !selectedPosters[id]).forEach(id => updateItemStatus(id, 'uploaded'));
    document.querySelectorAll('.manual-queue-checkbox').forEach(checkbox => {
        const itemId = checkbox.dataset.itemId;
        checkbox.checked = manualQueueIds.has(itemId);
        checkbox.closest('.item-card-wrapper')?.classList.toggle('manual-queued', checkbox.checked);
    });
    Object.keys(selectedPosters).forEach(id => updateItemStatus(id, 'selected'));
    updateUploadAllButton();
    applyGridFilters();
}

async function confirmProtectedUploads(itemIds) {
    // Refresh before confirmation; the server checks again when the job runs.
    await loadProtectedItems();
    const protectedIds = itemIds.filter(id => protectedItemIds.has(id));
    if (!protectedIds.length) return false;
    const confirmed = await showConfirmDialog({
        title: 'Change protected artwork?',
        message: `Explicitly allow this manual job to change ${protectedIds.length} protected item(s)?`,
        details: 'Automatic jobs and retries will continue to skip these items.',
        confirmText: 'Allow this manual change', variant: 'warning'
    });
    if (!confirmed) throw new Error('Protected artwork was not changed');
    return true;
}

async function enqueuePosterJob(payload) {
    const response = await fetch('/jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not queue job');
    currentAutoBatchJobId = data.job_id;
    saveActiveAutoBatchJob(data.job_id);
    showAlert('Poster job queued. Progress and results are saved.', 'info');
    await loadJobQueue();
    return data;
}

async function refreshGridArtwork(itemId) {
    const wrapper = document.querySelector(`.item-card-wrapper[data-item-id="${cssEscapeValue(itemId)}"]`);
    if (!wrapper) return;
    const response = await fetch(`/item/${encodeURIComponent(itemId)}/artwork`);
    const data = await response.json();
    if (!response.ok) return;
    const container = wrapper.querySelector('.card-img-top-wrapper');
    let image = container?.querySelector('.jellyfin-poster-large');
    if (!container) return;
    if (!image) {
        image = document.createElement('img');
        image.className = 'card-img-top jellyfin-poster-large';
        image.alt = `${wrapper.querySelector('.card-title')?.textContent.trim() || 'Item'} poster`;
        container.prepend(image);
    }
    image.onload = () => {
        image.style.display = '';
        const placeholder = container.querySelector('.jellyfin-poster-placeholder-large');
        if (placeholder) placeholder.style.display = 'none';
    };
    image.src = `/jellyfin-image?url=${encodeURIComponent(data.url + '&refresh=' + Date.now())}`;
}

function renderJobQueue(jobs) {
    const panel = document.getElementById('jobQueuePanel');
    const list = document.getElementById('jobQueueList');
    if (!panel || !list) return;
    panel.hidden = jobs.length === 0;
    const visible = [...jobs.filter(job => !job.done || ['interrupted', 'cancelled', 'failed'].includes(job.status)),
        ...jobs.filter(job => job.done && !['interrupted', 'cancelled', 'failed'].includes(job.status)).slice(0, 10)];
    const revision = JSON.stringify(visible);
    if (list.dataset.revision === revision) return; // Preserve focus between unchanged polls.
    list.dataset.revision = revision;
    list.innerHTML = visible.map(job => `
        <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 border-bottom py-2">
            <div><strong>${escapeHtml(job.kind || 'Poster')} job</strong>
                <span class="badge bg-secondary ms-1">${escapeHtml(job.status)}</span>
                <div class="small text-muted">${escapeHtml(job.message || '')}</div>
            </div>
            <div class="btn-group btn-group-sm">
                ${!job.done ? `<button class="btn btn-outline-danger" data-job-action="cancel" data-job-id="${escapeHtml(job.job_id)}">Cancel</button>` : ''}
                ${job.resumable ? `<button class="btn btn-outline-primary" data-job-action="resume" data-job-id="${escapeHtml(job.job_id)}">Resume unfinished</button>` : ''}
                ${job.result_count ? `<button class="btn btn-outline-secondary" data-job-action="results" data-job-id="${escapeHtml(job.job_id)}">Results</button>` : ''}
            </div>
        </div>`).join('');
    list.querySelectorAll('[data-job-action]').forEach(button => button.addEventListener('click', async () => {
        const job = jobs.find(job => job.job_id === button.dataset.jobId);
        if (button.dataset.jobAction === 'results') {
            showBatchResults(await loadJobResults(job.job_id));
            return;
        }
        const action = button.dataset.jobAction;
        const confirmed = await showConfirmDialog({
            title: action === 'resume' ? 'Resume unfinished uploads?' : 'Cancel job?',
            message: action === 'resume'
                ? 'Continue pending targets without repeating recorded successes. An upload interrupted before its outcome was saved may be attempted again.'
                : 'Stop before the next upload. Completed artwork changes will remain.',
            confirmText: action === 'resume' ? 'Resume' : 'Cancel job', variant: 'warning'
        });
        if (!confirmed) return;
        button.disabled = true;
        try {
            const response = await fetch(`/jobs/${job.job_id}/${action}`, { method: 'POST' });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Job action failed');
            if (action === 'resume') currentAutoBatchJobId = job.job_id;
            await loadJobQueue();
        } catch (error) {
            showAlert(error.message, 'danger');
            button.disabled = false;
        }
    }));
}

async function loadJobResults(jobId, start = 0) {
    const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/results?start=${start}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load job results');
    return data.results || [];
}

// Only results added since the last check are loaded. The last one checked may still have been in progress.
async function refreshNewJobArtwork(job) {
    const start = Math.max((checkedJobResults.get(job.job_id) || 0) - 1, 0);
    const results = await loadJobResults(job.job_id, start);
    checkedJobResults.set(job.job_id, start + results.length);
    for (const result of results) {
        const key = `${job.job_id}:${result.item_id}`;
        if (!result.primary_uploaded || refreshedArtwork.has(key)) continue;
        refreshedArtwork.add(key);
        await refreshGridArtwork(result.item_id);
    }
}

// Called from the page right after the job panel, so jobs and progress show while the grid is still loading.
// The full poll (selections, failed and processed lists) starts once the page has loaded.
function showJobQueueEarly() {
    fetch('/jobs').then(response => (response.ok ? response.json() : null)).then(data => {
        if (!data || jobQueueInitialized) return;
        const jobs = data.jobs || [];
        renderJobQueue(jobs);
        const saved = jobs.find(job => job.job_id === getSavedActiveAutoBatchJob());
        const active = (saved && !saved.done ? saved : null) || jobs.find(job => job.status === 'running') || jobs.find(job => !job.done);
        if (!active) return;
        autoBatchStartedAt = Date.parse(active.created_at);
        setAutoBatchRunning(true);
        updateAutoBatchProgress(active);
    }).catch(error => console.warn('Job progress temporarily unavailable:', error));
}

async function loadJobQueue() {
    if (jobQueueLoading) return;
    jobQueueLoading = true;
    stopAutoBatchPolling();
    let pollAgain = true;
    try {
        const response = await fetch('/jobs');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not load jobs');
        const jobs = data.jobs || [];
        renderJobQueue(jobs);
        let outcomesChanged = false;
        for (const job of jobs) {
            const revision = `${job.status}:${job.result_count}:${job.completed_targets}`;
            if (displayedJobUpdates.get(job.job_id) !== revision) {
                outcomesChanged = true;
                displayedJobUpdates.set(job.job_id, revision);
                if (jobQueueInitialized) await refreshNewJobArtwork(job);
                else checkedJobResults.set(job.job_id, job.result_count || 0);
            }
        }
        jobQueueInitialized = true;
        if (outcomesChanged) {
            await restoreSelections();
            await Promise.all([loadFailedItems(), loadProcessedItems(), loadLatestAutoBatchResults()]);
        }
        const tracked = jobs.find(job => job.job_id === currentAutoBatchJobId);
        const active = (tracked && !tracked.done ? tracked : null) || jobs.find(job => job.status === 'running') || jobs.find(job => !job.done);
        if (active) {
            currentAutoBatchJobId = active.job_id;
            saveActiveAutoBatchJob(active.job_id);
            autoBatchStartedAt = Date.parse(active.created_at);
            setAutoBatchRunning(true);
            updateAutoBatchProgress(active);
        } else {
            if (tracked) updateAutoBatchProgress(tracked);
            currentAutoBatchJobId = null;
            clearActiveAutoBatchJob();
            setAutoBatchRunning(false);
            pollAgain = false;
        }
    } catch (error) {
        // A transient polling error must not forget a still-running durable job.
        console.warn('Job progress temporarily unavailable:', error);
    } finally {
        jobQueueLoading = false;
        autoBatchPollTimer = setTimeout(loadJobQueue, pollAgain ? 1500 : 5000);
    }
}
