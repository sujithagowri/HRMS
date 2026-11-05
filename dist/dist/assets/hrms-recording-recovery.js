/* F2F recording recovery. Loaded before the pre-built React application.
 * Chunks stay in IndexedDB until the server acknowledges the exact byte count.
 * Finalized jobs retry on reload/online; interrupted jobs offer download only.
 */
(function () {
  'use strict';
  var API = '/api/interview-recordings';
  var TTL = 24 * 60 * 60 * 1000;
  var sessions = new WeakMap(), jobs = new Map(), active = new Set(), uploads = new Map();
  var dbPromise, notice = '', panel;

  function uuid() {
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var h = Array.from(bytes, function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function database() {
    if (!dbPromise) dbPromise = new Promise(function (resolve, reject) {
      var req = indexedDB.open('hrms-interview-recovery', 1);
      req.onupgradeneeded = function () {
        req.result.createObjectStore('jobs', { keyPath: 'key' });
        req.result.createObjectStore('chunks', { keyPath: ['key', 'seq'] });
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
    return dbPromise;
  }
  async function store(name, mode, action) {
    var db = await database();
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(name, mode), req = action(tx.objectStore(name));
      tx.oncomplete = function () { resolve(req && req.result); };
      tx.onerror = tx.onabort = function () { reject(tx.error || new Error('Local recording storage failed')); };
    });
  }
  function range(key) { return IDBKeyRange.bound([key, 0], [key, Number.MAX_SAFE_INTEGER]); }
  function snapshot(job) {
    // Promises and in-memory blobs are deliberately not cloned into IndexedDB.
    return { key: job.key, metadata: job.metadata, mime: job.mime, created: job.created,
      finalized: job.finalized, recordId: job.recordId, state: job.state, error: job.error,
      storageFailed: job.storageFailed };
  }
  function persist(job) {
    job.queue = job.queue.then(function () {
      var value = snapshot(job);
      return store('jobs', 'readwrite', function (s) { return s.put(value); });
    }).catch(function () {
      job.storageFailed = true;
      notice = 'Browser recovery storage is unavailable. Keep this page open until video upload finishes.';
      render();
    });
    return job.queue;
  }
  async function remove(job) {
    // A tombstone prevents another tab or a failed cleanup from re-uploading.
    job.state = 'ready';
    await persist(job);
    await store('chunks', 'readwrite', function (s) { return s.delete(range(job.key)); });
    await store('jobs', 'readwrite', function (s) { return s.delete(job.key); });
    jobs.delete(job.key);
    if (!jobs.size) notice = '';
    render();
  }
  async function blobFor(job) {
    if (job.blob) return job.blob;
    if (job.finalized && job.storageFailed) throw new Error('Browser storage could not retain the complete video. Retry from the original open page.');
    var chunks = await store('chunks', 'readonly', function (s) { return s.getAll(range(job.key)); });
    return new Blob(chunks.map(function (c) { return c.blob; }), { type: job.mime || (chunks[0] && chunks[0].blob.type) || 'video/webm' });
  }
  async function request(url, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 120000);
    try {
      var response = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
      var data = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        var error = new Error(data.error || data.message || data.detail || 'Upload failed (HTTP ' + response.status + ')');
        error.retryable = response.status >= 500 || [408, 429].indexOf(response.status) >= 0;
        throw error;
      }
      return data;
    } finally { clearTimeout(timer); }
  }
  async function retry(action) {
    for (var attempt = 0; ; attempt++) {
      try { return await action(); }
      catch (error) {
        if (attempt >= 2 || error.retryable === false || navigator.onLine === false) throw error;
        await new Promise(function (resolve) { setTimeout(resolve, 1000 * Math.pow(2, attempt)); });
      }
    }
  }
  async function create(job) {
    // Always use the same UUID, including after a lost response or reload.
    var result = await retry(function () {
      return request(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({}, job.metadata, { clientUploadId: job.key })) });
    });
    job.recordId = result.id;
    await persist(job);
    return result;
  }
  function uploadJob(job) {
    if (uploads.has(job.key)) return uploads.get(job.key);
    if (job.state === 'ready') return Promise.resolve({ videoStatus: 'ready' });
    var promise = (async function () {
      try {
        job.state = 'uploading'; job.error = ''; render(); await persist(job);
        var blob = await blobFor(job);
        if (!blob.size) throw new Error('No video chunks are available in this browser.');
        if (!job.recordId) await create(job);
        var result = await retry(function () {
          return request(API + '/' + job.recordId + '/video', { method: 'POST',
            headers: { 'Content-Type': blob.type || job.mime || 'video/webm' }, body: blob });
        });
        if (result.videoStatus !== 'ready' || result.videoBytes !== blob.size) {
          throw new Error('The server did not confirm the complete video. Please retry.');
        }
        job.state = 'ready';
        try { await remove(job); }
        catch (_) { notice = 'Video uploaded. Browser copy cleanup will be retried on reload.'; render(); }
        window.dispatchEvent(new Event('hrmsRecordingUploaded'));
        return result;
      } catch (error) {
        job.state = 'failed'; job.error = error.message; await persist(job); render(); throw error;
      }
    })();
    uploads.set(job.key, promise);
    promise.then(function () { uploads.delete(job.key); }, function () { uploads.delete(job.key); });
    return promise;
  }
  function track(recorder, metadata) {
    var job = { key: uuid(), metadata: metadata, mime: recorder.mimeType,
      created: Date.now(), finalized: false, state: 'recording', queue: Promise.resolve(), seq: 0 };
    sessions.set(recorder, job); jobs.set(job.key, job); active.add(job.key); persist(job);
    recorder.addEventListener('dataavailable', function (event) {
      if (!event.data || !event.data.size) return;
      job.mime = event.data.type || recorder.mimeType || job.mime;
      var chunk = { key: job.key, seq: job.seq++, blob: event.data };
      job.queue = job.queue.then(function () {
        return store('chunks', 'readwrite', function (s) { return s.put(chunk); });
      }).catch(function () {
        job.storageFailed = true;
        notice = 'Browser recovery storage is full or unavailable. Keep this page open until video upload finishes.';
        render();
      });
      persist(job);
    });
    recorder.addEventListener('error', function () {
      notice = 'Video recording was interrupted. Keep this page open to submit or download the available video.';
      render();
    });
    recorder.addEventListener('stop', function () {
      if (!job.finalized) {
        active.delete(job.key); job.state = 'interrupted'; persist(job); render();
      }
    });
    return job.key;
  }
  async function save(recorder, metadata, blob) {
    var job = sessions.get(recorder);
    if (!job) { track(recorder, metadata); job = sessions.get(recorder); }
    job.blob = blob; job.mime = blob.type; job.metadata = metadata;
    job.finalized = true; job.state = 'pending'; active.delete(job.key);
    // Capture the final dataavailable write before creating server metadata.
    await job.queue;
    await persist(job); render();
    try { return await create(job); }
    catch (error) { job.state = 'failed'; job.error = error.message; await persist(job); render(); throw error; }
  }
  async function download(job) {
    try {
      var blob = await blobFor(job), a = document.createElement('a');
      if (!blob.size) throw new Error('No local video chunks are available.');
      var url = URL.createObjectURL(blob);
      a.href = url; a.download = 'interview-' + job.key + (blob.type.indexOf('mp4') >= 0 ? '.mp4' : '.webm');
      a.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
    } catch (error) { job.error = error.message; render(); }
  }
  function render() {
    if (!document.body) return;
    if (!panel) {
      panel = document.createElement('aside'); panel.setAttribute('aria-label', 'Interview video uploads');
      panel.style.cssText = 'position:fixed;bottom:16px;left:16px;z-index:900;max-width:360px;max-height:45vh;overflow:auto;padding:14px;border-radius:12px;background:var(--bg2,#fff);color:var(--text,#182235);box-shadow:0 4px 24px #0004;font:13px system-ui';
      document.body.appendChild(panel);
    }
    panel.replaceChildren();
    function text(value) { var p = document.createElement('p'); p.textContent = value; panel.appendChild(p); }
    if (notice) text(notice);
    jobs.forEach(function (job) {
      if (active.has(job.key) || job.state === 'ready') return;
      text((job.metadata.candidateName || 'Interview') + ': ' +
        (!job.finalized ? 'Recording interrupted — check available video' :
          job.state === 'uploading' ? 'Interview submitted — video uploading' :
            job.state === 'failed' ? 'Video upload failed' : 'Video waiting to upload'));
      if (job.error) text(job.error);
      function button(label, action) {
        var b = document.createElement('button'); b.textContent = label;
        b.style.cssText = 'margin:3px;padding:6px 9px;cursor:pointer';
        b.onclick = action; panel.appendChild(b);
      }
      if (job.state !== 'uploading') {
        if (job.finalized) button('Retry upload', function () { uploadJob(job).catch(function () {}); });
        button('Download video', function () { download(job); });
        button('Discard local copy', function () {
          if (window.confirm('Discard this browser copy? Any video not uploaded will be lost.')) {
            remove(job).catch(function () { notice = 'Could not discard the local copy.'; render(); });
          }
        });
      }
    });
    if (panel.childNodes.length) text('Local copies older than 24 hours are removed on the next visit.');
    panel.hidden = !panel.childNodes.length;
  }
  async function recover() {
    try {
      var saved = await store('jobs', 'readonly', function (s) { return s.getAll(); });
      for (var item of saved) {
        if (jobs.has(item.key)) continue;
        item.queue = Promise.resolve();
        if (Date.now() - item.created > TTL || item.state === 'ready') { await remove(item); continue; }
        jobs.set(item.key, item);
      }
      render();
      jobs.forEach(function (job) {
        if (job.finalized && job.state !== 'ready' && !active.has(job.key) && navigator.onLine !== false) {
          uploadJob(job).catch(function () {});
        }
      });
    } catch (_) { /* Storage errors are shown when an actual recording starts. */ }
  }
  window.HRMSRecording = {
    track: track, save: save,
    upload: function (recorder, id, blob) {
      var job = sessions.get(recorder); job.recordId = id; job.blob = blob; return uploadJob(job);
    },
    statusLabel: function (record) {
      return record.hasVideo ? '▶ Play' : record.videoStatus === 'failed' ? 'Video unavailable' : 'Video pending';
    }
  };
  window.addEventListener('online', recover);
  window.addEventListener('beforeunload', function (event) {
    if (Array.from(jobs.values()).some(function (job) { return job.state !== 'ready'; })) {
      event.preventDefault(); event.returnValue = '';
    }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', recover);
  else recover();
})();
