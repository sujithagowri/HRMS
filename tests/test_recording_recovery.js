/* Run with Node 16+: node tests/test_recording_recovery.js
 * Exercise the shipped browser module across reloads with shared IDB storage.
 */
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const { Blob } = require('buffer');
const source = fs.readFileSync('dist/dist/assets/hrms-recording-recovery.js', 'utf8');

function fakeIDB() {
  const tables = { jobs: new Map(), chunks: new Map() };
  let failWrites = false;
  const database = {
    transaction(name) {
      const tx = {};
      tx.objectStore = () => {
        function operation(action, write) {
          const req = {};
          setImmediate(() => {
            if (write && failWrites) {
              tx.error = new Error('Quota exceeded');
              if (tx.onabort) tx.onabort();
              return;
            }
            req.result = action();
            if (tx.oncomplete) tx.oncomplete();
          });
          return req;
        }
        const table = tables[name];
        const matches = (value, range) => !range || value.key === range.key;
        return {
          put: value => operation(() => table.set(name === 'jobs' ? value.key : value.key + ':' + value.seq,
            Object.assign({}, value)), true),
          getAll: range => operation(() => Array.from(table.values()).filter(v => matches(v, range))
            .sort((a, b) => (a.seq || 0) - (b.seq || 0)).map(v => Object.assign({}, v))),
          delete: key => operation(() => {
            if (typeof key === 'string') table.delete(key);
            else for (const [id, value] of table) if (matches(value, key)) table.delete(id);
          }, true)
        };
      };
      return tx;
    }
  };
  return {
    tables,
    failWrites(value) { failWrites = value; },
    open() { const req = {}; setImmediate(() => { req.result = database; req.onsuccess(); }); return req; }
  };
}
function element() {
  return { childNodes: [], style: {}, setAttribute() {},
    appendChild(child) { this.childNodes.push(child); }, replaceChildren() { this.childNodes = []; } };
}
function boot(db, fetch) {
  const window = new EventTarget();
  const document = new EventTarget();
  document.readyState = 'loading'; document.body = element(); document.createElement = element;
  const navigator = { onLine: true };
  vm.runInNewContext(source, {
    window, document, navigator, indexedDB: db,
    IDBKeyRange: { bound: lower => ({ key: lower[0] }) },
    crypto: require('crypto').webcrypto, Blob, AbortController, Event, URL,
    fetch, console, setTimeout: (cb, ms) => setTimeout(cb, ms < 10000 ? 0 : ms), clearTimeout
  });
  return { api: window.HRMSRecording, window, document, navigator,
    reload: () => document.dispatchEvent(new Event('DOMContentLoaded')) };
}
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const tick = () => new Promise(setImmediate);
async function until(condition) {
  for (let i = 0; i < 1000; i++) { if (condition()) return; await tick(); }
  assert.fail('Timed out waiting for recording recovery');
}
function recorder(app, mime = 'video/webm') {
  const rec = new EventTarget(); rec.mimeType = mime;
  app.api.track(rec, { candidateName: 'Candidate', candidateEmail: 'c@example.com', role: 'Engineer' });
  return rec;
}
function chunk(rec, value) {
  const event = new Event('dataavailable'); event.data = new Blob([value], { type: rec.mimeType });
  rec.dispatchEvent(event);
}
const metadata = { candidateName: 'Candidate', transcript: 'Full answer', responses: [] };

async function run() {
  // A lost create response reuses the UUID; chunks include the final stop event.
  {
    const db = fakeIDB(), keys = []; let creates = 0, body;
    const app = boot(db, async (url, options) => {
      if (!url.endsWith('/video')) {
        keys.push(JSON.parse(options.body).clientUploadId);
        if (++creates === 1) throw new Error('Lost response');
        return response({ id: 7 });
      }
      body = options.body;
      return response({ videoStatus: 'ready', videoBytes: body.size });
    });
    const rec = recorder(app, 'video/mp4'); chunk(rec, 'first'); chunk(rec, 'last');
    const blob = new Blob(['firstlast'], { type: 'video/mp4' });
    const saved = await app.api.save(rec, metadata, blob);
    assert.equal(saved.id, 7); assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
    assert.equal(db.tables.chunks.size, 2);
    await app.api.upload(rec, saved.id, blob);
    assert.equal(body.type, 'video/mp4'); assert.equal(await body.text(), 'firstlast');
    assert.equal(db.tables.jobs.size, 0); assert.equal(db.tables.chunks.size, 0);
    console.log('PASS lost response, final chunks, MIME, server acknowledgement and cleanup');
  }
  // An offline final submission is reconstructed on reload, including metadata.
  {
    const db = fakeIDB(); const offline = boot(db, async () => { throw new Error('Offline'); });
    offline.navigator.onLine = false;
    const rec = recorder(offline); chunk(rec, 'one'); chunk(rec, 'two');
    await assert.rejects(offline.api.save(rec, metadata, new Blob(['onetwo'], { type: 'video/webm' })));
    assert.equal(db.tables.jobs.size, 1);
    let uploaded = false;
    const online = boot(db, async (url, options) => {
      if (!url.endsWith('/video')) {
        assert.equal(JSON.parse(options.body).transcript, metadata.transcript);
        return response({ id: 9 });
      }
      assert.equal(await options.body.text(), 'onetwo'); uploaded = true;
      return response({ videoStatus: 'ready', videoBytes: options.body.size });
    });
    online.reload(); await until(() => uploaded && db.tables.jobs.size === 0);
    console.log('PASS offline submission recovers video and transcript after reload');
  }
  // A rejected upload keeps local chunks; an online event retries the same ID.
  {
    const db = fakeIDB(); let failing = true, creates = 0, uploaded = false;
    const app = boot(db, async (url, options) => {
      if (!url.endsWith('/video')) { creates++; return response({ id: 11 }); }
      if (failing) return response({ error: 'Too large' }, 413);
      uploaded = true; return response({ videoStatus: 'ready', videoBytes: options.body.size });
    });
    const rec = recorder(app); chunk(rec, 'video');
    const blob = new Blob(['video'], { type: 'video/webm' });
    await app.api.save(rec, metadata, blob);
    await assert.rejects(app.api.upload(rec, 11, blob), /Too large/);
    assert.equal(db.tables.jobs.size, 1); assert.equal(db.tables.chunks.size, 1);
    failing = false; app.window.dispatchEvent(new Event('online'));
    await until(() => uploaded && db.tables.jobs.size === 0);
    assert.equal(creates, 1);
    console.log('PASS failed upload retained and online retry reuses server ID');
  }
  // An interrupted interview is never auto-submitted as a completed interview.
  {
    const db = fakeIDB(); let requests = 0;
    const app = boot(db, async () => { requests++; return response({}); });
    const rec = recorder(app); chunk(rec, 'partial');
    await until(() => db.tables.chunks.size === 1 && db.tables.jobs.size === 1);
    const next = boot(db, async () => { requests++; return response({}); });
    next.reload(); for (let i = 0; i < 30; i++) await tick();
    assert.equal(requests, 0); assert.equal(db.tables.chunks.size, 1);
    console.log('PASS interrupted interview keeps partial video without marking completion');
  }
  // A mismatched server byte count must never clear the recovery copy.
  {
    const db = fakeIDB();
    const app = boot(db, async url => response(url.endsWith('/video') ?
      { videoStatus: 'ready', videoBytes: 1 } : { id: 12 }));
    const rec = recorder(app); chunk(rec, 'video');
    const blob = new Blob(['video'], { type: 'video/webm' });
    await app.api.save(rec, metadata, blob);
    await assert.rejects(app.api.upload(rec, 12, blob), /confirm the complete video/);
    assert.equal(db.tables.chunks.size, 1);
    console.log('PASS incomplete server acknowledgement retains recovery copy');
  }
  // Quota failure permits the complete in-memory upload but blocks truncated reloads.
  {
    const db = fakeIDB(); const app = boot(db, async () => { throw new Error('Offline'); });
    app.navigator.onLine = false;
    const rec = recorder(app); chunk(rec, 'first');
    await until(() => db.tables.chunks.size === 1 && db.tables.jobs.size === 1);
    db.failWrites(true); chunk(rec, 'last');
    for (let i = 0; i < 30; i++) await tick();
    db.failWrites(false);
    await assert.rejects(app.api.save(rec, metadata, new Blob(['firstlast'], { type: 'video/webm' })));
    let uploads = 0;
    const next = boot(db, async () => { uploads++; return response({ id: 13 }); });
    next.reload(); for (let i = 0; i < 50; i++) await tick();
    assert.equal(uploads, 0);
    assert.equal(db.tables.jobs.values().next().value.state, 'failed');
    console.log('PASS incomplete browser storage cannot silently upload truncated video');
  }
  // Retention cleanup removes both metadata and chunks on the next visit.
  {
    const db = fakeIDB(); let uploads = 0;
    db.tables.jobs.set('expired', { key: 'expired', created: Date.now() - 86400001 });
    db.tables.chunks.set('expired:0', { key: 'expired', seq: 0, blob: new Blob(['old']) });
    const app = boot(db, async () => { uploads++; }); app.reload();
    await until(() => db.tables.jobs.size === 0 && db.tables.chunks.size === 0);
    assert.equal(uploads, 0);
    console.log('PASS expired recovery data is removed');
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
