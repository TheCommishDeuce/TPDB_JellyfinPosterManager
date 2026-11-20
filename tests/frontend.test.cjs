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
