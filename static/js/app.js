// Theme helpers (default to dark mode)
function getPreferredTheme() {
    try {
        const saved = localStorage.getItem('jpm_theme');
        if (saved === 'light' || saved === 'dark') return saved;
    } catch (e) {}
    return 'dark'; // default
}

function updateThemeMeta(theme) {
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    const colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
    if (themeMeta) {
        themeMeta.setAttribute('content', theme === 'dark' ? '#0f1115' : '#f5f5f5');
    }
    if (colorSchemeMeta) {
        colorSchemeMeta.setAttribute('content', theme === 'dark' ? 'dark light' : 'light dark');
    }
}

function updateThemeToggle(theme) {
    const icon = document.getElementById('themeToggleIcon');
    const text = document.getElementById('themeToggleText');
    const btn = document.getElementById('themeToggle');
    if (!icon || !text || !btn) return;

    if (theme === 'dark') {
        icon.classList.remove('fa-moon');
        icon.classList.add('fa-sun');
        text.textContent = 'Light';
        btn.setAttribute('aria-label', 'Switch to light mode');
    } else {
        icon.classList.remove('fa-sun');
        icon.classList.add('fa-moon');
        text.textContent = 'Dark';
        btn.setAttribute('aria-label', 'Switch to dark mode');
    }
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('jpm_theme', theme); } catch (e) {}
    updateThemeMeta(theme);
    updateThemeToggle(theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || getPreferredTheme();
    applyTheme(current === 'dark' ? 'light' : 'dark');
}

function initTheme() {
    const theme = document.documentElement.getAttribute('data-theme') || getPreferredTheme();
    applyTheme(theme);

    // Optional: react to OS changes if user hasn't explicitly chosen
    if (!localStorage.getItem('jpm_theme') && window.matchMedia) {
        const mq = window.matchMedia('(prefers-color-scheme: dark)');
        const handler = (e) => applyTheme(e.matches ? 'dark' : 'light');
        if (mq.addEventListener) mq.addEventListener('change', handler);
        else if (mq.addListener) mq.addListener(handler);
    }
}

// Global Variables
let currentItemId = null;
let selectedPosters = {};
let loadingModal = null;
let posterModal = null;
let resultsModal = null;
let failedItemsPanelVisible = false;
let activeFailedItemIds = new Set();
let activeFailedItemDetails = new Map();
let activeProcessedItemDetails = new Map();
let autoBatchPollTimer = null;
let currentAutoBatchJobId = null;
let autoBatchStartedAt = null;
let manualSelectionVisible = false;
let posterSearchProgressTimer = null;

document.addEventListener('DOMContentLoaded', function() {
    // Theme first
    initTheme();
    const themeBtn = document.getElementById('themeToggle');
    if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

    // Modals
    const lm = document.getElementById('loadingModal');
    const pm = document.getElementById('posterModal');
    const rm = document.getElementById('resultsModal');
    if (lm && bootstrap?.Modal) loadingModal = new bootstrap.Modal(lm);
    if (pm && bootstrap?.Modal) posterModal = new bootstrap.Modal(pm);
    if (rm && bootstrap?.Modal) {
        resultsModal = new bootstrap.Modal(rm);
        rm.addEventListener('hidden.bs.modal', () => loadFailedItems({ autoExpand: true }));
    }

    // Initialize counters/buttons
    updateUploadAllButton();
    loadFailedItems();
    loadProcessedItems();

    // If URL has filter param, apply it on load
    const urlParams = new URLSearchParams(window.location.search);
    const currentFilter = urlParams.get('type') || 'all';
    if (currentFilter !== 'all') {
        filterContent(currentFilter);
    }

    console.log('Jellyfin Poster Manager initialized');
});

// Filter and Sort Functions
function filterContent(type) {
    // Normalize to DOM data-type values
    let domType = type;
    if (type === 'movies') domType = 'movie';
    if (type === 'series') domType = 'series';

    const items = document.querySelectorAll('.item-card-wrapper');
    let visibleCount = 0;

    items.forEach(item => {
        const itemType = item.getAttribute('data-type');
        if (type === 'all' || itemType === domType) {
            item.classList.remove('hidden');
            visibleCount++;
        } else {
            item.classList.add('hidden');
        }
    });

    const totalCount = document.getElementById('totalItemCount');
    if (totalCount) totalCount.textContent = visibleCount;

    // Update URL without reload to persist filter
    const url = new URL(window.location);
    if (type === 'all') {
        url.searchParams.delete('type');
    } else {
        url.searchParams.set('type', type); // keep 'movies'/'series'
    }
    window.history.pushState({}, '', url);
    applyProcessedItemMarkers(activeProcessedItemDetails);
    applyFailedItemMarkers(activeFailedItemIds);
}

function sortContent(sortBy) {
    const url = new URL(window.location);
    url.searchParams.set('sort', sortBy);
    window.location.href = url.toString();
}

function startPosterSearchProgress() {
    stopPosterSearchProgress();

    const loadingText = document.getElementById('loadingText');
    const loadingSubtext = document.getElementById('loadingSubtext');
    const startedAt = Date.now();
    const steps = [
        { at: 0, text: 'Searching TPDB for matching entries...' },
        { at: 5, text: 'Checking matching TPDB entries for available posters...' },
        { at: 10, text: 'Trying additional matches if the first result has no posters...' },
        { at: 15, text: 'Converting poster previews for the picker...' },
        { at: 25, text: 'Still working. TPDB responses can take a little while...' }
    ];

    const update = () => {
        const elapsed = Math.floor((Date.now() - startedAt) / 1000);
        const currentStep = [...steps].reverse().find(step => elapsed >= step.at) || steps[0];
        if (loadingText) loadingText.textContent = 'Searching and converting posters...';
        if (loadingSubtext) loadingSubtext.textContent = currentStep.text;
    };

    update();
    posterSearchProgressTimer = setInterval(update, 1000);
}

function stopPosterSearchProgress() {
    if (posterSearchProgressTimer) {
        clearInterval(posterSearchProgressTimer);
        posterSearchProgressTimer = null;
    }

    const loadingSubtext = document.getElementById('loadingSubtext');
    if (loadingSubtext) loadingSubtext.textContent = 'This may take a few moments';
}

// Load posters for item
async function loadPosters(itemId) {
    currentItemId = itemId;
    startPosterSearchProgress();
    if (loadingModal) loadingModal.show();

    try {
        const response = await fetch(`/item/${itemId}/posters`);
        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        if (loadingModal) loadingModal.hide();
        displayPosters(data.item, data.posters);
    } catch (error) {
        console.error('Error loading posters:', error);
        if (loadingModal) loadingModal.hide();
        showAlert('Failed to load posters: ' + error.message, 'danger');
    } finally {
        stopPosterSearchProgress();
    }
}

// Display posters in modal (image-only, no author/download box)
function displayPosters(item, posters) {
    const modalBody = document.getElementById('posterModalBody');
    const modalTitle = document.querySelector('#posterModal .modal-title');
    if (modalTitle) modalTitle.innerHTML = `<i class="fas fa-images me-2"></i>Choose Poster for ${item.title}`;

    if (!posters || posters.length === 0) {
        modalBody.innerHTML = `
            <div class="text-center py-5">
                <i class="fas fa-search fa-3x text-muted mb-3"></i>
                <h5 class="text-muted">No posters found</h5>
                <p class="text-muted">No posters were found for "${item.title}"</p>
            </div>
        `;
    } else {
        let html = `
            <div class="mb-3">
                <h6><i class="fas fa-film me-2"></i>${item.title}</h6>
                <small class="text-muted">${item.year || 'Unknown Year'} • ${item.type}</small>
                <small class="text-muted ms-3">
                    <i class="fas fa-images me-1"></i>
                    Found ${posters.length} poster${posters.length !== 1 ? 's' : ''}
                </small>
            </div>
            <div class="row">
        `;

        posters.forEach((poster, index) => {
            const imageSource = poster.base64 || '';
            html += `
                <div class="col-lg-2 col-md-3 col-sm-4 col-6 mb-3">
                    <div class="card poster-card h-100" data-poster-id="${poster.id}" onclick="selectPoster('${poster.url}', ${poster.id})">
                        <div class="poster-container">
                            ${!poster.base64 ? `
                                <div class="poster-loading d-flex align-items-center justify-content-center">
                                    <div class="text-center">
                                        <i class="fas fa-exclamation-triangle text-warning mb-2"></i>
                                        <br>
                                        <small class="text-muted">Image failed to load</small>
                                    </div>
                                </div>
                            ` : ''}
                            <img src="${imageSource}"
                                class="card-img-top poster-image"
                                alt="Poster ${index + 1}"
                                loading="lazy"
                                style="${!poster.base64 ? 'display: none;' : ''}">
                        </div>
                    </div>
                </div>
            `;
        });

        html += '</div>';
        modalBody.innerHTML = html;
    }

    if (posterModal) posterModal.show();
}

// Select a poster (store selection server-side; no immediate upload)
async function selectPoster(posterUrl, posterId) {
    try {
        // Visual feedback
        document.querySelectorAll('.poster-card').forEach(card => card.classList.remove('selected'));
        const selectedCard = document.querySelector(`[data-poster-id="${posterId}"]`);
        if (selectedCard) selectedCard.classList.add('selected');

        const response = await fetch(`/item/${currentItemId}/select`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ poster_url: posterUrl })
        });

        const data = await response.json();

        if (data.success) {
            selectedPosters[currentItemId] = posterUrl;
            updateItemStatus(currentItemId, 'selected');

            // Close modal after short delay
            setTimeout(() => {
                if (posterModal) posterModal.hide();
            }, 400);

            updateUploadAllButton();
        } else {
            throw new Error(data.error || 'Failed to select poster');
        }
    } catch (error) {
        console.error('Error selecting poster:', error);
        showAlert('Failed to select poster: ' + error.message, 'danger');
    }
}

// Update item status in UI
function updateItemStatus(itemId, status) {
    const statusElement = document.getElementById(`status-${itemId}`);
    const itemCard = document.querySelector(`[data-item-id="${itemId}"]`);

    if (!statusElement || !itemCard) return;

    switch (status) {
        case 'selected':
            statusElement.innerHTML = `
                <span class="badge status-selected">
                    <i class="fas fa-check me-1"></i>Selected
                </span>
                <button class="btn btn-warning btn-sm mt-1 w-100" onclick="uploadPoster('${itemId}')">
                    <i class="fas fa-cloud-upload-alt me-1"></i>Upload Now
                </button>
            `;
            itemCard.classList.add('selected');
            break;

        case 'uploading':
            statusElement.innerHTML = `
                <span class="badge bg-info">
                    <i class="fas fa-spinner fa-spin me-1"></i>Uploading...
                </span>
            `;
            break;

        case 'uploaded':
            statusElement.innerHTML = `
                <span class="badge status-uploaded">
                    <i class="fas fa-check-circle me-1"></i>Uploaded!
                </span>
            `;
            itemCard.classList.remove('selected');
            delete selectedPosters[itemId];
            updateUploadAllButton();
            break;

        case 'error':
            statusElement.innerHTML = `
                <span class="badge status-error">
                    <i class="fas fa-exclamation-triangle me-1"></i>Error
                </span>
                <button class="btn btn-outline-warning btn-sm mt-1 w-100" onclick="uploadPoster('${itemId}')">
                    <i class="fas fa-redo me-1"></i>Retry Upload
                </button>
            `;
            break;
    }
}

// Upload individual selected poster
async function uploadPoster(itemId) {
    updateItemStatus(itemId, 'uploading');

    try {
        const response = await fetch(`/upload/${itemId}`, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            updateItemStatus(itemId, 'uploaded');
            showAlert('Poster uploaded successfully!', 'success');
            // Optional: reload to refresh thumbnails
            setTimeout(() => window.location.reload(), 800);
        } else {
            updateItemStatus(itemId, 'error');
            showAlert('Upload failed: ' + (data.error || 'Unknown error'), 'danger');
        }
    } catch (error) {
        console.error('Error uploading poster:', error);
        updateItemStatus(itemId, 'error');
        showAlert('Upload failed: ' + error.message, 'danger');
    }
}

// Upload all selected posters (batch)
async function uploadAllSelected() {
    const selectedCount = Object.keys(selectedPosters).length;
    if (selectedCount === 0) {
        showAlert('No posters selected', 'warning');
        return;
    }

    if (!confirm(`Upload ${selectedCount} selected poster(s)?`)) {
        return;
    }

    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');

    if (progressContainer) progressContainer.style.display = 'block';
    if (progressBar) progressBar.style.width = '20%';
    if (progressText) progressText.textContent = 'Starting...';

    const uploadBtn = document.getElementById('uploadAllBtn');
    if (uploadBtn) {
        uploadBtn.disabled = true;
        uploadBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Uploading...';
    }

    try {
        const response = await fetch('/upload-all', { method: 'POST' });
        const data = await response.json();

        if (progressBar) progressBar.style.width = '80%';
        if (!data.results) throw new Error(data.error || 'Batch upload failed');

        // Reflect results in UI
        data.results.forEach(result => {
            if (result.success) {
                updateItemStatus(result.item_id, 'uploaded');
            } else {
                updateItemStatus(result.item_id, 'error');
            }
        });

        if (progressBar) progressBar.style.width = '100%';
        if (progressText) progressText.textContent = '100%';

        showBatchResults(data.results);
        loadFailedItems({ autoExpand: true });
        loadProcessedItems();

        // Refresh after short delay to update any thumbnails
        setTimeout(() => {
            if (progressContainer) progressContainer.style.display = 'none';
            window.location.reload();
        }, 1500);

    } catch (error) {
        console.error('Error in batch upload:', error);
        showAlert('Batch upload failed: ' + error.message, 'danger');
        if (progressContainer) progressContainer.style.display = 'none';
    } finally {
        if (uploadBtn) {
            uploadBtn.disabled = false;
            uploadBtn.innerHTML = '<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected';
        }
        updateUploadAllButton();
    }
}

// Show batch upload results
function showBatchResults(results) {
    const modalBody = document.getElementById('resultsModalBody');

    let successCount = results.filter(r => r.success).length;
    let failCount = results.length - successCount;
    const defaultFilter = failCount > 0 ? 'failed' : 'all';

    const html = `
        <div class="row mb-3">
            <div class="col-md-6">
                <div class="card border-success">
                    <div class="card-body text-center">
                        <i class="fas fa-check-circle fa-2x text-success mb-2"></i>
                        <h4 class="text-success">${successCount}</h4>
                        <small class="text-muted">Successful</small>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card border-danger">
                    <div class="card-body text-center">
                        <i class="fas fa-exclamation-circle fa-2x text-danger mb-2"></i>
                        <h4 class="text-danger">${failCount}</h4>
                        <small class="text-muted">Failed</small>
                    </div>
                </div>
            </div>
        </div>
        <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2">
            <h6 class="mb-0">Detailed Results</h6>
            <div class="btn-group btn-group-sm" role="group" aria-label="Filter upload results">
                <button class="btn btn-outline-secondary batch-results-filter" type="button" data-filter="all">
                    All (${results.length})
                </button>
                <button class="btn btn-outline-danger batch-results-filter" type="button" data-filter="failed">
                    Failed (${failCount})
                </button>
                <button class="btn btn-outline-success batch-results-filter" type="button" data-filter="successful">
                    Successful (${successCount})
                </button>
            </div>
        </div>
        <div class="table-responsive">
            <table class="table table-sm results-table">
                <thead>
                    <tr>
                        <th>Item</th>
                        <th>Status</th>
                        <th>Error</th>
                    </tr>
                </thead>
                <tbody id="batchResultsBody"></tbody>
            </table>
        </div>
    `;

    modalBody.innerHTML = html;
    renderBatchResults(results, defaultFilter);

    document.querySelectorAll('.batch-results-filter').forEach(button => {
        button.addEventListener('click', () => renderBatchResults(results, button.getAttribute('data-filter')));
    });

    if (resultsModal) resultsModal.show();
}

function renderBatchResults(results, filter) {
    const tbody = document.getElementById('batchResultsBody');
    if (!tbody) return;

    document.querySelectorAll('.batch-results-filter').forEach(button => {
        const isActive = button.getAttribute('data-filter') === filter;
        button.classList.toggle('active', isActive);
        button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });

    const filteredResults = results.filter(result => {
        if (filter === 'failed') return !result.success;
        if (filter === 'successful') return result.success;
        return true;
    });

    if (filteredResults.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td class="text-muted text-center" colspan="3">No results in this filter.</td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = filteredResults.map(result => `
        <tr>
            <td>${escapeHtml(result.item_title || result.item_id || 'Unknown')}</td>
            <td>
                ${result.success ?
                    '<span class="badge bg-success">Success</span>' :
                    '<span class="badge bg-danger">Failed</span>'
                }
            </td>
            <td>${escapeHtml(result.error || '-')}</td>
        </tr>
    `).join('');
}

// Button enable state
function updateUploadAllButton() {
    const uploadBtn = document.getElementById('uploadAllBtn');
    const selectedCountSpan = document.getElementById('selectedCount');
    const toolbarCount = document.getElementById('manualSelectionToolbarCount');
    const selectedCount = Object.keys(selectedPosters).length;

    if (selectedCountSpan) selectedCountSpan.textContent = selectedCount;
    if (toolbarCount) toolbarCount.textContent = selectedCount;

    if (uploadBtn) {
        if (selectedCount > 0) {
            uploadBtn.disabled = false;
            uploadBtn.innerHTML = `<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected (${selectedCount})`;
        } else {
            uploadBtn.disabled = true;
            uploadBtn.innerHTML = '<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected';
        }
    }

    updateManualSelectionVisibility();
}

function updateManualSelectionVisibility() {
    const manualRow = document.getElementById('manualSelectionRow');
    const toolbarBtn = document.getElementById('manualSelectionToolbarBtn');
    const selectedCount = Object.keys(selectedPosters).length;
    const shouldShow = selectedCount > 0 || manualSelectionVisible;

    if (manualRow) manualRow.style.display = shouldShow ? '' : 'none';
    if (toolbarBtn) {
        toolbarBtn.classList.toggle('active', shouldShow);
        toolbarBtn.setAttribute('aria-expanded', shouldShow ? 'true' : 'false');
    }
}

function toggleManualSelectionPanel() {
    manualSelectionVisible = !manualSelectionVisible;
    updateManualSelectionVisibility();
}

// Notifications
function showAlert(message, type = 'info') {
    const safeMessage = escapeHtml(message);
    const alertHtml = `
        <div class="alert alert-${type} alert-dismissible fade show" role="alert">
            <i class="fas fa-info-circle me-2"></i>
            ${safeMessage}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
    `;
    const container = document.querySelector('.container') || document.body;
    container.insertAdjacentHTML('afterbegin', alertHtml);
    setTimeout(() => {
        const alert = container.querySelector('.alert');
        if (alert) alert.remove();
    }, 5000);
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatOperationLabel(operation) {
    const labels = {
        'auto-poster': 'Auto-Get Posters',
        'manual-upload': 'Manual Upload',
        'batch-upload': 'Batch Upload',
        'direct-upload': 'Direct Upload',
        'retry-auto-poster': 'Retry',
        'retry-all-auto-poster': 'Retry All',
        'poster': 'Poster Lookup'
    };
    return labels[operation] || String(operation || 'Poster Lookup')
        .replace(/-/g, ' ')
        .replace(/\b\w/g, letter => letter.toUpperCase());
}

function formatLogTimestamp(timestamp) {
    if (!timestamp) return 'Unknown time';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString();
}

async function loadFailedItems(options = {}) {
    const failedItemsBody = document.getElementById('failedItemsBody');
    const failedItemsCount = document.getElementById('failedItemsCount');
    const failedItemsPanel = document.getElementById('failedItemsPanel');
    const retryAllBtn = document.getElementById('retryAllFailedBtn');
    const clearBtn = document.getElementById('clearFailedItemsBtn');
    const failedItemsRow = document.getElementById('failedItemsRow');
    if (!failedItemsBody || !failedItemsCount || !failedItemsPanel || !retryAllBtn || !clearBtn || !failedItemsRow) return;

    try {
        const response = await fetch('/failed-items?limit=100');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load failed items');

        const items = data.items || [];
        activeFailedItemIds = new Set(items.map(item => item.item_id).filter(Boolean));
        activeFailedItemDetails = new Map(items.filter(item => item.item_id).map(item => [item.item_id, item]));
        applyProcessedItemMarkers(activeProcessedItemDetails);
        applyFailedItemMarkers(activeFailedItemIds);
        failedItemsCount.textContent = items.length;
        const toolbarCount = document.getElementById('failedItemsToolbarCount');
        if (toolbarCount) toolbarCount.textContent = items.length;
        retryAllBtn.disabled = items.length === 0;
        clearBtn.disabled = items.length === 0;
        failedItemsRow.style.display = items.length === 0 ? 'none' : '';
        if (options.autoExpand && items.length > 0) {
            failedItemsPanelVisible = true;
        }

        if (items.length === 0) {
            failedItemsPanelVisible = false;
            updateFailedItemsPanelVisibility();
            failedItemsBody.innerHTML = '';
            return;
        }

        updateFailedItemsPanelVisibility();
        failedItemsBody.innerHTML = items.map(item => {
            const label = item.item_year ? `${item.item_title || 'Unknown'} (${item.item_year})` : (item.item_title || 'Unknown');
            const canRetry = Boolean(item.item_id);
            const itemLabel = canRetry ? `
                <button class="btn btn-link btn-sm p-0 failed-item-link"
                        type="button"
                        data-item-id="${escapeHtml(item.item_id)}">
                    ${escapeHtml(label)}
                </button>
            ` : escapeHtml(label);
            return `
                <tr>
                    <td>${itemLabel}</td>
                    <td>${escapeHtml(item.item_type || '-')}</td>
                    <td>${escapeHtml(formatOperationLabel(item.operation))}</td>
                    <td>${escapeHtml(item.error || '-')}</td>
                    <td class="text-end">
                        <button class="btn btn-outline-warning btn-sm retry-failed-item-btn"
                                type="button"
                                data-item-id="${escapeHtml(item.item_id || '')}"
                                ${canRetry ? '' : 'disabled'}>
                            <i class="fas fa-rotate-right me-1"></i>Retry
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        document.querySelectorAll('.retry-failed-item-btn').forEach(button => {
            button.addEventListener('click', () => retryFailedItem(button.getAttribute('data-item-id'), button));
        });
        document.querySelectorAll('.failed-item-link').forEach(button => {
            button.addEventListener('click', () => scrollToItemCard(button.getAttribute('data-item-id')));
        });
    } catch (error) {
        console.error('Failed items error:', error);
        showAlert('Failed to load failed items: ' + error.message, 'danger');
    }
}

function updateFailedItemsPanelVisibility() {
    const failedItemsPanel = document.getElementById('failedItemsPanel');
    const failedItemsCount = document.getElementById('failedItemsCount');
    const failedItemsRow = document.getElementById('failedItemsRow');
    const toolbarBtn = document.getElementById('failedItemsToolbarBtn');
    if (!failedItemsPanel || !failedItemsCount || !failedItemsRow) return;

    const hasItems = Number(failedItemsCount.textContent) > 0;
    failedItemsRow.style.display = hasItems && failedItemsPanelVisible ? '' : 'none';
    failedItemsPanel.style.display = hasItems && failedItemsPanelVisible ? 'block' : 'none';
    if (toolbarBtn) {
        toolbarBtn.disabled = !hasItems;
        toolbarBtn.classList.toggle('active', hasItems && failedItemsPanelVisible);
        toolbarBtn.setAttribute('aria-expanded', hasItems && failedItemsPanelVisible ? 'true' : 'false');
    }
}

async function toggleFailedItemsFromToolbar() {
    if (!failedItemsPanelVisible) {
        await loadFailedItems();
    }
    const failedItemsCount = document.getElementById('failedItemsCount');
    if (!failedItemsCount || Number(failedItemsCount.textContent) === 0) return;
    failedItemsPanelVisible = !failedItemsPanelVisible;
    updateFailedItemsPanelVisibility();
}

async function loadProcessedItems() {
    try {
        const response = await fetch('/processed-items?limit=1000');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load processed items');

        const items = data.items || [];
        activeProcessedItemDetails = new Map(items.filter(item => item.item_id).map(item => [item.item_id, item]));
        applyProcessedItemMarkers(activeProcessedItemDetails);
        applyFailedItemMarkers(activeFailedItemIds);
    } catch (error) {
        console.error('Processed items error:', error);
    }
}

function applyProcessedItemMarkers(itemDetails) {
    document.querySelectorAll('.item-card-wrapper').forEach(wrapper => {
        const itemId = wrapper.getAttribute('data-item-id');
        const card = wrapper.querySelector('.item-card');
        const posterWrapper = wrapper.querySelector('.card-img-top-wrapper');
        if (!card || !posterWrapper) return;

        const detail = itemDetails.get(itemId);
        const isProcessed = Boolean(detail) && !activeFailedItemIds.has(itemId);
        card.classList.toggle('processed-item', isProcessed);

        let overlay = wrapper.querySelector('.processed-item-overlay');
        if (isProcessed && !overlay) {
            overlay = document.createElement('div');
            overlay.className = 'processed-item-overlay';
            posterWrapper.insertBefore(overlay, posterWrapper.firstChild);
        }

        if (isProcessed && overlay) {
            overlay.title = `Processed ${formatLogTimestamp(detail.timestamp)}`;
            overlay.innerHTML = '<span class="badge bg-success"><i class="fas fa-check me-1"></i>Processed</span>';
        } else if (!isProcessed && overlay) {
            overlay.remove();
        }
    });
}

function applyFailedItemMarkers(itemIds) {
    document.querySelectorAll('.item-card-wrapper').forEach(wrapper => {
        const itemId = wrapper.getAttribute('data-item-id');
        const card = wrapper.querySelector('.item-card');
        const posterWrapper = wrapper.querySelector('.card-img-top-wrapper');
        if (!card || !posterWrapper) return;

        const isFailed = itemIds.has(itemId);
        const detail = activeFailedItemDetails.get(itemId);
        card.classList.toggle('failed-item', isFailed);

        const processedOverlay = wrapper.querySelector('.processed-item-overlay');
        if (isFailed && processedOverlay) {
            processedOverlay.remove();
            card.classList.remove('processed-item');
        }

        let overlay = wrapper.querySelector('.failed-item-overlay');
        if (isFailed && !overlay) {
            overlay = document.createElement('div');
            overlay.className = 'failed-item-overlay';
            posterWrapper.insertBefore(overlay, posterWrapper.firstChild);
        }

        if (isFailed && overlay) {
            const reason = detail?.error || 'Unknown failure';
            const timestamp = formatLogTimestamp(detail?.timestamp);
            overlay.title = `Failed ${timestamp}\n${reason}`;
            overlay.innerHTML = '<span class="badge bg-danger"><i class="fas fa-triangle-exclamation me-1"></i>Failed</span>';
        } else if (!isFailed && overlay) {
            overlay.remove();
        }
    });
}

function scrollToItemCard(itemId) {
    if (!itemId) return;

    const wrapper = Array.from(document.querySelectorAll('.item-card-wrapper'))
        .find(item => item.getAttribute('data-item-id') === itemId);
    if (!wrapper) {
        showAlert('That item is not currently visible in the library grid.', 'warning');
        return;
    }

    if (wrapper.classList.contains('hidden')) {
        wrapper.classList.remove('hidden');
        showAlert('Showing the failed item even though it is outside the current filter.', 'info');
    }

    wrapper.scrollIntoView({ behavior: 'smooth', block: 'center' });

    const card = wrapper.querySelector('.item-card');
    if (card) {
        card.classList.remove('failed-card-focus');
        void card.offsetWidth;
        card.classList.add('failed-card-focus');
    }
}

async function clearFailedItems() {
    if (!confirm('Clear all failed item entries from failed.log?')) return;

    const clearBtn = document.getElementById('clearFailedItemsBtn');
    const retryAllBtn = document.getElementById('retryAllFailedBtn');
    if (clearBtn) {
        clearBtn.disabled = true;
        clearBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Clearing';
    }

    try {
        const response = await fetch('/failed-items', { method: 'DELETE' });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to clear failed items');

        const failedItemsBody = document.getElementById('failedItemsBody');
        const failedItemsCount = document.getElementById('failedItemsCount');
        const failedItemsPanel = document.getElementById('failedItemsPanel');
        const failedItemsRow = document.getElementById('failedItemsRow');
        const failedToolbarCount = document.getElementById('failedItemsToolbarCount');
        if (failedItemsBody) failedItemsBody.innerHTML = '';
        if (failedItemsCount) failedItemsCount.textContent = '0';
        if (failedToolbarCount) failedToolbarCount.textContent = '0';
        activeFailedItemIds = new Set();
        activeFailedItemDetails = new Map();
        applyFailedItemMarkers(activeFailedItemIds);
        applyProcessedItemMarkers(activeProcessedItemDetails);
        failedItemsPanelVisible = false;
        if (failedItemsRow) failedItemsRow.style.display = 'none';
        if (failedItemsPanel) updateFailedItemsPanelVisibility();
        if (retryAllBtn) retryAllBtn.disabled = true;
        showAlert('Failed items cleared', 'success');
    } catch (error) {
        console.error('Clear failed items error:', error);
        showAlert('Failed to clear failed items: ' + error.message, 'danger');
    } finally {
        if (clearBtn) {
            clearBtn.disabled = false;
            clearBtn.innerHTML = '<i class="fas fa-trash me-1"></i>Clear';
        }
        loadFailedItems();
    }
}

async function retryFailedItem(itemId, button) {
    if (!itemId) return;
    if (button) {
        button.disabled = true;
        button.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Retrying';
    }

    try {
        const response = await fetch('/failed-items/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ item_id: itemId })
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Retry failed');

        showAlert(`Poster retry succeeded for ${data.item_title || itemId}`, 'success');
        loadFailedItems();
        loadProcessedItems();
    } catch (error) {
        console.error('Retry failed item error:', error);
        showAlert('Retry failed: ' + error.message, 'danger');
        if (button) {
            button.disabled = false;
            button.innerHTML = '<i class="fas fa-rotate-right me-1"></i>Retry';
        }
    }
}

async function retryAllFailedItems() {
    const retryAllBtn = document.getElementById('retryAllFailedBtn');
    if (!confirm('Retry poster fetch and upload for all recent failed items?')) return;

    if (retryAllBtn) {
        retryAllBtn.disabled = true;
        retryAllBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Retrying...';
    }

    try {
        const response = await fetch('/failed-items/retry-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ limit: 100 })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Retry all failed');

        showBatchResults(data.results || []);
        loadFailedItems({ autoExpand: true });
        loadProcessedItems();
    } catch (error) {
        console.error('Retry all failed items error:', error);
        showAlert('Retry all failed: ' + error.message, 'danger');
    } finally {
        if (retryAllBtn) {
            retryAllBtn.disabled = false;
            retryAllBtn.innerHTML = '<i class="fas fa-rotate-right me-1"></i>Retry All';
        }
    }
}

function formatPhaseLabel(phase) {
    const labels = {
        starting: 'Starting',
        preparing: 'Preparing',
        loading: 'Loading',
        searching: 'Searching',
        downloading: 'Downloading',
        applying: 'Applying',
        applied: 'Applied',
        failed: 'Failed',
        rate_limited: 'Rate Limited',
        completed: 'Completed'
    };
    return labels[phase] || formatOperationLabel(phase || 'starting');
}

function setAutoBatchRunning(isRunning) {
    const autoBtn = document.getElementById('autoPosterBtn');
    const cancelBtn = document.getElementById('cancelAutoBatchBtn');

    if (autoBtn) {
        autoBtn.disabled = isRunning;
        autoBtn.innerHTML = isRunning ?
            '<i class="fas fa-spinner fa-spin me-1"></i> Running...' :
            '<i class="fas fa-magic me-1"></i> Auto-Get Posters';
    }
    if (cancelBtn) {
        cancelBtn.style.display = isRunning ? 'inline-block' : 'none';
        cancelBtn.disabled = !isRunning;
        cancelBtn.innerHTML = '<i class="fas fa-stop me-1"></i>Cancel';
    }
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return 'Calculating...';
    const rounded = Math.round(seconds);
    const minutes = Math.floor(rounded / 60);
    const remainingSeconds = rounded % 60;
    if (minutes <= 0) return `${remainingSeconds}s`;
    return `${minutes}m ${remainingSeconds}s`;
}

function updateAutoBatchCurrentPoster(url) {
    const img = document.getElementById('autoBatchCurrentPoster');
    const empty = document.getElementById('autoBatchCurrentPosterEmpty');
    if (!img || !empty) return;

    if (!url) {
        img.removeAttribute('src');
        img.style.display = 'none';
        empty.style.display = 'inline-block';
        return;
    }

    img.src = '/jellyfin-image?url=' + encodeURIComponent(url);
    img.style.display = 'block';
    empty.style.display = 'none';
}

function calculateAutoBatchEta(job, processed, remaining) {
    if (!autoBatchStartedAt || processed <= 0 || remaining <= 0 || job.done) {
        return job.done ? 'Done' : 'Calculating...';
    }
    const elapsedSeconds = (Date.now() - autoBatchStartedAt) / 1000;
    return formatDuration((elapsedSeconds / processed) * remaining);
}

function updateAutoBatchProgress(job) {
    const panel = document.getElementById('autoBatchProgressPanel');
    if (!panel || !job) return;

    const total = Number(job.total_items || 0);
    const processed = Number(job.processed || 0);
    const remaining = Number(job.remaining ?? Math.max(total - processed, 0));
    const percent = total > 0 ? Math.round((processed / total) * 100) : 0;

    panel.style.display = 'block';
    document.getElementById('autoBatchProgressStatus').textContent = job.message || 'Running automatic poster batch...';
    document.getElementById('autoBatchCurrentItem').textContent = job.current_item ? `Current item: ${job.current_item}` : 'No item currently processing';
    document.getElementById('autoBatchProgressCounts').textContent = `${processed} / ${total}`;
    document.getElementById('autoBatchProgressBar').style.width = `${percent}%`;
    document.getElementById('autoBatchProgressBar').setAttribute('aria-valuenow', String(percent));
    document.getElementById('autoBatchRemaining').textContent = remaining;
    document.getElementById('autoBatchSuccessful').textContent = job.successful || 0;
    document.getElementById('autoBatchFailed').textContent = job.failed || 0;
    document.getElementById('autoBatchPhase').textContent = formatPhaseLabel(job.phase);
    document.getElementById('autoBatchEta').textContent = calculateAutoBatchEta(job, processed, remaining);
    updateAutoBatchCurrentPoster(job.old_poster_url);
}

function stopAutoBatchPolling() {
    if (autoBatchPollTimer) {
        clearInterval(autoBatchPollTimer);
        autoBatchPollTimer = null;
    }
}

async function pollAutoBatchProgress(jobId) {
    try {
        const response = await fetch(`/batch-auto-poster/progress/${jobId}`);
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to load batch progress');

        const job = data.job;
        updateAutoBatchProgress(job);

        if (job.done) {
            stopAutoBatchPolling();
            setAutoBatchRunning(false);
            currentAutoBatchJobId = null;
            loadFailedItems({ autoExpand: true });
            loadProcessedItems();

            if (job.results && job.results.length > 0) {
                showBatchResults(job.results);
            }
            if (!job.success && job.error) {
                showAlert(job.error, 'danger');
            }
        }
    } catch (error) {
        stopAutoBatchPolling();
        setAutoBatchRunning(false);
        currentAutoBatchJobId = null;
        console.error('Auto-batch progress error:', error);
        showAlert('Failed to update auto-batch progress: ' + error.message, 'danger');
    }
}

async function cancelAutoBatch() {
    if (!currentAutoBatchJobId) return;
    if (!confirm('Cancel the running Auto-Get Posters job?')) return;

    const cancelBtn = document.getElementById('cancelAutoBatchBtn');
    if (cancelBtn) {
        cancelBtn.disabled = true;
        cancelBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Cancelling';
    }

    try {
        const response = await fetch(`/batch-auto-poster/cancel/${currentAutoBatchJobId}`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to cancel batch');
        updateAutoBatchProgress(data.job);
    } catch (error) {
        console.error('Auto-batch cancel error:', error);
        showAlert('Failed to cancel auto-batch: ' + error.message, 'danger');
        if (cancelBtn) {
            cancelBtn.disabled = false;
            cancelBtn.innerHTML = '<i class="fas fa-stop me-1"></i>Cancel';
        }
    }
}

// Start automatic batch poster job
async function startAutoBatchPoster(filter) {
    try {
        if (!filter) filter = 'no-poster';

        const confirmText = {
            'no-poster': 'Automatically find and upload posters for items without posters?',
            'all': 'Automatically find and upload posters for ALL items?',
            'movies': 'Automatically find and upload posters for all Movies?',
            'series': 'Automatically find and upload posters for all Series?'
        }[filter] || 'Start automatic poster upload?';
        const skipProcessed = Boolean(document.getElementById('skipProcessedAutoBatch')?.checked);
        const fullConfirmText = skipProcessed ?
            `${confirmText}\n\nAlready processed items in results.log will be skipped.` :
            confirmText;

        if (!confirm(fullConfirmText)) return;

        stopAutoBatchPolling();
        setAutoBatchRunning(true);
        currentAutoBatchJobId = null;
        autoBatchStartedAt = Date.now();

        updateAutoBatchProgress({
            total_items: 0,
            processed: 0,
            remaining: 0,
            successful: 0,
            failed: 0,
            phase: 'starting',
            message: 'Starting automatic poster batch...',
            current_item: null,
            old_poster_url: null,
            new_poster_url: null
        });

        const resp = await fetch('/batch-auto-poster/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filter, skip_processed: skipProcessed })
        });

        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || 'Automatic batch failed');

        currentAutoBatchJobId = data.job_id;
        await pollAutoBatchProgress(data.job_id);
        autoBatchPollTimer = setInterval(() => pollAutoBatchProgress(data.job_id), 1000);

    } catch (err) {
        console.error('Auto-batch error:', err);
        stopAutoBatchPolling();
        setAutoBatchRunning(false);
        currentAutoBatchJobId = null;
        showAlert('Automatic batch failed: ' + err.message, 'danger');
    }
}
