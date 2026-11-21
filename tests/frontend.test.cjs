const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function browser() {
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {} },
        document: { addEventListener() {}, querySelectorAll: () => [], querySelector: () => null, getElementById: () => null },
        setTimeout: () => 1, clearInterval() {},
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        URLSearchParams, URL, Date, Set, Map,
    });
    vm.runInContext(fs.readFileSync('static/js/app.js', 'utf8'), context);
    vm.runInContext(fs.readFileSync('static/js/jobs.js', 'utf8'), context);
    return context;
}

test('URL-only previews lazy-load the thumbnail instead of reporting failure', () => {
    const context = browser();
    const html = vm.runInContext(`renderPosterImageFrame({url:'https://theposterdb.com/api/assets/1', preview_url:'https://images.theposterdb.com/preview/1', preview_needs_load:true}, 'Poster')`, context);
    assert.match(html, /data-full-src=/);
    assert.match(html, /images.theposterdb.com/);
    assert.doesNotMatch(html, /Image failed to load/);
    assert.doesNotMatch(html, /display: none/); // IntersectionObserver cannot observe a hidden image.
});

test('lazy preview becomes visible after loading', () => {
    const context = browser();
    let onLoad;
    const image = { dataset: { fullSrc: '/thumbnail?url=test' }, style: { display: 'none' },
        removeAttribute() {}, classList: { remove() {} }, closest: () => null,
        addEventListener(event, handler) { if (event === 'load') onLoad = handler; } };
    context.loadPosterPreviewImage(image);
    onLoad();
    assert.equal(image.style.display, '');
    assert.equal(image.src, '/thumbnail?url=test');
});

test('saved selections and the manual search queue are restored from the server', async () => {
    const context = browser();
    context.fetch = async () => ({ ok: true, json: async () => ({ selections: { show: 'poster' }, queued_item_ids: ['movie'] }) });
    await context.restoreSelections();
    assert.equal(vm.runInContext('selectedPosters.show', context), 'poster');
    assert.equal(vm.runInContext("manualQueueIds.has('movie')", context), true);
});

test('a transient polling failure does not forget the active job', async () => {
    const context = browser();
    context.fetch = async () => { throw new Error('temporary outage'); };
    vm.runInContext("currentAutoBatchJobId = 'active-job'", context);
    await context.loadJobQueue();
    assert.equal(vm.runInContext('currentAutoBatchJobId', context), 'active-job');
    assert.equal(vm.runInContext('autoBatchPollTimer', context), 1);
});

test('job polls only refresh artwork for results added since the last poll', async () => {
    const context = browser();
    const requested = [];
    const refreshed = [];
    let job = { job_id: 'run', status: 'running', done: false, result_count: 2, completed_targets: 2 };
    const results = [{ item_id: 'a', primary_uploaded: true }, { item_id: 'b', primary_uploaded: true }];
    context.fetch = async (url) => {
        requested.push(url);
        if (url === '/jobs') return { ok: true, json: async () => ({ jobs: [job] }) };
        const start = Number(new URL(url, 'http://x').searchParams.get('start'));
        return { ok: true, json: async () => ({ results: results.slice(start) }) };
    };
    context.refreshGridArtwork = async (itemId) => refreshed.push(itemId);
    context.restoreSelections = async () => {};
    for (const name of ['loadFailedItems', 'loadProcessedItems', 'loadProtectedItems', 'loadLatestAutoBatchResults',
        'setAutoBatchRunning', 'updateAutoBatchProgress', 'saveActiveAutoBatchJob', 'renderJobQueue']) context[name] = () => {};
    await context.loadJobQueue();
    assert.deepEqual(refreshed, []); // Artwork already on the page when it loaded is current.
    results.push({ item_id: 'c', primary_uploaded: true }, { item_id: 'd', primary_uploaded: false });
    job = { ...job, result_count: 4, completed_targets: 4 };
    await context.loadJobQueue();
    assert.deepEqual(refreshed, ['b', 'c']); // b was the last result checked, so it may have been mid-upload.
    await context.loadJobQueue();
    assert.deepEqual(refreshed, ['b', 'c']); // Unchanged poll: nothing reloads.
    assert.ok(requested.includes('/jobs/run/results?start=1'));
});

test('the job panel and progress show before the grid has loaded', async () => {
    const context = browser();
    const shown = [];
    const job = { job_id: 'run', status: 'running', done: false, created_at: '2026-01-01T00:00:00Z', result_count: 0 };
    context.fetch = async () => ({ ok: true, json: async () => ({ jobs: [job] }) });
    context.renderJobQueue = jobs => shown.push(['panel', jobs.length]);
    context.setAutoBatchRunning = running => shown.push(['running', running]);
    context.updateAutoBatchProgress = active => shown.push(['progress', active.job_id]);
    context.showJobQueueEarly();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(shown, [['panel', 1], ['running', true], ['progress', 'run']]);
    const page = fs.readFileSync('templates/index.html', 'utf8');
    assert.ok(page.indexOf('showJobQueueEarly()') < page.indexOf('item-card-wrapper'));
    const layout = fs.readFileSync('templates/layout.html', 'utf8');
    assert.ok(layout.indexOf("filename='js/jobs.js'") < layout.indexOf('{% block content %}'));
});

test('the loading popup is armed before the picker cache check', async () => {
    const context = browser();
    const events = [];
    let respond;
    context.window = {};
    context.startDelayedPosterSearchLoading = () => { events.push('loading armed'); return () => events.push('loading stopped'); };
    context.applyPosterSearchData = () => events.push('posters shown');
    context.fetch = (url) => {
        events.push(url.includes('cache_only=true') ? 'cache check' : 'search');
        return new Promise(resolve => { respond = () => resolve({ ok: true, json: async () => ({ from_cache: true, item: {} }) }); });
    };
    const pending = context.loadPosters('movie');
    assert.deepEqual(events, ['loading armed', 'cache check']); // A slow cache check still shows the popup.
    respond();
    await pending;
    assert.deepEqual(events, ['loading armed', 'cache check', 'loading stopped', 'posters shown']);
});

test('upload flows do not reload the page and discard other work', () => {
    const source = fs.readFileSync('static/js/app.js', 'utf8');
    assert.doesNotMatch(source, /window\.location\.reload/);
    assert.match(source, /enqueuePosterJob\(\{ kind: 'manual'/);
    assert.match(source, /enqueuePosterJob\(\{ kind: 'retry'/);
});
