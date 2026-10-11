'use strict';

const $ = (selector) => document.querySelector(selector);
const form = $('#download-form');
const list = $('#job-list');
const rows = new Map();
const icons = {
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  play: '<path d="m8 4 12 8-12 8z"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
};
function svg(name) { return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`; }
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function bytes(value) {
  if (value == null) return '';
  if (value < 1000) return `${value} B`;
  if (value < 1e6) return `${(value / 1000).toFixed(1)} KB`;
  if (value < 1e9) return `${(value / 1e6).toFixed(1)} MB`;
  return `${(value / 1e9).toFixed(2)} GB`;
}
function duration(seconds) {
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return `${minutes}:${String(remainder).padStart(2, '0')}`;
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const message = Array.isArray(body.detail) ? body.detail.map((e) => e.msg.replace(/^Value error, /, '')).join(' ') : body.detail;
    throw new Error(message || `Request failed (${response.status}).`);
  }
  return response.json();
}
function formError(message) {
  $('#form-error').textContent = message || '';
  $('#form-error').hidden = !message;
}
function syncOptions() {
  const original = form.elements.preset.value === 'original';
  $('#target-size').disabled = original;
  if (original) {
    $('#format').value = 'mp4';
    $('#target-size').value = '';
  }
  $('#size-help').textContent = original ? 'Keep original skips compression. Choose another preset to set a target.' : 'Two-pass encoding aims for this size. Smaller targets reduce quality.';
}
form.addEventListener('change', (event) => {
  if (event.target.name === 'format' && event.target.value !== 'mp4' && form.elements.preset.value === 'original') {
    form.querySelector('[value="balanced"]').checked = true;
  }
  syncOptions();
});
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#submit-button');
  formError('');
  button.disabled = true;
  button.querySelector('span').textContent = 'Adding download…';
  try {
    let headers = {};
    try { headers = JSON.parse($('#request-headers').value.trim() || '{}'); }
    catch { throw new Error('Request headers must be a valid JSON object.'); }
    if (!headers || Array.isArray(headers) || typeof headers !== 'object' || Object.values(headers).some((value) => typeof value !== 'string')) {
      throw new Error('Request headers must be a JSON object with string values.');
    }
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify({
      url: $('#stream-url').value.trim(), name: $('#file-name').value.trim() || 'my-video',
      format: $('#format').value, preset: form.elements.preset.value,
      target_mb: $('#target-size').value ? Number($('#target-size').value) : null,
      referer: $('#referer').value.trim(),
      connections: Number($('#connections').value), engine: $('#download-engine').value,
      user_agent: $('#user-agent').value.trim(), cookies: $('#cookies').value.trim(), headers,
      fallback_urls: $('#alternative-urls').value.split('\n').map((url) => url.trim()).filter(Boolean),
    }) });
    addRow(job);
    await refresh();
    $('#downloads-heading').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) { formError(error.message); }
  finally {
    button.disabled = false;
    button.querySelector('span').textContent = 'Download & convert';
  }
});

function addRow(job) {
  if (rows.has(job.id)) return;
  const row = el('article', 'job');
  row.dataset.id = job.id;
  row.innerHTML = `<div class="job-top"><span class="file-icon">${svg('file')}</span><div class="job-info"><h3 class="job-name"></h3><div class="job-meta"></div></div><span class="job-state"></span><div class="job-actions"></div></div><div class="job-progress"><div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-fill"></div></div><div class="progress-caption"><span class="stage"></span><span class="percent"></span></div></div><p class="job-error" hidden></p><div class="delete-confirm" hidden><span>Remove this file from this machine?</span><button class="secondary-button confirm-remove" type="button">Remove</button><button class="secondary-button keep-file" type="button">Keep it</button></div>`;
  row.querySelector('.keep-file').addEventListener('click', () => { row.querySelector('.delete-confirm').hidden = true; });
  row.querySelector('.confirm-remove').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await api(`/api/jobs/${job.id}`, { method: 'DELETE' }); await refresh(); }
    catch (error) { formError(error.message); }
    finally { button.disabled = false; }
  });
  rows.set(job.id, row);
  list.prepend(row);
  updateRow(job);
}
function action(label, icon, callback, className = 'secondary-button') {
  const button = el('button', className);
  button.type = 'button';
  if (icon) button.innerHTML = svg(icon);
  if (className === 'icon-button') button.setAttribute('aria-label', label);
  else button.append(document.createTextNode(label));
  button.addEventListener('click', callback);
  return button;
}
function updateRow(job) {
  const row = rows.get(job.id);
  const active = !['completed', 'failed', 'cancelled'].includes(job.status);
  row.querySelector('.job-name').textContent = job.filename;
  const meta = row.querySelector('.job-meta');
  const parts = [job.format === 'hevc' ? 'MP4 · HEVC' : job.format === 'webm' ? 'WebM · VP9' : 'MP4'];
  if (job.duration) parts.push(duration(job.duration));
  if (job.height) parts.push(`${job.height}p`);
  if (job.output_bytes) parts.push(bytes(job.output_bytes));
  if (!job.output_bytes) parts.push(job.source_host);
  meta.textContent = parts.join(' · ');
  if (job.output_bytes && job.source_bytes) {
    const saving = 100 * (1 - job.output_bytes / job.source_bytes);
    const savings = el('span', saving > 0 ? 'savings' : 'job-target', saving > 0 ? ` · ${Math.round(saving)}% smaller` : ` · ${Math.abs(Math.round(saving))}% larger than source`);
    meta.append(savings);
  }
  if (job.target_mb) meta.append(el('span', 'job-target', ` · Target ${job.target_mb} MB`));
  const state = row.querySelector('.job-state');
  state.className = `job-state ${job.status}`;
  state.textContent = job.status === 'completed' ? 'Ready' : job.status === 'running' ? 'Processing' : job.status === 'queued' ? 'Queued' : job.status === 'failed' ? 'Failed' : 'Cancelled';
  row.querySelector('.job-progress').hidden = !active;
  row.querySelector('.progress-fill').style.width = `${job.progress}%`;
  row.querySelector('.progress-track').setAttribute('aria-valuenow', String(job.progress));
  row.querySelector('.progress-track').setAttribute('aria-label', `Progress for ${job.filename}`);
  row.querySelector('.stage').textContent = job.stage;
  if (job.status === 'running' && job.stage.startsWith('Downloading segments') && job.download_speed) {
    row.querySelector('.stage').textContent += ` · ${bytes(job.download_speed)}/s`;
  }
  row.querySelector('.percent').textContent = `${Math.round(job.progress)}%`;
  row.querySelector('.job-error').hidden = !job.error;
  row.querySelector('.job-error').textContent = job.error || '';
  // Preserve focused actions and open confirmation while progress changes.
  if (row.dataset.status !== job.status) {
    const actions = row.querySelector('.job-actions');
    actions.replaceChildren();
    if (job.status === 'completed') {
      actions.append(action('Preview', 'play', () => {
        $('#preview-title').textContent = job.filename;
        $('#preview-video').src = `/api/jobs/${job.id}/file?preview=true`;
        $('#preview-dialog').showModal();
      }));
      const link = el('a', 'secondary-button');
      link.href = `/api/jobs/${job.id}/file`;
      link.download = job.filename;
      link.innerHTML = svg('download');
      link.append(document.createTextNode('Download'));
      actions.append(link);
    }
    if (active) actions.append(action('Cancel', null, async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }); await refresh(); }
      catch (error) { formError(error.message); button.disabled = false; }
    }));
    else actions.append(action(`Remove ${job.filename}`, 'trash', () => { row.querySelector('.delete-confirm').hidden = false; }, 'icon-button'));
    row.dataset.status = job.status;
  }
}
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const jobs = await api('/api/jobs');
    const ids = new Set(jobs.map((job) => job.id));
    for (const [id, row] of rows) if (!ids.has(id)) { row.remove(); rows.delete(id); }
    for (const job of [...jobs].reverse()) { if (!rows.has(job.id)) addRow(job); else updateRow(job); }
    const count = jobs.filter((job) => job.status === 'completed').length;
    const processing = jobs.filter((job) => ['running', 'queued'].includes(job.status)).length;
    $('#file-count').textContent = `${count} ${count === 1 ? 'file' : 'files'}${processing ? ` · ${processing} processing` : ''}`;
    $('#empty-state').hidden = jobs.length > 0;
    $('#connection-message').hidden = true;
  } catch {
    $('#connection-message').textContent = 'Cannot reach the local app. Make sure it is running; reconnection is automatic.';
    $('#connection-message').hidden = false;
  } finally { refreshing = false; }
}
$('#close-preview').addEventListener('click', () => $('#preview-dialog').close());
$('#preview-dialog').addEventListener('close', () => { $('#preview-video').pause(); $('#preview-video').removeAttribute('src'); $('#preview-video').load(); });
$('#preview-dialog').addEventListener('click', (event) => { if (event.target === $('#preview-dialog')) $('#preview-dialog').close(); });
syncOptions();
$('#browser-agent').addEventListener('click', () => { $('#user-agent').value = navigator.userAgent; });
refresh();
setInterval(refresh, 1200);
api('/api/health').then((health) => { if (!health.ffmpeg) formError('Install FFmpeg on this machine and restart the app to process video.'); }).catch(() => {});
