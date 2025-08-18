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
    if (rm && bootstrap?.Modal) resultsModal = new bootstrap.Modal(rm);

    // Initialize counters/buttons
    updateUploadAllButton();

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
}

function sortContent(sortBy) {
    const url = new URL(window.location);
    url.searchParams.set('sort', sortBy);
    window.location.href = url.toString();
}

// Load posters for item
async function loadPosters(itemId) {
    currentItemId = itemId;
    const lt = document.getElementById('loadingText');
    if (lt) lt.textContent = 'Searching and converting posters...';
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

    let html = `
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
        <h6>Detailed Results:</h6>
        <div class="table-responsive">
            <table class="table table-sm results-table">
                <thead>
                    <tr>
                        <th>Item</th>
                        <th>Status</th>
                        <th>Error</th>
                    </tr>
                </thead>
                <tbody>
    `;

    results.forEach(result => {
        html += `
            <tr>
                <td>${result.item_title || result.item_id}</td>
                <td>
                    ${result.success ?
                        '<span class="badge bg-success">Success</span>' :
                        '<span class="badge bg-danger">Failed</span>'
                    }
                </td>
                <td>${result.error || '-'}</td>
            </tr>
        `;
    });

    html += `
                </tbody>
            </table>
        </div>
    `;

    modalBody.innerHTML = html;
    if (resultsModal) resultsModal.show();
}

// Button enable state
function updateUploadAllButton() {
    const uploadBtn = document.getElementById('uploadAllBtn');
    const selectedCountSpan = document.getElementById('selectedCount');
    const selectedCount = Object.keys(selectedPosters).length;

    if (selectedCountSpan) selectedCountSpan.textContent = selectedCount;

    if (uploadBtn) {
        if (selectedCount > 0) {
            uploadBtn.disabled = false;
            uploadBtn.innerHTML = `<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected (${selectedCount})`;
        } else {
            uploadBtn.disabled = true;
            uploadBtn.innerHTML = '<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected';
        }
    }
}

// Notifications
function showAlert(message, type = 'info') {
    const alertHtml = `
        <div class="alert alert-${type} alert-dismissible fade show" role="alert">
            <i class="fas fa-info-circle me-2"></i>
            ${message}
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

        if (!confirm(confirmText)) return;

        const autoBtn = document.getElementById('autoPosterBtn');
        if (autoBtn) {
            autoBtn.disabled = true;
            autoBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Running...';
        }

        const lt = document.getElementById('loadingText');
        if (lt) lt.textContent = 'Running automatic poster batch...';
        if (loadingModal) loadingModal.show();

        const resp = await fetch('/batch-auto-poster', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filter })
        });

        const data = await resp.json();
        if (loadingModal) loadingModal.hide();

        if (!data.success) {
            showAlert(data.error || 'Automatic batch failed', 'danger');
            return;
        }

        showBatchResults(data.results);

    } catch (err) {
        console.error('Auto-batch error:', err);
        if (loadingModal) loadingModal.hide();
        showAlert('Automatic batch failed: ' + err.message, 'danger');
    } finally {
        const autoBtn = document.getElementById('autoPosterBtn');
        if (autoBtn) {
            autoBtn.disabled = false;
            autoBtn.innerHTML = '<i class="fas fa-magic me-1"></i> Auto-Get Posters';
        }
    }
}
