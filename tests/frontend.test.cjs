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

test('the processed tooltip lists season posters as sorted ranges', () => {
    const context = browser();
    context.formatLogTimestamp = () => 'now';
    const tooltip = context.formatProcessedItemTooltip({ poster_targets: { series_poster: true,
        season_titles: ['Season 36', 'Season 35', 'Season 34', 'Season 33', 'Specials', 'Season 8', 'Season 1'] } });
    assert.equal(tooltip, 'Series poster\nSeason posters: 1, 8, 33\u201336, Specials\nProcessed now');
});

test('upload flows do not reload the page and discard other work', () => {
    const source = fs.readFileSync('static/js/app.js', 'utf8');
    assert.doesNotMatch(source, /window\.location\.reload/);
    assert.match(source, /enqueuePosterJob\(\{ kind: 'manual'/);
    assert.match(source, /enqueuePosterJob\(\{ kind: 'retry'/);
});
