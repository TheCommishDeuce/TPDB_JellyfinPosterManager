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

function initAutoBatchSeasonSettings() {
    const includeInput = document.getElementById('includeSeasonPostersAutoBatch');
    const replaceInput = document.getElementById('replaceSeasonPostersAutoBatch');
    if (!includeInput || !replaceInput) return;

    const savedIncludeSeasonPosters = localStorage.getItem('jpm_include_season_posters');
    const savedReplaceSeasonPosters = localStorage.getItem('jpm_replace_season_posters');
    includeInput.checked = savedIncludeSeasonPosters === 'true';
    replaceInput.checked = savedReplaceSeasonPosters === null ? true : savedReplaceSeasonPosters === 'true';

    const syncReplaceState = () => {
        replaceInput.disabled = !includeInput.checked;
    };

    includeInput.addEventListener('change', () => {
        syncReplaceState();
        localStorage.setItem('jpm_include_season_posters', includeInput.checked ? 'true' : 'false');
    });
    replaceInput.addEventListener('change', () => {
        localStorage.setItem('jpm_replace_season_posters', replaceInput.checked ? 'true' : 'false');
    });
    syncReplaceState();
}

function initSeriesSeasonCounts() {
    const badges = document.querySelectorAll('[data-season-count-item-id]');
    if (!badges.length) return;

    const loadSeasonCount = async (badge) => {
        if (!badge || badge.dataset.loaded === 'true') return;
        badge.dataset.loaded = 'true';

        try {
            const response = await fetch(`/item/${encodeURIComponent(badge.dataset.seasonCountItemId)}/season-count`);
            const data = await response.json();
            if (!response.ok || data.error || data.season_count === null || data.season_count === undefined) {
                badge.style.display = 'none';
                return;
            }

            const count = Number(data.season_count);
            const text = badge.querySelector('.season-count-text');
            if (text) text.textContent = `${count} season${count === 1 ? '' : 's'}`;
        } catch (error) {
            console.warn('Could not load season count:', error);
            badge.style.display = 'none';
        }
    };

    if (!('IntersectionObserver' in window)) {
        badges.forEach(loadSeasonCount);
        return;
    }

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            observer.unobserve(entry.target);
            loadSeasonCount(entry.target);
        });
    }, { rootMargin: '200px' });

    badges.forEach(badge => observer.observe(badge));
}

// Global Variables
let currentItemId = null;
let selectedPosters = {};
let loadingModal = null;
let posterModal = null;
let seasonPostersModal = null;
let resultsModal = null;
let confirmModal = null;
let failedItemsPanelVisible = false;
let activeFailedItemIds = new Set();
let activeFailedItemDetails = new Map();
let activeProcessedItemDetails = new Map();
let protectedItemIds = new Set();
let autoBatchPollTimer = null;
let currentAutoBatchJobId = null;
let autoBatchStartedAt = null;
let latestAutoBatchJob = null;
let activeGridStatusFilters = new Set();
let manualQueueIds = new Set();
let manualQueueOrder = [];
let manualQueueResults = new Map();
let manualQueueErrors = new Map();
let manualQueuePresentedIds = new Set();
let manualQueueSelectionIds = new Set();
let manualQueueActive = false;
let manualQueueWorkerRunning = false;
let manualQueueCurrentItemId = null;
let posterSearchProgressTimer = null;
let posterSearchGroups = [];
let currentPosterSelection = null;
let currentPosterSearchItem = null;
let currentPosterEligibleSeasons = [];
let posterGroupDisplayMode = 'group';
let currentPosterSetLimit = 3;
let canBrowseMorePosterSets = false;
let loadingPosterSetUrls = new Set();
const AUTO_BATCH_ESTIMATE_MIN_PROCESSED = 10;
const ACTIVE_AUTO_BATCH_JOB_KEY = 'jpm_active_auto_batch_job_id';

document.addEventListener('DOMContentLoaded', function() {
    // Theme first
    initTheme();
    const themeBtn = document.getElementById('themeToggle');
    if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
    initAutoBatchSeasonSettings();
    initSeriesSeasonCounts();

    // Modals
    const lm = document.getElementById('loadingModal');
    const pm = document.getElementById('posterModal');
    const sm = document.getElementById('seasonPostersModal');
    const rm = document.getElementById('resultsModal');
    const cm = document.getElementById('confirmModal');
    if (lm && bootstrap?.Modal) loadingModal = new bootstrap.Modal(lm);
    if (pm && bootstrap?.Modal) {
        posterModal = new bootstrap.Modal(pm);
        pm.addEventListener('hidden.bs.modal', () => {
            if (!manualQueueActive || !manualQueueCurrentItemId) return;
            manualQueuePresentedIds.add(manualQueueCurrentItemId);
            manualQueueCurrentItemId = null;
            setTimeout(showNextManualQueueResult, 150);
        });
    }
    if (sm && bootstrap?.Modal) seasonPostersModal = new bootstrap.Modal(sm);
    if (rm && bootstrap?.Modal) {
        resultsModal = new bootstrap.Modal(rm);
        rm.addEventListener('hidden.bs.modal', () => loadFailedItems({ autoExpand: true }));
    }
    if (cm && bootstrap?.Modal) confirmModal = new bootstrap.Modal(cm);

    // Initialize counters/buttons
    updateUploadAllButton();
    initManualQueueControls();
    initProtectedItemButtons();
    loadProtectedItems();
    loadFailedItems();
    loadProcessedItems();
    loadLatestAutoBatchResults();

    const urlParams = new URLSearchParams(window.location.search);
    const libraryFilter = document.getElementById('libraryFilter');
    if (libraryFilter) libraryFilter.value = urlParams.get('library') || '';
    filterContent(urlParams.get('type') || 'all', false);
    resumeAutoBatchProgressOnLoad();

    console.log('Jellyfin Poster Manager initialized');
});

// Filter and Sort Functions
function filterContent(type, updateUrl = true) {
    const filterId = type === 'movies' ? 'filterMovies' : type === 'series' ? 'filterSeries' : 'filterAll';
    const filterInput = document.getElementById(filterId);
    if (filterInput) filterInput.checked = true;

    const selectedLibrary = updateLibraryFilterOptions(type);
    applyGridFilters(type, selectedLibrary);

    if (!updateUrl) return;

    const url = new URL(window.location);
    if (type === 'all') {
        url.searchParams.delete('type');
    } else {
        url.searchParams.set('type', type); // keep 'movies'/'series'
    }
    if (selectedLibrary) {
        url.searchParams.set('library', selectedLibrary);
    } else {
        url.searchParams.delete('library');
    }
    url.hash = '';
    window.history.pushState({}, '', url);
}

function updateLibraryFilterOptions(type = getCurrentContentFilter()) {
    const libraryFilter = document.getElementById('libraryFilter');
    if (!libraryFilter) return '';

    let domType = type;
    if (type === 'movies') domType = 'movie';
    if (type === 'series') domType = 'series';

    const matchingLibraryIds = new Set(
        Array.from(document.querySelectorAll('.item-card-wrapper'))
            .filter(item => type === 'all' || item.getAttribute('data-type') === domType)
            .map(item => item.getAttribute('data-library-id') || '')
            .filter(Boolean)
    );

    Array.from(libraryFilter.options).forEach(option => {
        if (!option.value) {
            option.textContent = type === 'movies'
                ? 'All Movie Libraries'
                : type === 'series'
                    ? 'All Series Libraries'
                    : 'All Libraries';
            option.hidden = false;
            option.disabled = false;
            return;
        }

        const hasMatchingItems = type === 'all' || matchingLibraryIds.has(option.value);
        option.hidden = !hasMatchingItems;
        option.disabled = !hasMatchingItems;
    });

    if (libraryFilter.value && libraryFilter.selectedOptions[0]?.disabled) {
        libraryFilter.value = '';
    }

    return libraryFilter.value || '';
}

function applyGridFilters(type = getCurrentContentFilter(), selectedLibrary = document.getElementById('libraryFilter')?.value || '') {
    let domType = type;
    if (type === 'movies') domType = 'movie';
    if (type === 'series') domType = 'series';

    const items = document.querySelectorAll('.item-card-wrapper');
    const visibleLibraryIds = new Set();
    let visibleCount = 0;

    items.forEach(item => {
        const itemType = item.getAttribute('data-type');
        const itemLibrary = item.getAttribute('data-library-id') || '';
        const matchesType = type === 'all' || itemType === domType;
        const matchesLibrary = !selectedLibrary || itemLibrary === selectedLibrary;
        const matchesStatus = matchesGridStatusFilters(item);
        const isVisible = matchesType && matchesLibrary && matchesStatus;
        item.classList.toggle('hidden', !isVisible);
        if (isVisible) {
            visibleCount++;
            visibleLibraryIds.add(itemLibrary);
        }
    });

    document.querySelectorAll('.library-group-header').forEach(header => {
        const headerLibrary = header.getAttribute('data-library-id') || '';
        header.classList.toggle('hidden', !visibleLibraryIds.has(headerLibrary));
    });

    const visibleItemCount = document.getElementById('visibleItemCount');
    if (visibleItemCount) visibleItemCount.textContent = visibleCount;

    const itemCountTotalText = document.getElementById('itemCountTotalText');
    const allItemCount = Number(document.getElementById('allItemCount')?.dataset.count || items.length);
    if (itemCountTotalText) {
        itemCountTotalText.innerHTML = type === 'all' && !selectedLibrary && activeGridStatusFilters.size === 0 ? '' : ` of <strong>${allItemCount}</strong>`;
    }

    updateFilterToolbarState();
}

function getCurrentContentFilter() {
    const currentType = document.querySelector('input[name="contentFilter"]:checked')?.id;
    return currentType === 'filterMovies' ? 'movies' : currentType === 'filterSeries' ? 'series' : 'all';
}

function matchesGridStatusFilters(item) {
    if (activeGridStatusFilters.size === 0) return true;

    const itemId = item.getAttribute('data-item-id');
    const card = item.querySelector('.item-card');
    const states = {
        processed: card?.classList.contains('processed-item'),
        failed: card?.classList.contains('failed-item'),
        queued: item.classList.contains('manual-queued') || Boolean(selectedPosters[itemId]) || manualQueueSelectionIds.has(itemId),
        locked: card?.classList.contains('protected-item') || protectedItemIds.has(itemId),
    };

    for (const filter of activeGridStatusFilters) {
        if (states[filter]) return true;
    }
    return false;
}

function setGridStatusFilter(checkbox) {
    if (checkbox.checked) activeGridStatusFilters.add(checkbox.value);
    else activeGridStatusFilters.delete(checkbox.value);
    applyGridFilters();
}

function clearGridStatusFilters() {
    activeGridStatusFilters.clear();
    document.querySelectorAll('.grid-status-filter').forEach(checkbox => {
        checkbox.checked = false;
    });
    applyGridFilters();
}

function updateFilterToolbarState() {
    const filterBtn = document.getElementById('filterDropdownBtn');
    if (filterBtn) filterBtn.classList.toggle('active', activeGridStatusFilters.size > 0);
}

function filterLibrary() {
    filterContent(getCurrentContentFilter());
}

async function showSeasonPosters(itemId) {
    const titleElement = document.getElementById('seasonPostersModalTitle');
    const bodyElement = document.getElementById('seasonPostersModalBody');
    if (!itemId || !bodyElement) return;

    bodyElement.innerHTML = `
        <div class="text-center py-4">
            <div class="spinner-border text-primary mb-3" role="status"></div>
            <div class="text-muted">Loading season posters...</div>
        </div>
    `;
    if (seasonPostersModal) seasonPostersModal.show();

    try {
        const response = await fetch(`/item/${encodeURIComponent(itemId)}/seasons`);
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || 'Failed to load season posters');

        const item = data.item || {};
        const seasons = data.seasons || [];
        if (titleElement) {
            titleElement.innerHTML = `<i class="fas fa-layer-group me-2"></i>${escapeHtml(item.title || 'Season Posters')}`;
        }

        if (!seasons.length) {
            bodyElement.innerHTML = '<div class="text-muted text-center py-4">No eligible seasons found.</div>';
            return;
        }

        bodyElement.innerHTML = `
            <div class="season-poster-grid">
                ${seasons.map(season => renderSeasonPosterCard(season)).join('')}
            </div>
        `;
    } catch (error) {
        console.error('Season posters error:', error);
        bodyElement.innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(error.message)}</div>`;
    }
}

function renderSeasonPosterCard(season) {
    const defaultLabel = season.is_special ? 'Specials' : `Season ${season.number ?? '-'}`;
    const title = season.title || defaultLabel;
    const showDefaultLabel = defaultLabel.trim().toLowerCase() !== title.trim().toLowerCase();
    const posterHtml = season.thumbnail_url
        ? `<img src="/jellyfin-image?url=${encodeURIComponent(season.thumbnail_url)}" alt="${escapeHtml(title)} poster">`
        : '<div class="text-center px-2"><i class="fas fa-image fa-2x mb-2 d-block"></i><small>No poster</small></div>';
    const statusClass = season.has_poster ? 'bg-success' : 'bg-secondary';
    const statusText = season.has_poster ? 'Has poster' : 'Missing poster';
    return `
        <article class="season-poster-card">
            <div class="season-poster-frame">${posterHtml}</div>
            <div class="season-poster-meta">
                <div class="fw-semibold text-truncate" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
                ${showDefaultLabel ? `<small class="text-muted d-block">${escapeHtml(defaultLabel)}</small>` : ''}
                <span class="badge ${statusClass} mt-2">${statusText}</span>
            </div>
        </article>
    `;
}

function sortContent(sortBy) {
    const url = new URL(window.location);
    url.searchParams.set('sort', sortBy);
    window.location.href = url.toString();
}

function startPosterSearchProgress(itemType = '') {
    stopPosterSearchProgress();

    const loadingText = document.getElementById('loadingText');
    const loadingSubtext = document.getElementById('loadingSubtext');
    const startedAt = Date.now();
    const isSeries = itemType === 'Series';
    const steps = isSeries ? [
        { at: 0, text: 'Searching TPDb for matching entries...' },
        { at: 5, text: 'Opening the best match and reading posters...' },
        { at: 10, text: 'Checking linked sets for matching season posters...' },
        { at: 18, text: 'Checking season-specific poster pages...' },
        { at: 25, text: 'Downloading poster previews...' },
        { at: 40, text: 'Still working. TPDb is being checked gently to avoid rate limits...' }
    ] : [
        { at: 0, text: 'Searching TPDb for matching entries...' },
        { at: 5, text: 'Opening the best match and reading movie posters...' },
        { at: 10, text: 'Reading poster details from the result page...' },
        { at: 18, text: 'Downloading poster previews...' },
        { at: 35, text: 'Still working. TPDb is being checked gently to avoid rate limits...' }
    ];

    const update = () => {
        const elapsed = Math.floor((Date.now() - startedAt) / 1000);
        const currentStep = [...steps].reverse().find(step => elapsed >= step.at) || steps[0];
        if (loadingText) loadingText.textContent = 'Searching for posters...';
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

async function fetchPostersForItem(itemId, setLimit = 3) {
    const response = await fetch(`/item/${itemId}/posters?set_limit=${encodeURIComponent(setLimit)}`);
    const data = await response.json();
    if (data.error) throw new Error(data.error);
    return data;
}

// Load posters for item
async function loadPosters(itemId, setLimit = 3) {
    preparePosterSearchForItem(itemId, setLimit);
    const itemType = document.querySelector(`[data-item-id="${cssEscapeValue(itemId)}"]`)?.getAttribute('data-type') === 'series' ? 'Series' : 'Movie';
    startPosterSearchProgress(itemType);
    if (loadingModal) loadingModal.show();

    try {
        const data = await fetchPostersForItem(itemId, setLimit);

        if (loadingModal) loadingModal.hide();
        currentPosterSetLimit = data.poster_set_limit || setLimit;
        canBrowseMorePosterSets = Boolean(data.can_browse_more_sets);
        displayPosters(data.item, data.posters, data.poster_groups || [], data.eligible_seasons || []);
    } catch (error) {
        console.error('Error loading posters:', error);
        if (loadingModal) loadingModal.hide();
        showAlert('Failed to load posters: ' + error.message, 'danger');
    } finally {
        stopPosterSearchProgress();
    }
}

function preparePosterSearchForItem(itemId, setLimit = 3) {
    if (currentPosterSearchItem?.id !== itemId) {
        currentPosterSelection = null;
        posterGroupDisplayMode = 'group';
        loadingPosterSetUrls = new Set();
    }
    currentItemId = itemId;
    currentPosterSetLimit = setLimit;
}

function isCurrentManualQueueModal() {
    return manualQueueActive && manualQueueCurrentItemId === currentItemId;
}

function updatePosterSelectionFooter() {
    const modalFooter = document.getElementById('posterModalFooter');
    const queueBtn = document.getElementById('queuePosterSelectionBtn');
    const uploadBtn = document.getElementById('uploadPosterSelectionBtn');
    const selectionHint = document.getElementById('posterSelectionHint');
    const hasSelection = hasCurrentPosterSelection();
    const fromQueue = isCurrentManualQueueModal();

    if (modalFooter) modalFooter.style.display = '';
    if (queueBtn) {
        queueBtn.disabled = !hasSelection;
        queueBtn.innerHTML = fromQueue
            ? '<i class="fas fa-check me-1"></i>Save Queued Selection'
            : '<i class="fas fa-list-check me-1"></i>Queue Upload';
    }
    if (uploadBtn) {
        uploadBtn.disabled = !hasSelection || fromQueue;
        uploadBtn.style.display = fromQueue ? 'none' : '';
    }
    if (selectionHint) {
        if (fromQueue) {
            selectionHint.textContent = hasSelection
                ? 'Save this selection, then the next queued item will open.'
                : 'Choose a poster for this queued item.';
        } else {
            selectionHint.textContent = hasSelection
                ? 'Queue this poster for later, or upload it now.'
                : 'Choose a poster, then queue it or upload it now.';
        }
    }
}

// Display posters in modal (image-only, no author/download box)
function displayPosters(item, posters, posterGroups = [], eligibleSeasons = []) {
    const modalBody = document.getElementById('posterModalBody');
    const modalTitle = document.querySelector('#posterModal .modal-title');
    const modalFooter = document.getElementById('posterModalFooter');
    const previousItemId = currentPosterSearchItem?.id;
    if (modalTitle) modalTitle.innerHTML = `<i class="fas fa-images me-2"></i>Choose Poster for ${item.title}`;
    if (modalFooter) modalFooter.style.display = '';
    posterSearchGroups = Array.isArray(posterGroups) ? posterGroups : [];
    if (previousItemId !== item.id) {
        currentPosterSelection = null;
        posterGroupDisplayMode = 'group';
        loadingPosterSetUrls = new Set();
    }
    currentPosterSearchItem = item;
    currentPosterEligibleSeasons = Array.isArray(eligibleSeasons) ? eligibleSeasons : [];
    updatePosterSelectionFooter();

    if (item.type === 'Series' && posterSearchGroups.length > 0) {
        displayPosterGroups(item, posterSearchGroups, currentPosterEligibleSeasons);
        return;
    }

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
                        ${renderSinglePosterMetadata(poster)}
                    </div>
                </div>
            `;
        });

        html += '</div>';
        modalBody.innerHTML = html;
    }

    if (posterModal) posterModal.show();
}

function renderSinglePosterMetadata(poster) {
    const uploader = poster.uploader || 'Unknown uploader';
    const setCount = poster.set_poster_count;
    const setLink = poster.set_url
        ? `<a href="${escapeHtml(poster.set_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">TPDb Set</a>`
        : '';
    if (!uploader && !setCount && !setLink) return '';

    return `
        <div class="poster-card-meta">
            <div class="text-truncate" title="${escapeHtml(uploader)}">
                <i class="fas fa-user me-1"></i>${escapeHtml(uploader)}
            </div>
            ${(setCount || setLink) ? `
                <small class="text-muted">
                    ${setCount ? `<i class="fas fa-images me-1"></i>${escapeHtml(setCount)} poster${String(setCount) === '1' ? '' : 's'}` : ''}
                    ${setCount && setLink ? ' &bull; ' : ''}
                    ${setLink}
                </small>
            ` : ''}
        </div>
    `;
}

function displayPosterGroups(item, groups, eligibleSeasons) {
    const modalBody = document.getElementById('posterModalBody');
    updatePosterSelectionFooter();
    let html = `
        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-3">
            <div>
                <h6><i class="fas fa-film me-2"></i>${escapeHtml(item.title)}</h6>
                <small class="text-muted">${escapeHtml(item.year || 'Unknown Year')} &bull; ${escapeHtml(item.type)}</small>
                <small class="text-muted ms-3">
                    <i class="fas fa-layer-group me-1"></i>
                    Found ${groups.length} TPDb result${groups.length !== 1 ? 's' : ''}
                </small>
            </div>
            <div class="btn-group btn-group-sm" role="group" aria-label="Poster display mode">
                <button type="button" class="btn btn-outline-secondary poster-group-view-btn ${posterGroupDisplayMode === 'target' ? 'active' : ''}" data-mode="target">
                    By Target
                </button>
                <button type="button" class="btn btn-outline-secondary poster-group-view-btn ${posterGroupDisplayMode === 'group' ? 'active' : ''}" data-mode="group">
                    By Set
                </button>
            </div>
        </div>
    `;

    html += posterGroupDisplayMode === 'group'
        ? renderPosterGroupsByGroup(groups, eligibleSeasons)
        : renderPosterGroupsByTarget(groups, eligibleSeasons);

    modalBody.innerHTML = html;
    modalBody.querySelectorAll('.poster-group-view-btn').forEach(button => {
        button.addEventListener('click', () => setPosterGroupDisplayMode(button.dataset.mode));
    });
    modalBody.querySelectorAll('.load-poster-set-btn').forEach(button => {
        button.addEventListener('click', () => loadPosterSet(button.dataset.setUrl));
    });
    modalBody.querySelectorAll('.select-poster-group-btn').forEach(button => {
        button.addEventListener('click', () => selectPosterGroup(button.dataset.groupId));
    });
    modalBody.querySelectorAll('.select-poster-set-btn').forEach(button => {
        button.addEventListener('click', () => selectPosterSet(button.dataset.groupId, Number(button.dataset.setIndex || 0), button.dataset.setId || null));
    });
    modalBody.querySelectorAll('.grouped-poster-option').forEach(card => {
        card.addEventListener('click', () => {
            if (card.dataset.targetType === 'season') {
                selectGroupedSeasonPoster(card.dataset.groupId, card.dataset.seasonId, card.dataset.posterId);
            } else {
                selectGroupedShowPoster(card.dataset.groupId, card.dataset.posterId);
            }
        });
    });

    if (posterModal) posterModal.show();
}

function renderPosterGroupsByGroup(groups, eligibleSeasons) {
    let groupNumber = 1;
    return groups.map((group) => {
        const showPosters = group.show_posters || [];
        const seasonLists = (eligibleSeasons || []).map(season => ({
            season,
            posters: (group.season_posters || []).filter(poster => poster.season_id === season.id)
        }));
        const allPosters = [
            ...showPosters,
            ...seasonLists.flatMap(entry => entry.posters)
        ];
        const setIds = [...new Set(allPosters.map(poster => poster.set_id).filter(Boolean))];
        let html = '';
        if (setIds.length) {
            html += setIds.map(setId => {
                const displayGroupNumber = groupNumber++;
                const setPosters = [];
                const showPoster = showPosters.find(poster => poster.set_id === setId);
                if (showPoster) {
                    setPosters.push({ poster: showPoster, targetType: 'show' });
                }
                seasonLists.forEach(entry => {
                    const poster = entry.posters.find(currentPoster => currentPoster.set_id === setId);
                    if (poster) {
                        setPosters.push({ poster, targetType: 'season' });
                    }
                });

                return renderPosterSetSection(group, setPosters, displayGroupNumber, null, setId);
            }).join('');
            html += renderUnloadedPosterSets(group, setIds);
            return html;
        }

        const setCount = Math.max(
            showPosters.length,
            ...seasonLists.map(entry => entry.posters.length),
            0
        );

        if (!setCount) return renderUnloadedPosterSets(group, setIds);

        html += Array.from({ length: setCount }, (_, setIndex) => {
            const displayGroupNumber = groupNumber++;
            const setPosters = [];
            if (showPosters[setIndex]) {
                setPosters.push({ poster: showPosters[setIndex], targetType: 'show' });
            }
            seasonLists.forEach(entry => {
                if (entry.posters[setIndex]) {
                    setPosters.push({ poster: entry.posters[setIndex], targetType: 'season' });
                }
            });

            return renderPosterSetSection(group, setPosters, displayGroupNumber, setIndex, null);
        }).join('');
        html += renderUnloadedPosterSets(group, setIds);
        return html;
    }).join('');
}

function renderUnloadedPosterSets(group, loadedSetIds = []) {
    const availableSets = group.available_sets || [];
    const loadedIds = new Set((loadedSetIds || []).map(String));
    const unloadedSets = availableSets.filter(setInfo => {
        if (!setInfo?.set_url) return false;
        return !loadedIds.has(String(setInfo.set_id || ''));
    });
    if (!unloadedSets.length) return '';

    return `
        <section class="poster-group poster-set-browser mb-4">
            <div class="poster-group-header mb-3">
                <h6 class="mb-1">More TPDb Sets</h6>
                <small class="text-muted">Load individual sets when you want to preview them.</small>
            </div>
            <div class="poster-set-browser-list">
                ${unloadedSets.map(setInfo => {
                    const isLoading = loadingPosterSetUrls.has(setInfo.set_url);
                    return `
                        <div class="poster-set-browser-row d-flex flex-wrap justify-content-between align-items-center gap-2">
                            <div>
                                <div class="fw-semibold">${escapeHtml(setInfo.uploader || 'Unknown uploader')}</div>
                                <small class="text-muted">
                                    ${escapeHtml(setInfo.set_poster_count || '?')} poster${String(setInfo.set_poster_count || '') === '1' ? '' : 's'}
                                    &bull;
                                    <a href="${escapeHtml(setInfo.set_url)}" target="_blank" rel="noopener">TPDb Set</a>
                                </small>
                            </div>
                            <button type="button" class="btn btn-sm btn-outline-primary load-poster-set-btn"
                                data-set-url="${escapeHtml(setInfo.set_url)}"
                                ${isLoading ? 'disabled' : ''}>
                                <i class="fas ${isLoading ? 'fa-spinner fa-spin' : 'fa-plus'} me-1"></i>${isLoading ? 'Loading' : 'Load set'}
                            </button>
                        </div>
                    `;
                }).join('')}
            </div>
        </section>
    `;
}

function renderPosterSetSection(group, setPosters, displayGroupNumber, setIndex = null, setId = null) {
    const posters = setPosters.map(entry => entry.poster);
    const metadata = getPosterSetMetadata(group, posters, setId);
    const coverageText = getPosterSetCoverageText(setPosters, group);
    let html = `
        <section class="poster-group poster-set-group mb-4">
            <div class="poster-group-header d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                <div>
                    <small class="text-muted">
                        <i class="fas fa-user me-1"></i>${escapeHtml(metadata.uploader)}
                        ${metadata.setPosterCount ? ` &bull; <i class="fas fa-images me-1"></i>${escapeHtml(metadata.setPosterCount)} poster${String(metadata.setPosterCount) === '1' ? '' : 's'}` : ''}
                        ${coverageText ? ` &bull; <i class="fas fa-layer-group me-1"></i>${escapeHtml(coverageText)}` : ''}
                        ${metadata.setUrl ? ` &bull; <a href="${escapeHtml(metadata.setUrl)}" target="_blank" rel="noopener">TPDb Set</a>` : ` &bull; ${escapeHtml(group.title || 'TPDb result')}`}
                    </small>
                </div>
                <button type="button" class="btn btn-sm btn-outline-success select-poster-set-btn"
                    data-group-id="${escapeHtml(group.id)}"
                    data-set-index="${setIndex ?? ''}"
                    data-set-id="${escapeHtml(setId || '')}">
                    <i class="fas fa-check-double me-1"></i>Select Set
                </button>
            </div>
            <div class="row">
    `;

    setPosters.forEach((entry, posterIndex) => {
        html += renderGroupedPosterCard(entry.poster, posterIndex, entry.targetType, group.id);
    });

    return `${html}</div></section>`;
}

function renderPosterGroupsByTarget(groups, eligibleSeasons) {
    let html = renderPosterTargetSection(
        'Series poster',
        groups.flatMap(group => group.show_posters || []),
        'show'
    );

    (eligibleSeasons || []).forEach(season => {
        const posters = groups.flatMap(group => (group.season_posters || []).filter(poster => poster.season_id === season.id));
        if (posters.length) {
            html += renderPosterTargetSection(season.title || 'Season', posters, 'season');
        }
    });

    if (!groups.some(group => (group.season_posters || []).length > 0)) {
        html += `
            <div class="alert alert-info">
                No season posters were detected for the eligible seasons.
            </div>
        `;
    }

    return html;
}

function renderPosterTargetSection(title, posters, targetType) {
    if (!posters.length) return '';
    const groups = new Map();
    posters.forEach(poster => {
        const key = poster.group_id || poster.id;
        if (!groups.has(key)) groups.set(key, poster);
    });
    const metadataText = Array.from(groups.values()).slice(0, 3).map(poster => {
        const sourceLink = poster.set_url
            ? `<a href="${escapeHtml(poster.set_url)}" target="_blank" rel="noopener">TPDb Set</a>`
            : '';
        return `${escapeHtml(poster.uploader || 'Unknown uploader')}${poster.set_poster_count ? `, ${escapeHtml(poster.set_poster_count)} poster${String(poster.set_poster_count) === '1' ? '' : 's'}` : ''}${sourceLink ? `, ${sourceLink}` : ''}`;
    }).join(' &bull; ');
    let html = `
        <section class="poster-group poster-target-section mb-4">
            <div class="poster-group-header mb-3">
                <h6 class="mb-1">${escapeHtml(title)}</h6>
                <small class="text-muted">${metadataText || `${posters.length} poster${posters.length === 1 ? '' : 's'}`}</small>
            </div>
            <div class="row">
    `;
    posters.forEach((poster, index) => {
        html += renderGroupedPosterCard(poster, index, targetType, poster.group_id);
    });
    return `${html}</div></section>`;
}

function getPosterSetUploader(posters) {
    const posterWithUploader = posters.find(poster => poster.uploader && poster.uploader !== 'Unknown');
    return posterWithUploader?.uploader || '';
}

function getPosterSetMetadata(group, posters, setId = null) {
    const matchingAvailableSet = (group.available_sets || []).find(setInfo => {
        return setId && String(setInfo.set_id || '') === String(setId);
    });
    const posterWithSet = posters.find(poster => poster.set_url || poster.set_poster_count || poster.uploader);
    return {
        uploader: getPosterSetUploader(posters) || matchingAvailableSet?.uploader || 'Unknown uploader',
        setPosterCount: posterWithSet?.set_poster_count || matchingAvailableSet?.set_poster_count || '',
        setUrl: posterWithSet?.set_url || matchingAvailableSet?.set_url || '',
    };
}

function getPosterSetCoverageText(setPosters, group) {
    const seasonTargets = new Set(
        setPosters
            .filter(entry => entry.targetType === 'season' && entry.poster?.season_id)
            .map(entry => entry.poster.season_id)
    );
    const eligibleCount = Number(group.eligible_season_count || currentPosterEligibleSeasons.length || 0);
    if (!eligibleCount) return '';
    return `${seasonTargets.size}/${eligibleCount} seasons`;
}

function setPosterGroupDisplayMode(mode) {
    posterGroupDisplayMode = mode === 'group' ? 'group' : 'target';
    displayPosterGroups(currentPosterSearchItem, posterSearchGroups, currentPosterEligibleSeasons);
    applyGroupedSelectionHighlight();
}

async function loadPosterSet(setUrl) {
    if (!currentItemId || !setUrl || loadingPosterSetUrls.has(setUrl)) return;
    loadingPosterSetUrls.add(setUrl);
    displayPosterGroups(currentPosterSearchItem, posterSearchGroups, currentPosterEligibleSeasons);

    try {
        const response = await fetch(`/item/${currentItemId}/posters?set_limit=${encodeURIComponent(currentPosterSetLimit)}&set_url=${encodeURIComponent(setUrl)}`);
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        mergePosterGroups(data.poster_groups || []);
        canBrowseMorePosterSets = Boolean(data.can_browse_more_sets);
        displayPosterGroups(currentPosterSearchItem, posterSearchGroups, currentPosterEligibleSeasons);
        applyGroupedSelectionHighlight();
    } catch (error) {
        console.error('Error loading TPDb set:', error);
        showAlert('Failed to load TPDb set: ' + error.message, 'danger');
        displayPosterGroups(currentPosterSearchItem, posterSearchGroups, currentPosterEligibleSeasons);
    } finally {
        loadingPosterSetUrls.delete(setUrl);
        displayPosterGroups(currentPosterSearchItem, posterSearchGroups, currentPosterEligibleSeasons);
        applyGroupedSelectionHighlight();
    }
}

function mergePosterGroups(newGroups) {
    const nextPosterId = getHighestPosterId(posterSearchGroups) + 1;
    reassignPosterIds(newGroups || [], nextPosterId);

    (newGroups || []).forEach(newGroup => {
        const existingGroup = findPosterGroup(newGroup.id);
        if (!existingGroup) {
            posterSearchGroups.push(newGroup);
            return;
        }

        existingGroup.show_posters = mergePostersByUrl(existingGroup.show_posters || [], newGroup.show_posters || []);
        existingGroup.season_posters = mergePostersByUrl(existingGroup.season_posters || [], newGroup.season_posters || []);
        existingGroup.available_sets = mergeSetsByUrl(existingGroup.available_sets || [], newGroup.available_sets || []);
        existingGroup.covered_season_count = Math.max(existingGroup.covered_season_count || 0, newGroup.covered_season_count || 0);
        existingGroup.covered_season_keys = [...new Set([...(existingGroup.covered_season_keys || []), ...(newGroup.covered_season_keys || [])])];
    });
}

function getHighestPosterId(groups) {
    let highestId = 0;
    (groups || []).forEach(group => {
        [...(group.show_posters || []), ...(group.season_posters || [])].forEach(poster => {
            const numericId = Number(poster?.id);
            if (Number.isFinite(numericId)) highestId = Math.max(highestId, numericId);
        });
    });
    return highestId;
}

function reassignPosterIds(groups, nextPosterId) {
    (groups || []).forEach(group => {
        [...(group.show_posters || []), ...(group.season_posters || [])].forEach(poster => {
            poster.id = nextPosterId;
            nextPosterId += 1;
        });
    });
    return nextPosterId;
}

function mergePostersByUrl(existingPosters, newPosters) {
    const seenUrls = new Set(existingPosters.map(poster => poster.url));
    return [
        ...existingPosters,
        ...newPosters.filter(poster => {
            if (!poster?.url || seenUrls.has(poster.url)) return false;
            seenUrls.add(poster.url);
            return true;
        })
    ];
}

function mergeSetsByUrl(existingSets, newSets) {
    const seenUrls = new Set(existingSets.map(setInfo => setInfo.set_url));
    return [
        ...existingSets,
        ...newSets.filter(setInfo => {
            if (!setInfo?.set_url || seenUrls.has(setInfo.set_url)) return false;
            seenUrls.add(setInfo.set_url);
            return true;
        })
    ];
}

function renderGroupedPosterCard(poster, index, targetType, groupId) {
    const imageSource = poster.base64 || '';
    const label = targetType === 'season' ? (poster.season_title || 'Season') : 'Series';
    return `
        <div class="col-lg-2 col-md-3 col-sm-4 col-6 mb-3">
            <div class="card poster-card grouped-poster-option h-100"
                data-poster-id="${escapeHtml(poster.id)}"
                data-group-id="${escapeHtml(groupId)}"
                data-target-type="${escapeHtml(targetType)}"
                data-season-id="${escapeHtml(poster.season_id || '')}">
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
                        alt="${escapeHtml(label)} poster ${index + 1}"
                        loading="lazy"
                        style="${!poster.base64 ? 'display: none;' : ''}">
                </div>
                <div class="poster-target-label">${escapeHtml(label)}</div>
            </div>
        </div>
    `;
}

function findPosterGroup(groupId) {
    return posterSearchGroups.find(group => String(group.id) === String(groupId));
}

function findPosterInGroup(group, posterId, targetType) {
    const posters = targetType === 'season' ? group.season_posters || [] : group.show_posters || [];
    return posters.find(poster => String(poster.id) === String(posterId));
}

function createEmptySeriesPosterSelection() {
    return {
        type: 'series_group',
        series_poster_url: null,
        season_posters: {}
    };
}

function hasCurrentPosterSelection() {
    if (typeof currentPosterSelection === 'string') return currentPosterSelection.length > 0;
    return Boolean(
        currentPosterSelection?.series_poster_url ||
        Object.keys(currentPosterSelection?.season_posters || {}).length > 0
    );
}

function buildSelectionFromGroup(group) {
    const showPosters = group.show_posters || [];
    const selection = {
        type: 'series_group',
        series_poster_url: showPosters[0]?.url || null,
        season_posters: {}
    };

    (group.season_posters || []).forEach(poster => {
        if (!poster.season_id || selection.season_posters[poster.season_id]) return;
        selection.season_posters[poster.season_id] = {
            url: poster.url,
            title: poster.season_title || 'Season'
        };
    });

    return selection;
}

function buildSelectionFromPosterSet(group, setIndex, setId = null) {
    const showPosters = group.show_posters || [];
    if (setId) {
        const showPoster = showPosters.find(poster => poster.set_id === setId);
        const selection = {
            type: 'series_group',
            series_poster_url: showPoster?.url || null,
            season_posters: {}
        };

        (currentPosterEligibleSeasons || []).forEach(season => {
            const poster = (group.season_posters || []).find(currentPoster => (
                currentPoster.season_id === season.id && currentPoster.set_id === setId
            ));
            if (!poster?.season_id) return;
            selection.season_posters[poster.season_id] = {
                url: poster.url,
                title: poster.season_title || season.title || 'Season'
            };
        });

        return selection;
    }

    const selection = {
        type: 'series_group',
        series_poster_url: showPosters[setIndex]?.url || null,
        season_posters: {}
    };

    (currentPosterEligibleSeasons || []).forEach(season => {
        const seasonPosters = (group.season_posters || []).filter(poster => poster.season_id === season.id);
        const poster = seasonPosters[setIndex];
        if (!poster?.season_id) return;
        selection.season_posters[poster.season_id] = {
            url: poster.url,
            title: poster.season_title || season.title || 'Season'
        };
    });

    return selection;
}

async function saveCurrentPosterSelection(options = {}) {
    if (!hasCurrentPosterSelection()) {
        await clearCurrentPosterSelection();
        return;
    }

    const body = typeof currentPosterSelection === 'string'
        ? { poster_url: currentPosterSelection }
        : { selection: currentPosterSelection };
    const response = await fetch(`/item/${currentItemId}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!data.success) throw new Error(data.error || 'Failed to select poster');

    selectedPosters[currentItemId] = currentPosterSelection;
    if (options.fromQueue) {
        manualQueueSelectionIds.add(currentItemId);
    } else {
        manualQueueSelectionIds.delete(currentItemId);
    }
    updateItemStatus(currentItemId, 'selected');
    updateUploadAllButton();
    updatePosterSelectionFooter();
}

async function clearCurrentPosterSelection() {
    await clearPosterSelectionForItem(currentItemId);

    currentPosterSelection = null;
    updatePosterSelectionFooter();
}

async function clearPosterSelectionForItem(itemId) {
    const response = await fetch(`/item/${itemId}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clear_selection: true })
    });
    const data = await response.json();
    if (!data.success) throw new Error(data.error || 'Failed to clear poster selection');

    delete selectedPosters[itemId];
    manualQueueSelectionIds.delete(itemId);
    const statusElement = document.getElementById(`status-${itemId}`);
    const itemCard = document.querySelector(`[data-item-id="${cssEscapeValue(itemId)}"]`);
    if (statusElement) statusElement.innerHTML = '';
    if (itemCard) itemCard.classList.remove('selected');
    setManualQueueUploadState(itemId, false);
    updateUploadAllButton();
}

async function selectPosterGroup(groupId) {
    const group = findPosterGroup(groupId);
    if (!group) return;

    try {
        currentPosterSelection = buildSelectionFromGroup(group);
        applyGroupedSelectionHighlight();
        updatePosterSelectionFooter();
    } catch (error) {
        console.error('Error selecting poster set:', error);
        showAlert('Failed to select poster set: ' + error.message, 'danger');
    }
}

async function selectPosterSet(groupId, setIndex, setId = null) {
    const group = findPosterGroup(groupId);
    if (!group) return;

    try {
        currentPosterSelection = buildSelectionFromPosterSet(group, setIndex, setId);
        applyGroupedSelectionHighlight();
        updatePosterSelectionFooter();
    } catch (error) {
        console.error('Error selecting poster set:', error);
        showAlert('Failed to select poster set: ' + error.message, 'danger');
    }
}

async function selectGroupedShowPoster(groupId, posterId) {
    const group = findPosterGroup(groupId);
    const poster = group ? findPosterInGroup(group, posterId, 'show') : null;
    if (!group || !poster) return;

    try {
        currentPosterSelection = currentPosterSelection || createEmptySeriesPosterSelection();
        currentPosterSelection.series_poster_url =
            currentPosterSelection.series_poster_url === poster.url ? null : poster.url;
        applyGroupedSelectionHighlight();
        updatePosterSelectionFooter();
    } catch (error) {
        console.error('Error selecting series poster:', error);
        showAlert('Failed to select series poster: ' + error.message, 'danger');
    }
}

async function selectGroupedSeasonPoster(groupId, seasonId, posterId) {
    const group = findPosterGroup(groupId);
    const poster = group ? findPosterInGroup(group, posterId, 'season') : null;
    if (!group || !poster || !seasonId) return;

    try {
        currentPosterSelection = currentPosterSelection || createEmptySeriesPosterSelection();
        if (currentPosterSelection.season_posters[seasonId]?.url === poster.url) {
            delete currentPosterSelection.season_posters[seasonId];
        } else {
            currentPosterSelection.season_posters[seasonId] = {
                url: poster.url,
                title: poster.season_title || 'Season'
            };
        }
        applyGroupedSelectionHighlight();
        updatePosterSelectionFooter();
    } catch (error) {
        console.error('Error selecting season poster:', error);
        showAlert('Failed to select season poster: ' + error.message, 'danger');
    }
}

function applyGroupedSelectionHighlight() {
    document.querySelectorAll('.grouped-poster-option').forEach(card => {
        const group = findPosterGroup(card.dataset.groupId);
        const poster = group ? findPosterInGroup(group, card.dataset.posterId, card.dataset.targetType) : null;
        let selected = false;
        if (poster && card.dataset.targetType === 'show') {
            selected = currentPosterSelection?.series_poster_url === poster.url;
        } else if (poster && card.dataset.targetType === 'season') {
            selected = currentPosterSelection?.season_posters?.[poster.season_id]?.url === poster.url;
        }
        card.classList.toggle('selected', selected);
    });
}

function setManualQueueUploadState(itemId, queuedForUpload) {
    const checkbox = document.querySelector(`.manual-queue-checkbox[data-item-id="${cssEscapeValue(itemId)}"]`);
    const label = checkbox?.nextElementSibling;
    if (!label?.classList.contains('manual-queue-label')) return;

    label.classList.toggle('queued-for-upload', queuedForUpload);
    label.innerHTML = queuedForUpload
        ? '<i class="fas fa-clock me-1"></i>Queued for Upload'
        : '<i class="fas fa-list-check me-1"></i>Queue';
    applyGridFilters();
}

async function queueCurrentPosterSelection() {
    if (!currentPosterSelection) {
        showAlert('Choose a poster before saving.', 'warning');
        return;
    }
    try {
        await saveCurrentPosterSelection({ fromQueue: true });
        showAlert(isCurrentManualQueueModal() ? 'Queued selection saved' : 'Poster queued for upload', 'success');
        if (posterModal) posterModal.hide();
    } catch (error) {
        console.error('Error queueing poster selection:', error);
        showAlert('Failed to queue poster: ' + error.message, 'danger');
    }
}

async function uploadCurrentPosterSelection() {
    if (!currentPosterSelection) {
        showAlert('Choose a poster before uploading.', 'warning');
        return;
    }
    try {
        await saveCurrentPosterSelection({ fromQueue: false });
        if (posterModal) posterModal.hide();
        await uploadPoster(currentItemId);
    } catch (error) {
        console.error('Error uploading poster selection:', error);
        showAlert('Failed to upload poster: ' + error.message, 'danger');
    }
}

function finishPosterSelection() {
    if (posterModal) posterModal.hide();
}

// Select a poster in the modal; footer buttons decide queue vs upload.
async function selectPoster(posterUrl, posterId) {
    try {
        // Visual feedback
        document.querySelectorAll('.poster-card').forEach(card => card.classList.remove('selected'));
        const selectedCard = document.querySelector(`[data-poster-id="${posterId}"]`);
        if (selectedCard) selectedCard.classList.add('selected');

        currentPosterSelection = posterUrl;
        updatePosterSelectionFooter();
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
            statusElement.innerHTML = '';
            itemCard.classList.add('selected');
            setManualQueueUploadState(itemId, true);
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
            manualQueueSelectionIds.delete(itemId);
            setManualQueueUploadState(itemId, false);
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

    const confirmed = await showConfirmDialog({
        title: 'Upload selected posters?',
        message: `Upload ${selectedCount} selected poster(s)?`,
        confirmText: 'Upload',
        variant: 'primary'
    });
    if (!confirmed) return;

    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');

    if (progressContainer) progressContainer.style.display = 'block';
    if (progressBar) progressBar.style.width = '20%';
    if (progressText) progressText.textContent = 'Starting...';

    const uploadBtn = document.getElementById('uploadAllBtn');
    if (uploadBtn) {
        uploadBtn.disabled = true;
        uploadBtn.classList.add('is-expanded');
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
            uploadBtn.classList.remove('is-expanded');
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

function updateLastResultsButton() {
    const button = document.getElementById('lastAutoBatchResultsBtn');
    if (!button) return;

    const hasResults = Boolean(latestAutoBatchJob?.results?.length);
    button.disabled = !hasResults;
}

async function loadLatestAutoBatchResults() {
    try {
        const response = await fetch('/batch-auto-poster/latest-results');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to load latest results');

        latestAutoBatchJob = data.job || null;
        updateLastResultsButton();
    } catch (error) {
        console.error('Latest results error:', error);
    }
}

async function showLastAutoBatchResults() {
    if (!latestAutoBatchJob) {
        await loadLatestAutoBatchResults();
    }

    if (!latestAutoBatchJob?.results?.length) {
        showAlert('No previous Auto-Get results are available.', 'info');
        return;
    }

    showBatchResults(latestAutoBatchJob.results);
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
            <td>
                ${escapeHtml(result.item_title || result.item_id || 'Unknown')}
                ${renderBatchResultMeta(result)}
            </td>
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

function renderBatchResultMeta(result) {
    const details = [];
    if (result.operation) details.push(formatOperationLabel(result.operation));
    if (result.timestamp) details.push(formatLogTimestamp(result.timestamp));
    if (result.season_results?.length) {
        const successCount = result.season_results.filter(season => season.success).length;
        details.push(`${successCount}/${result.season_results.length} seasons`);
    }
    if (!details.length) return '';
    return `<div class="small text-muted">${details.map(escapeHtml).join(' &middot; ')}</div>`;
}

// Button enable state
function updateUploadAllButton() {
    const uploadBtn = document.getElementById('uploadAllBtn');
    const queueBtn = document.getElementById('manualQueueStartBtn');
    const queueActionCount = document.getElementById('manualQueueActionCount');
    const uploadActionCount = document.getElementById('manualUploadActionCount');
    const queueCountSpan = document.getElementById('manualQueueCount');
    const selectedCountSpan = document.getElementById('selectedCount');
    const selectedCount = Object.keys(selectedPosters).length;
    const queuedCount = manualQueueIds.size;

    if (queueCountSpan) queueCountSpan.textContent = queuedCount;
    if (queueActionCount) queueActionCount.textContent = queuedCount;
    if (selectedCountSpan) selectedCountSpan.textContent = selectedCount;
    if (uploadActionCount) uploadActionCount.textContent = selectedCount;

    if (queueBtn) {
        queueBtn.disabled = queuedCount === 0 && !manualQueueActive;
        if (manualQueueWorkerRunning) {
            queueBtn.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>Set Posters for Queued<span class="badge bg-primary ms-2" id="manualQueueActionCount">${queuedCount}</span>`;
        } else {
            queueBtn.innerHTML = `<i class="fas fa-search me-1"></i>Set Posters for Queued<span class="badge bg-primary ms-2" id="manualQueueActionCount">${queuedCount}</span>`;
        }
    }

    if (uploadBtn) {
        if (selectedCount > 0) {
            uploadBtn.disabled = false;
        } else {
            uploadBtn.disabled = true;
        }
        uploadBtn.innerHTML = `<i class="fas fa-cloud-upload-alt"></i><span class="badge bg-light text-dark ms-2" id="manualUploadActionCount">${selectedCount}</span>`;
    }

    updateManualSelectionVisibility();
}

function updateManualSelectionVisibility() {
    const manualRow = document.getElementById('manualSelectionRow');

    if (manualRow) manualRow.style.display = '';
}

function initManualQueueControls() {
    document.querySelectorAll('.manual-queue-checkbox').forEach(checkbox => {
        checkbox.addEventListener('change', () => toggleManualQueueItem(checkbox));
    });
}

async function toggleManualQueueItem(checkbox) {
    const itemId = checkbox.getAttribute('data-item-id');
    if (!itemId) return;

    const wrapper = document.querySelector(`[data-item-id="${cssEscapeValue(itemId)}"]`);
    const hasQueuedUpload = Boolean(selectedPosters[itemId]) || manualQueueSelectionIds.has(itemId);

    if (hasQueuedUpload && checkbox.checked) {
        checkbox.checked = false;
        const confirmed = await showConfirmDialog({
            title: 'Discard queued upload?',
            message: 'This item already has a selected poster queued for upload.',
            details: 'Discard the selected poster and queue this item for manual selection instead?',
            confirmText: 'Discard and Queue',
            cancelText: 'Keep Queued Upload',
            variant: 'warning'
        });

        if (!confirmed) {
            updateUploadAllButton();
            return;
        }

        try {
            await clearPosterSelectionForItem(itemId);
        } catch (error) {
            console.error('Error clearing queued poster:', error);
            showAlert('Failed to discard queued poster: ' + error.message, 'danger');
            updateUploadAllButton();
            return;
        }

        checkbox.checked = true;
    }

    if (checkbox.checked) {
        manualQueueIds.add(itemId);
        wrapper?.classList.add('manual-queued');
    } else {
        manualQueueIds.delete(itemId);
        wrapper?.classList.remove('manual-queued');
    }

    updateUploadAllButton();
    applyGridFilters();
}

function setManualQueueItemQueued(itemId, queued) {
    const checkbox = document.querySelector(`.manual-queue-checkbox[data-item-id="${cssEscapeValue(itemId)}"]`);
    const wrapper = document.querySelector(`[data-item-id="${cssEscapeValue(itemId)}"]`);
    if (checkbox) checkbox.checked = queued;
    wrapper?.classList.toggle('manual-queued', queued);
    if (queued) manualQueueIds.add(itemId);
    else manualQueueIds.delete(itemId);
    applyGridFilters();
}

function isPosterModalOpen() {
    return document.getElementById('posterModal')?.classList.contains('show');
}

async function startManualPosterQueue() {
    if (manualQueueIds.size === 0 && !manualQueueActive) {
        showAlert('Choose one or more items to queue first.', 'warning');
        return;
    }

    if (!manualQueueActive) {
        manualQueueActive = true;
        manualQueueOrder = Array.from(document.querySelectorAll('.manual-queue-checkbox:checked'))
            .map(checkbox => checkbox.getAttribute('data-item-id'))
            .filter(Boolean);
        manualQueueResults = new Map();
        manualQueueErrors = new Map();
        manualQueuePresentedIds = new Set();
    }

    updateUploadAllButton();
    startManualQueueWorker();
    showNextManualQueueResult();
}

async function startManualQueueWorker() {
    if (manualQueueWorkerRunning) return;
    manualQueueWorkerRunning = true;
    updateUploadAllButton();

    try {
        for (const itemId of manualQueueOrder) {
            if (!manualQueueActive || manualQueueResults.has(itemId) || manualQueueErrors.has(itemId)) continue;
            if (!manualQueueIds.has(itemId)) {
                manualQueuePresentedIds.add(itemId);
                continue;
            }

            try {
                const data = await fetchPostersForItem(itemId, 3);
                manualQueueResults.set(itemId, data);
            } catch (error) {
                manualQueueErrors.set(itemId, error.message);
                showAlert(`Failed to load queued posters: ${error.message}`, 'danger');
            }

            showNextManualQueueResult();
        }
    } finally {
        manualQueueWorkerRunning = false;
        updateUploadAllButton();
        showNextManualQueueResult();
    }
}

function showNextManualQueueResult() {
    if (!manualQueueActive || manualQueueCurrentItemId || isPosterModalOpen()) return;

    for (const itemId of manualQueueOrder) {
        if (manualQueuePresentedIds.has(itemId)) continue;
        if (!manualQueueIds.has(itemId)) {
            manualQueuePresentedIds.add(itemId);
            continue;
        }

        if (manualQueueErrors.has(itemId)) {
            manualQueuePresentedIds.add(itemId);
            setManualQueueItemQueued(itemId, false);
            continue;
        }

        const data = manualQueueResults.get(itemId);
        if (!data) break;

        manualQueueCurrentItemId = itemId;
        setManualQueueItemQueued(itemId, false);
        preparePosterSearchForItem(itemId, data.poster_set_limit || 3);
        currentPosterSetLimit = data.poster_set_limit || 3;
        canBrowseMorePosterSets = Boolean(data.can_browse_more_sets);
        displayPosters(data.item, data.posters, data.poster_groups || [], data.eligible_seasons || []);
        updateUploadAllButton();
        return;
    }

    const pending = manualQueueOrder.some(itemId =>
        !manualQueuePresentedIds.has(itemId) && !manualQueueResults.has(itemId) && !manualQueueErrors.has(itemId)
    );
    if (!pending && !manualQueueWorkerRunning) {
        manualQueueActive = false;
        manualQueueOrder = [];
        updateUploadAllButton();
    }
}

// Notifications
function getAlertIcon(type) {
    return {
        success: 'fa-check-circle',
        danger: 'fa-exclamation-circle',
        warning: 'fa-triangle-exclamation',
        info: 'fa-info-circle',
    }[type] || 'fa-info-circle';
}

function showAlert(message, type = 'info') {
    const safeMessage = escapeHtml(message);
    const container = document.getElementById('toastContainer');
    if (!container || !window.bootstrap?.Toast) {
        const alertHtml = `
            <div class="alert alert-${type} alert-dismissible fade show" role="alert">
                <i class="fas ${getAlertIcon(type)} me-2"></i>
                ${safeMessage}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
        `;
        const fallbackContainer = document.querySelector('.container') || document.body;
        fallbackContainer.insertAdjacentHTML('afterbegin', alertHtml);
        setTimeout(() => fallbackContainer.querySelector('.alert')?.remove(), 5000);
        return;
    }

    const toast = document.createElement('div');
    toast.className = `toast app-toast app-toast-${type}`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');
    toast.innerHTML = `
        <div class="toast-body d-flex align-items-start gap-2">
            <i class="fas ${getAlertIcon(type)} mt-1"></i>
            <div class="flex-grow-1">${safeMessage}</div>
            <button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
    `;
    container.appendChild(toast);

    const toastInstance = new bootstrap.Toast(toast, { delay: 5000 });
    toast.addEventListener('hidden.bs.toast', () => toast.remove());
    toastInstance.show();
}

function showConfirmDialog({ title = 'Confirm action', message = '', details = '', confirmText = 'Continue', cancelText = 'Cancel', variant = 'primary' } = {}) {
    const modalElement = document.getElementById('confirmModal');
    const titleElement = document.getElementById('confirmModalTitle');
    const bodyElement = document.getElementById('confirmModalBody');
    const confirmButton = document.getElementById('confirmModalConfirmBtn');
    const cancelButton = document.getElementById('confirmModalCancelBtn');
    if (!modalElement || !confirmButton || !bodyElement || !window.bootstrap?.Modal) {
        return Promise.resolve(false);
    }

    titleElement.textContent = title;
    cancelButton.textContent = cancelText;
    confirmButton.textContent = confirmText;
    confirmButton.className = `btn btn-${variant}`;
    bodyElement.innerHTML = `
        <p class="mb-${details ? '2' : '0'}">${escapeHtml(message)}</p>
        ${details ? `<div class="confirm-details">${escapeHtml(details).replace(/\n/g, '<br>')}</div>` : ''}
    `;

    return new Promise(resolve => {
        let resolved = false;
        const finish = (value) => {
            if (resolved) return;
            resolved = true;
            confirmButton.removeEventListener('click', onConfirm);
            modalElement.removeEventListener('hidden.bs.modal', onHidden);
            resolve(value);
        };
        const onConfirm = () => {
            finish(true);
            (confirmModal || bootstrap.Modal.getOrCreateInstance(modalElement)).hide();
        };
        const onHidden = () => finish(false);

        confirmButton.addEventListener('click', onConfirm);
        modalElement.addEventListener('hidden.bs.modal', onHidden, { once: true });
        (confirmModal || bootstrap.Modal.getOrCreateInstance(modalElement)).show();
    });
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function cssEscapeValue(value) {
    if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(String(value));
    return String(value).replace(/["\\]/g, '\\$&');
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
        applyProcessedItemMarkers(activeProcessedItemDetails, { refreshFilters: false });
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
        applyProcessedItemMarkers(activeProcessedItemDetails, { refreshFilters: false });
        applyFailedItemMarkers(activeFailedItemIds);
    } catch (error) {
        console.error('Processed items error:', error);
    }
}

async function clearProcessedItems() {
    await clearProcessedItemsByScope();
}

async function clearProcessedItemsForVisibleItems() {
    const visibleItemIds = Array.from(document.querySelectorAll('.item-card-wrapper:not(.hidden)'))
        .map(wrapper => wrapper.getAttribute('data-item-id'))
        .filter(itemId => itemId && activeProcessedItemDetails.has(itemId));

    if (visibleItemIds.length === 0) {
        showAlert('No visible processed items to clear.', 'info');
        return;
    }

    await clearProcessedItemsByScope(visibleItemIds);
}

async function clearProcessedItemsByScope(itemIds = null) {
    const isVisibleOnly = Array.isArray(itemIds);
    const confirmed = await showConfirmDialog({
        title: isVisibleOnly ? 'Clear visible processed items?' : 'Clear processed items?',
        message: isVisibleOnly
            ? `This will remove processed markers for ${itemIds.length} visible item${itemIds.length === 1 ? '' : 's'}.`
            : 'This will remove all processed markers and history used by Skip already processed.',
        details: isVisibleOnly
            ? 'Items outside the current visible grid will keep their processed history.'
            : 'Posters that were already changed will no longer be considered processed until they are processed again.',
        confirmText: isVisibleOnly ? 'Clear visible processed' : 'Clear processed',
        cancelText: 'Cancel',
        variant: 'danger'
    });

    if (!confirmed) return;

    try {
        const response = await fetch('/processed-items', {
            method: 'DELETE',
            headers: isVisibleOnly ? { 'Content-Type': 'application/json' } : undefined,
            body: isVisibleOnly ? JSON.stringify({ item_ids: itemIds }) : undefined
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to clear processed items');

        if (isVisibleOnly) {
            itemIds.forEach(itemId => activeProcessedItemDetails.delete(itemId));
        } else {
            activeProcessedItemDetails = new Map();
        }
        applyProcessedItemMarkers(activeProcessedItemDetails, { refreshFilters: false });
        applyFailedItemMarkers(activeFailedItemIds);
        latestAutoBatchJob = null;
        await loadLatestAutoBatchResults();
        showAlert(isVisibleOnly ? 'Visible processed items cleared' : 'Processed items cleared', 'success');
    } catch (error) {
        console.error('Clear processed items error:', error);
        showAlert('Failed to clear processed items: ' + error.message, 'danger');
    }
}

function initProtectedItemButtons() {
    document.querySelectorAll('.protected-item-toggle').forEach(button => {
        button.addEventListener('click', () => toggleProtectedItem(button.getAttribute('data-item-id'), button));
    });
}

async function loadProtectedItems() {
    try {
        const response = await fetch('/protected-items');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load protected items');

        protectedItemIds = new Set((data.items || []).filter(Boolean));
        applyProtectedItemMarkers();
    } catch (error) {
        console.error('Protected items error:', error);
        showAlert('Failed to load protected items: ' + error.message, 'danger');
    }
}

function applyProtectedItemMarkers() {
    document.querySelectorAll('.item-card-wrapper').forEach(wrapper => {
        const itemId = wrapper.getAttribute('data-item-id');
        const card = wrapper.querySelector('.item-card');
        const button = wrapper.querySelector('.protected-item-toggle');
        const isProtected = protectedItemIds.has(itemId);
        if (card) card.classList.toggle('protected-item', isProtected);
        if (!button) return;

        button.classList.toggle('active', isProtected);
        button.title = isProtected ? 'Protected from Auto-Get' : 'Protect from Auto-Get';
        button.setAttribute('aria-label', isProtected ? 'Remove Auto-Get protection' : 'Protect from Auto-Get');
        button.innerHTML = isProtected ? '<i class="fas fa-lock"></i>' : '<i class="fas fa-lock-open"></i>';
    });
    applyGridFilters();
}

async function toggleProtectedItem(itemId, button) {
    if (!itemId) return;
    if (button) button.disabled = true;

    try {
        const response = await fetch('/protected-items/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ item_id: itemId })
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Failed to update protected item');

        protectedItemIds = new Set((data.items || []).filter(Boolean));
        applyProtectedItemMarkers();
        showAlert(data.protected ? 'Item protected from Auto-Get' : 'Item can be processed by Auto-Get again', 'success');
    } catch (error) {
        console.error('Protected item toggle error:', error);
        showAlert('Failed to update protection: ' + error.message, 'danger');
    } finally {
        if (button) button.disabled = false;
    }
}

function formatProcessedItemTooltip(detail) {
    const timestamp = formatLogTimestamp(detail.timestamp);
    const targets = detail.poster_targets || {};
    const parts = [];

    if (targets.series_poster) {
        parts.push('Series poster');
    } else if (detail.item_type === 'Movie' || detail.poster_url) {
        parts.push(detail.item_type === 'Movie' ? 'Movie poster' : 'Primary poster');
    }

    const seasonTitles = Array.isArray(targets.season_titles) ? targets.season_titles.filter(Boolean) : [];
    if (seasonTitles.length) {
        parts.push(`Season posters: ${seasonTitles.join(', ')}`);
    } else if (Number(targets.season_count || 0) > 0) {
        const seasonCount = Number(targets.season_count);
        parts.push(`${seasonCount} season poster${seasonCount === 1 ? '' : 's'}`);
    }

    const targetSummary = parts.length ? parts.join('\n') : 'Poster processed';
    return `${targetSummary}\nProcessed ${timestamp}`;
}

function applyProcessedItemMarkers(itemDetails, options = {}) {
    const refreshFilters = options.refreshFilters !== false;
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
            overlay.title = formatProcessedItemTooltip(detail);
            overlay.innerHTML = '<span class="badge bg-success"><i class="fas fa-check"></i></span>';
        } else if (!isProcessed && overlay) {
            overlay.remove();
        }
    });
    if (refreshFilters) applyGridFilters();
}

function applyFailedItemMarkers(itemIds, options = {}) {
    const refreshFilters = options.refreshFilters !== false;
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
            overlay.innerHTML = '<span class="badge bg-danger"><i class="fas fa-triangle-exclamation"></i></span>';
        } else if (!isFailed && overlay) {
            overlay.remove();
        }
    });
    if (refreshFilters) applyGridFilters();
}

function applyAutoBatchResultMarkers(results = [], timestamp = null) {
    results.forEach(result => {
        const itemId = result.item_id;
        if (!itemId) return;

        if (result.success) {
            const seasonResults = Array.isArray(result.season_results) ? result.season_results : [];
            const successfulSeasons = seasonResults.filter(season => season.success);
            activeFailedItemIds.delete(itemId);
            activeFailedItemDetails.delete(itemId);
            activeProcessedItemDetails.set(itemId, {
                item_id: itemId,
                item_title: result.item_title,
                item_type: result.item_type,
                item_year: result.item_year,
                poster_url: result.poster_url,
                timestamp: timestamp || new Date().toISOString(),
                poster_targets: {
                    series_poster: Boolean(result.poster_url),
                    season_count: successfulSeasons.length,
                    season_titles: successfulSeasons.map(season => season.season_title).filter(Boolean)
                }
            });
        } else {
            activeProcessedItemDetails.delete(itemId);
            activeFailedItemIds.add(itemId);
            activeFailedItemDetails.set(itemId, {
                item_id: itemId,
                item_title: result.item_title,
                item_type: result.item_type,
                item_year: result.item_year,
                error: result.error || 'Unknown failure',
                timestamp: timestamp || new Date().toISOString(),
                operation: 'auto-poster'
            });
        }
    });

    applyProcessedItemMarkers(activeProcessedItemDetails, { refreshFilters: false });
    applyFailedItemMarkers(activeFailedItemIds);
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
    const confirmed = await showConfirmDialog({
        title: 'Clear failed items?',
        message: 'Clear all failed item entries?',
        confirmText: 'Clear list',
        variant: 'danger'
    });
    if (!confirmed) return;

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
        applyFailedItemMarkers(activeFailedItemIds, { refreshFilters: false });
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
    const confirmed = await showConfirmDialog({
        title: 'Retry failed items?',
        message: 'Retry poster fetch and upload for all recent failed items?',
        confirmText: 'Retry all',
        variant: 'warning'
    });
    if (!confirmed) return;

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

function saveActiveAutoBatchJob(jobId) {
    try {
        if (jobId) localStorage.setItem(ACTIVE_AUTO_BATCH_JOB_KEY, jobId);
        else localStorage.removeItem(ACTIVE_AUTO_BATCH_JOB_KEY);
    } catch (e) {}
}

function getSavedActiveAutoBatchJob() {
    try {
        return localStorage.getItem(ACTIVE_AUTO_BATCH_JOB_KEY);
    } catch (e) {
        return null;
    }
}

function clearActiveAutoBatchJob() {
    saveActiveAutoBatchJob(null);
}

async function resumeAutoBatchProgressOnLoad() {
    const savedJobId = getSavedActiveAutoBatchJob();
    if (!savedJobId || currentAutoBatchJobId) return;

    currentAutoBatchJobId = savedJobId;
    setAutoBatchRunning(true);
    await pollAutoBatchProgress(savedJobId);

    if (currentAutoBatchJobId === savedJobId && !autoBatchPollTimer) {
        autoBatchPollTimer = setInterval(() => pollAutoBatchProgress(savedJobId), 1000);
    }
}

function calculateAutoBatchEta(job, processed, remaining) {
    if (processed < AUTO_BATCH_ESTIMATE_MIN_PROCESSED && !job.done) {
        return 'Waiting for more samples';
    }
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
    const canShowEstimate = processed >= AUTO_BATCH_ESTIMATE_MIN_PROCESSED;
    const progressWrap = document.getElementById('autoBatchProgressBarWrap');
    const etaWrap = document.getElementById('autoBatchEtaWrap');

    panel.style.display = 'block';
    document.getElementById('autoBatchProgressStatus').textContent = job.message || 'Running automatic poster batch...';
    document.getElementById('autoBatchCurrentItem').textContent = job.current_item ? `Current item: ${job.current_item}` : 'No item currently processing';
    document.getElementById('autoBatchProgressCounts').textContent = `${processed} / ${total}`;
    if (progressWrap) progressWrap.style.display = canShowEstimate ? '' : 'none';
    if (etaWrap) etaWrap.style.display = canShowEstimate ? '' : 'none';
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
        if (!autoBatchStartedAt && job.created_at) {
            const createdAt = Date.parse(job.created_at);
            if (!Number.isNaN(createdAt)) autoBatchStartedAt = createdAt;
        }
        updateAutoBatchProgress(job);
        applyAutoBatchResultMarkers(job.results || [], job.updated_at);

        if (job.done) {
            stopAutoBatchPolling();
            setAutoBatchRunning(false);
            currentAutoBatchJobId = null;
            autoBatchStartedAt = null;
            clearActiveAutoBatchJob();
            latestAutoBatchJob = job;
            updateLastResultsButton();
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
        autoBatchStartedAt = null;
        clearActiveAutoBatchJob();
        console.error('Auto-batch progress error:', error);
        showAlert('Failed to update auto-batch progress: ' + error.message, 'danger');
    }
}

async function cancelAutoBatch() {
    if (!currentAutoBatchJobId) return;
    const confirmed = await showConfirmDialog({
        title: 'Cancel poster search?',
        message: 'Cancel the running poster search and apply job?',
        confirmText: 'Cancel job',
        variant: 'danger'
    });
    if (!confirmed) return;

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
        const queuedItemIds = filter === 'queued' ? Array.from(manualQueueIds) : [];
        if (filter === 'queued' && queuedItemIds.length === 0) {
            showAlert('Choose one or more items with the Queue checkbox first.', 'warning');
            return;
        }

        const confirmText = {
            'no-poster': 'Automatically find and upload posters for items without posters?',
            'all': 'Automatically find and upload posters for ALL items?',
            'queued': `Automatically find and upload posters for ${queuedItemIds.length} queued item${queuedItemIds.length === 1 ? '' : 's'}?`,
            'movies': 'Automatically find and upload posters for all Movies?',
            'series': 'Automatically find and upload posters for all Series?'
        }[filter] || 'Start automatic poster upload?';
        const librarySelect = document.getElementById('libraryFilter');
        const libraryId = librarySelect?.value || '';
        const libraryName = libraryId ? librarySelect.options[librarySelect.selectedIndex]?.text : '';

        const skipProcessed = Boolean(document.getElementById('skipProcessedAutoBatch')?.checked);
        const includeSeasonPosters = Boolean(document.getElementById('includeSeasonPostersAutoBatch')?.checked);
        const replaceSeasonPosters = Boolean(document.getElementById('replaceSeasonPostersAutoBatch')?.checked);
        const confirmNotes = [];
        if (skipProcessed) confirmNotes.push('Already processed items will be skipped.');
        if (includeSeasonPosters) {
            confirmNotes.push(replaceSeasonPosters ?
                'Season posters will be included and existing season posters may be replaced.' :
                'Season posters will be included only when a season is missing a poster.');
        }
        if (libraryName) confirmNotes.push(`Library: ${libraryName}`);

        const confirmed = await showConfirmDialog({
            title: 'Find and apply posters?',
            message: confirmText,
            details: confirmNotes.join('\n'),
            confirmText: 'Start',
            variant: 'primary'
        });
        if (!confirmed) return;

        stopAutoBatchPolling();
        setAutoBatchRunning(true);
        currentAutoBatchJobId = null;
        clearActiveAutoBatchJob();
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
            body: JSON.stringify({
                filter,
                library_id: libraryId,
                item_ids: queuedItemIds,
                skip_processed: skipProcessed,
                include_season_posters: includeSeasonPosters,
                replace_existing_season_posters: replaceSeasonPosters
            })
        });

        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || 'Automatic batch failed');

        currentAutoBatchJobId = data.job_id;
        saveActiveAutoBatchJob(data.job_id);
        await pollAutoBatchProgress(data.job_id);
        if (currentAutoBatchJobId === data.job_id && !autoBatchPollTimer) {
            autoBatchPollTimer = setInterval(() => pollAutoBatchProgress(data.job_id), 1000);
        }

    } catch (err) {
        console.error('Auto-batch error:', err);
        stopAutoBatchPolling();
        setAutoBatchRunning(false);
        currentAutoBatchJobId = null;
        autoBatchStartedAt = null;
        clearActiveAutoBatchJob();
        showAlert('Automatic batch failed: ' + err.message, 'danger');
    }
}
