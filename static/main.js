const newJobsList = document.getElementById('newJobsList');
const template = document.getElementById('newJobTemplate');
const uartTemplate = document.getElementById('uartTemplate');
const waitingJobs = document.getElementById('waitingJobs');
const recentJobs = document.getElementById('recentJobs');
const form = document.getElementById('newJobsForm');
const jobsDurationMinutes = document.getElementById('jobsDurationMinutes');
const autoFinishEnabled = document.getElementById('autoFinishEnabled');
let currentUser = 'user';
let currentUserId = '0';

function makeJobsId() {
  const now = new Date();
  const pad = (v) => String(v).padStart(2, '0');
  const ts = `${pad(now.getFullYear() % 100)}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `${currentUserId}_${ts}`;
}

function addUartItem(card, value = '') {
  const uartList = card.querySelector('.uart-list');
  const item = uartTemplate.content.firstElementChild.cloneNode(true);
  const input = item.querySelector('.uart-input');
  input.value = value;
  item.querySelector('.remove-uart-btn').addEventListener('click', () => item.remove());
  uartList.appendChild(item);
}

let fileBrowserModal = null;

function ensureFileBrowserModal() {
  if (fileBrowserModal) return fileBrowserModal;

  const overlay = document.createElement('div');
  overlay.className = 'file-browser-overlay';
  overlay.innerHTML = `
    <div class="file-browser-modal">
      <div class="file-browser-head">
        <strong>Select Path</strong>
        <button type="button" class="file-browser-close">×</button>
      </div>
      <div class="file-browser-path-row">
        <input class="file-browser-path" placeholder="/path/to/search" />
        <button type="button" class="mini-btn file-browser-go">Go</button>
      </div>
      <div class="file-browser-list"></div>
      <div class="file-browser-actions">
        <button type="button" class="mini-btn file-browser-use-path">Apply</button>
        <button type="button" class="mini-btn file-browser-cancel">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.style.display = 'none';

  const close = () => {
    overlay.style.display = 'none';
    overlay.dataset.mode = '';
    overlay.dataset.targetInput = '';
  };

  overlay.querySelector('.file-browser-close').addEventListener('click', close);
  overlay.querySelector('.file-browser-cancel').addEventListener('click', close);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) close();
  });

  fileBrowserModal = {
    overlay,
    pathInput: overlay.querySelector('.file-browser-path'),
    list: overlay.querySelector('.file-browser-list'),
    goBtn: overlay.querySelector('.file-browser-go'),
    usePathBtn: overlay.querySelector('.file-browser-use-path'),
    close,
  };
  return fileBrowserModal;
}

function findParentPath(pathValue) {
  const normalized = (pathValue || '').trim();
  if (!normalized) return '';
  if (normalized === '/') return '/';

  const clean = normalized.endsWith('/') && normalized.length > 1 ? normalized.slice(0, -1) : normalized;
  const slashIndex = clean.lastIndexOf('/');
  if (slashIndex <= 0) return '/';
  return clean.slice(0, slashIndex);
}

async function loadFsEntriesWithFallback(path, mode) {
  const trimmed = (path || '').trim();
  try {
    return await loadFsEntries(trimmed, mode);
  } catch (error) {
    if (!trimmed) throw error;
    const fallbackPath = findParentPath(trimmed);
    if (!fallbackPath || fallbackPath === trimmed) throw error;
    return loadFsEntries(fallbackPath, mode);
  }
}

async function loadFsEntries(path, mode) {
  const url = `/api/fs?path=${encodeURIComponent(path || '')}&mode=${encodeURIComponent(mode)}`;
  const response = await fetch(url);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || 'load failed');
  }
  return response.json();
}

async function browseViaFileSystem(target, mode = 'file') {
  const modal = ensureFileBrowserModal();
  modal.overlay.style.display = 'flex';
  modal.overlay.dataset.mode = mode;
  modal.overlay.currentTarget = target;

  const render = async (path) => {
    modal.list.textContent = 'Loading...';
    const data = await loadFsEntriesWithFallback(path || target.value || '', mode);
    modal.pathInput.value = data.cwd;
    modal.list.innerHTML = '';

    const addEntryButton = (name, pathValue, type, className = 'fs-item') => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = className;
      item.dataset.path = pathValue;
      item.dataset.type = type;
      item.textContent = name;
      item.addEventListener('click', async () => {
        const itemPath = item.dataset.path || '';
        const itemType = item.dataset.type;
        if (itemType === 'directory') {
          await render(itemPath);
          return;
        }
        target.value = itemPath;
        modal.close();
      });
      modal.list.appendChild(item);
    };

    if (data.parent) addEntryButton('..', data.parent, 'directory', 'fs-item fs-nav');

    data.entries.forEach((entry) => {
      const prefix = entry.type === 'directory' ? '\u{1F5C2}' : '\u{1F4C4}';
      addEntryButton(`${prefix} ${entry.name}`, entry.path, entry.type);
    });

    if (!data.entries.length && !data.parent) {
      const empty = document.createElement('div');
      empty.className = 'fs-empty';
      empty.textContent = '(empty)';
      modal.list.appendChild(empty);
    }
  };

  modal.goBtn.onclick = async () => {
    await render(modal.pathInput.value);
  };
  modal.pathInput.onkeydown = async (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    await render(modal.pathInput.value);
  };
  modal.usePathBtn.onclick = () => {
    const nextValue = modal.pathInput.value.trim();
    if (!nextValue) return;
    target.value = nextValue;
    modal.close();
  };

  try {
    await render(target.value);
  } catch (error) {
    modal.list.innerHTML = '';
    const errorNode = document.createElement('div');
    errorNode.className = 'fs-error';
    errorNode.textContent = `Failed: ${error.message}`;
    modal.list.appendChild(errorNode);
  }
}

function bindFileSystemBrowse(card, btnSelector, inputSelector, mode = 'file') {
  const btn = card.querySelector(btnSelector);
  const target = card.querySelector(inputSelector);
  if (!btn || !target) return;

  btn.addEventListener('click', async () => {
    await browseViaFileSystem(target, mode);
  });
}

function updateDbConfigState(card, key, enabled) {
  const input = card.querySelector(`input[name="${key}"]`);
  if (!input) return;
  input.disabled = !enabled;
  const browseMap = {
    database_path: '.database-browse-btn',
    reset_script: '.reset-browse-btn',
    imgload_script: '.imgload-browse-btn',
  };
  const browseBtn = card.querySelector(browseMap[key]);
  if (browseBtn) browseBtn.disabled = !enabled;
}

function bindDbConfigToggles(card, prefill = {}) {
  card.querySelectorAll('.db-config-toggle').forEach((toggle) => {
    const key = toggle.dataset.target;
    const enabledFlagKey = `${key}_enabled`;
    if (typeof prefill[enabledFlagKey] === 'boolean') {
      toggle.checked = prefill[enabledFlagKey];
    }
    const initialValue = prefill[key];
    if (typeof prefill[enabledFlagKey] !== 'boolean' && initialValue === 'auto') toggle.checked = false;

    updateDbConfigState(card, key, toggle.checked);
    toggle.addEventListener('change', () => updateDbConfigState(card, key, toggle.checked));
  });
}

function normalizeUartPaths(prefill = {}) {
  const normalizeList = (values) => values.map((value) => String(value || '').trim()).filter(Boolean);

  if (Array.isArray(prefill.uart_paths)) return normalizeList(prefill.uart_paths);

  if (typeof prefill.uart_paths === 'string') {
    const text = prefill.uart_paths.trim();
    if (!text) return [];
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return normalizeList(parsed);
    } catch (_) {}
    return text.split(/[\n,;]/).map((item) => item.trim()).filter(Boolean);
  }

  if (typeof prefill.uart_path === 'string' && prefill.uart_path.trim()) return [prefill.uart_path.trim()];
  if (typeof prefill.uart === 'string' && prefill.uart.trim()) return [prefill.uart.trim()];

  const legacyUartValues = ['uart1', 'uart2', 'uart3', 'uart4', 'uart_1', 'uart_2', 'uart_3', 'uart_4']
    .map((key) => prefill[key])
    .filter((value) => typeof value === 'string' && value.trim());
  if (legacyUartValues.length) return normalizeList(legacyUartValues);

  return [];
}

function createNewJobCard(prefill = {}, insertAfterNode = null, options = {}) {
  const node = template.content.firstElementChild.cloneNode(true);
  const normalizedUartPaths = normalizeUartPaths(prefill);

  node.querySelector('input[name="jobs_id"]').value = options.regenerateJobsId ? makeJobsId() : (prefill.jobs_id || makeJobsId());
  node.querySelector('select[name="haps_platform"]').value = prefill.haps_platform || 'BJ-HAPS80';
  node.querySelector('input[name="database_path"]').value = prefill.database_path && prefill.database_path !== 'auto' ? prefill.database_path : '';
  node.querySelector('input[name="reset_script"]').value = prefill.reset_script && prefill.reset_script !== 'auto' ? prefill.reset_script : '';
  node.querySelector('input[name="imgload_script"]').value = prefill.imgload_script && prefill.imgload_script !== 'auto' ? prefill.imgload_script : '';
  node.querySelector('input[name="binfile"]').value = prefill.binfile || '';
  node.querySelector('input[name="img_file"]').value = prefill.img_file || '';
  node.querySelector('input[name="log_path"]').value = prefill.log_path || '';

  const openocdCfg = prefill.openocd_cfg || {};
  node.querySelector('input[name="openocd_tool_path"]').value = openocdCfg.tool_path || '';
  node.querySelector('input[name="openocd_cfg_file"]').value = openocdCfg.cfg_file || '';

  (normalizedUartPaths.length ? normalizedUartPaths : ['']).forEach((val) => addUartItem(node, val));

  node.querySelector('.add-uart-btn').addEventListener('click', () => addUartItem(node));
  node.querySelector('.delete-btn').addEventListener('click', () => {
    node.remove();
    if (!newJobsList.children.length) createNewJobCard();
  });
  node.querySelector('.add-btn').addEventListener('click', () => createNewJobCard({}, node));

  bindFileSystemBrowse(node, '.browse-btn', '.binfile-path', 'file');
  bindFileSystemBrowse(node, '.img-file-browse-btn', '.img-file-path', 'file');
  bindFileSystemBrowse(node, '.database-browse-btn', '.database-path', 'file');
  bindFileSystemBrowse(node, '.reset-browse-btn', '.reset-script-path', 'file');
  bindFileSystemBrowse(node, '.imgload-browse-btn', '.imgload-script-path', 'file');
  bindDbConfigToggles(node, prefill);

  if (insertAfterNode && insertAfterNode.parentNode === newJobsList) {
    insertAfterNode.insertAdjacentElement('afterend', node);
  } else {
    newJobsList.appendChild(node);
  }
}

function initJobsTimingSettings() {
  const options = [];
  for (let value = 10; value <= 240; value += 10) options.push(value);
  jobsDurationMinutes.innerHTML = options.map((value) => `<option value="${value}">${value} min</option>`).join('');
  jobsDurationMinutes.value = '10';
}

function collectNewJobs() {
  return Array.from(newJobsList.querySelectorAll('.job-card')).map((card) => {
    const uartPaths = Array.from(card.querySelectorAll('.uart-input')).map((i) => i.value.trim()).filter(Boolean);
    const dbPathEnabled = card.querySelector('.db-config-toggle[data-target="database_path"]').checked;
    const resetScriptEnabled = card.querySelector('.db-config-toggle[data-target="reset_script"]').checked;
    const imgLoadScriptEnabled = card.querySelector('.db-config-toggle[data-target="imgload_script"]').checked;
    return {
      jobs_id: card.querySelector('input[name="jobs_id"]').value.trim(),
      haps_platform: card.querySelector('select[name="haps_platform"]').value,
      database_path: dbPathEnabled ? (card.querySelector('input[name="database_path"]').value.trim() || 'auto') : 'auto',
      database_path_enabled: dbPathEnabled,
      reset_script: resetScriptEnabled ? (card.querySelector('input[name="reset_script"]').value.trim() || 'auto') : 'auto',
      reset_script_enabled: resetScriptEnabled,
      imgload_script: imgLoadScriptEnabled ? (card.querySelector('input[name="imgload_script"]').value.trim() || 'auto') : 'auto',
      imgload_script_enabled: imgLoadScriptEnabled,
      binfile: card.querySelector('input[name="binfile"]').value.trim(),
      img_file: card.querySelector('input[name="img_file"]').value.trim(),
      log_path: card.querySelector('input[name="log_path"]').value.trim(),
      openocd_cfg: {
        tool_path: card.querySelector('input[name="openocd_tool_path"]').value.trim(),
        cfg_file: card.querySelector('input[name="openocd_cfg_file"]').value.trim(),
      },
      uart_paths: uartPaths,
      duration_minutes: Number.parseInt(jobsDurationMinutes.value, 10) || 10,
      auto_finish: autoFinishEnabled.checked,
      user_id: currentUserId,
    };
  });
}

async function submitJobs(event) {
  event.preventDefault();
  const response = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jobs: collectNewJobs() }),
  });
  if (!response.ok) return alert(`Submit failed: ${await response.text()}`);
  newJobsList.innerHTML = '';
  initJobsTimingSettings();
  createNewJobCard();
  refreshRecentJobs();
  refreshWaitingJobs();
}

async function finishJob(jobId) {
  if (!window.confirm('Finish this running job?')) return;
  const response = await fetch(`/api/jobs/${jobId}/stop`, { method: 'POST' });
  if (!response.ok) return alert('Finish failed');
  refreshRecentJobs();
  refreshWaitingJobs();
}


function formatWait(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(safe / 3600);
  const m = Math.floor((safe % 3600) / 60);
  const s = safe % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

async function cancelWaitingJob(waitingId) {
  const response = await fetch(`/api/waiting-jobs/${waitingId}?user_id=${encodeURIComponent(currentUserId)}`, { method: 'DELETE' });
  if (!response.ok) return alert(`Cancel failed: ${await response.text()}`);
  refreshWaitingJobs();
}

function renderWaitingJobs(jobs) {
  waitingJobs.innerHTML = '';
  if (!jobs.length) return (waitingJobs.textContent = 'No waiting jobs');

  jobs.forEach((job) => {
    const payload = job.payload || {};
    const item = document.createElement('div');
    item.className = 'recent-card row-grid waiting-card';
    item.innerHTML = `
      <div class="kv"><span class="key">JobsID</span><span class="val">${payload.jobs_id || '-'}</span></div>
      <div class="kv"><span class="key">HAPS Platform</span><span class="val">${payload.haps_platform || '-'}</span></div>
      <div class="kv"><span class="key">Wait Time</span><span class="val">${formatWait(job.wait_seconds)}</span></div>
      <div class="kv"><span class="key">Running User</span><span class="val">${job.running_user_id || '-'}</span></div>
      <div class="actions waiting-actions"></div>
    `;

    const actions = item.querySelector('.waiting-actions');
    if ((payload.user_id || '') === currentUserId) {
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'delete-btn';
      delBtn.textContent = '×';
      delBtn.addEventListener('click', () => cancelWaitingJob(job.id));
      actions.appendChild(delBtn);
    }

    if (job.overdue) {
      const note = document.createElement('div');
      note.className = 'job-alert';
      note.textContent = `Queue time reached. Running job is not finished, you can contact user: ${job.running_user_id || '-'}.`;
      item.appendChild(note);
    }

    waitingJobs.appendChild(item);
  });
}

async function refreshWaitingJobs() {
  const response = await fetch('/api/waiting-jobs');
  if (!response.ok) return;
  const data = await response.json();
  renderWaitingJobs(data.jobs || []);
}

function renderRecentJobs(jobs) {
  recentJobs.innerHTML = '';
  if (!jobs.length) return (recentJobs.textContent = 'No jobs yet');

  jobs.forEach((job) => {
    const payload = job.payload || {};
    const item = document.createElement('div');
    item.className = 'recent-card row-grid';
    item.innerHTML = `
      <div class="kv jobid-kv"><span class="key">JobsID</span><span class="val jobid-val">${payload.jobs_id || '-'}</span></div>
      <div class="kv status-kv"><span class="key">Status</span><span class="val status ${job.status}">${job.status}</span></div>
      <div class="kv"><span class="key">HAPS Platform</span><span class="val">${payload.haps_platform || '-'}</span></div>
      <div class="kv"><span class="key">Duration</span><span class="val">${payload.duration_minutes || 0} min</span></div>
      <div class="kv"><span class="key">Endtime</span><span class="val">${job.end_time || '-'}</span></div>
      <div class="kv"><span class="key">Log Info</span><span class="val">${payload.log_info || '-'}</span></div>
      <div class="actions"></div>
    `;

    const actions = item.querySelector('.actions');
    const copyBtn = document.createElement('button');
    copyBtn.textContent = 'Copy to New Jobs';
    copyBtn.className = 'copy-btn';
    copyBtn.type = 'button';
    copyBtn.addEventListener('click', () => createNewJobCard(payload, null, { regenerateJobsId: true }));
    actions.appendChild(copyBtn);

    if (job.status === 'Runing' && String(payload.user_id || '') === currentUserId) {
      const finishBtn = document.createElement('button');
      finishBtn.textContent = 'Finish';
      finishBtn.className = 'finish-btn';
      finishBtn.type = 'button';
      finishBtn.addEventListener('click', () => finishJob(job.id));
      actions.appendChild(finishBtn);
    }

    if (job.status === 'Runing' && String(job.message || '').includes('pending finish')) {
      const alert = document.createElement('div');
      alert.className = 'job-alert';
      alert.textContent = 'Time is up: this Running Job is waiting for manual Finish.';
      item.appendChild(alert);
    }

    recentJobs.appendChild(item);
  });
}

async function refreshRecentJobs() {
  const response = await fetch('/api/jobs');
  if (!response.ok) return;
  const data = await response.json();
  renderRecentJobs(data.jobs || []);
}

async function bootstrap() {
  try {
    const sessionResp = await fetch('/api/session');
    if (sessionResp.ok) {
      const session = await sessionResp.json();
      currentUser = session.user || 'user';
      currentUserId = session.user_id || currentUserId;
    }
  } catch (_) {}

  initJobsTimingSettings();
  createNewJobCard();
  refreshRecentJobs();
  refreshWaitingJobs();
  setInterval(() => { refreshRecentJobs(); refreshWaitingJobs(); }, 2000);
}

form.addEventListener('submit', submitJobs);
bootstrap();
