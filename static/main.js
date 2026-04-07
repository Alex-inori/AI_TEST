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
const promptedTimeoutConfirmJobs = new Set();
let stopConfirmModal = null;
const expandedUartJobs = new Set();
const uartBuffers = new Map();
const uartLastLineSeen = new Map();
let uartSocket = null;
let uartPingTimer = null;
let uartReconnectTimer = null;
let hapsPlatforms = [];
let uartDevices = [];
let servicePort = 8000;
let createJobsMaxNum = 5;

function serviceBaseUrl() {
  return `http://127.0.0.1:${servicePort}`;
}
function wsBaseUrl() {
  return `ws://127.0.0.1:${servicePort}`;
}
function buildApiUrl(path) {
  return `${serviceBaseUrl()}${path}`;
}
function trimOldestCreateJobsIfNeeded(limit = createJobsMaxNum) {
  const max = Number.parseInt(limit, 10);
  if (!Number.isFinite(max) || max <= 0) return;
  const cards = Array.from(newJobsList.querySelectorAll('.job-card'));
  const overflow = cards.length - max + 1;
  if (overflow <= 0) return;
  cards.slice(0, overflow).forEach((card) => card.remove());
}

function isEditingUartInput() {
  const active = document.activeElement;
  return !!(active && active.classList && active.classList.contains('uart-column-input'));
}

function isRunningStatus(status) {
  const text = String(status || '');
  return text.startsWith('Running::');
}

function statusClassName(status) {
  const text = String(status || '');
  if (text === 'Running::Loading HAPS_DB' || text === 'Running::Loading SW_IMG' || text === 'Running::Resetting HAPS_ENV') return 'running-light';
  if (text === 'Running::HAPS_RDY') return 'running-deep';
  if (isRunningStatus(text)) return 'running-deep';
  if (text === 'Finish') return 'Finish';
  if (text === 'Stopped' || text === 'Failed') return text;
  return '';
}

function ensureUartJobDevice(jobId, device) {
  const jobKey = String(jobId || '');
  const devKey = String(device || 'unknown');
  if (!uartBuffers.has(jobKey)) uartBuffers.set(jobKey, new Map());
  const devices = uartBuffers.get(jobKey);
  if (!devices.has(devKey)) devices.set(devKey, []);
  return devices.get(devKey);
}
function escapeHtml(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
function ansiColorToCss(code) {
  const fgMap = {
    30: '#000000', 31: '#ef4444', 32: '#22c55e', 33: '#eab308', 34: '#3b82f6', 35: '#d946ef', 36: '#06b6d4', 37: '#e5e7eb',
    90: '#6b7280', 91: '#f87171', 92: '#4ade80', 93: '#facc15', 94: '#60a5fa', 95: '#e879f9', 96: '#22d3ee', 97: '#ffffff',
  };
  const bgMap = {
    40: '#000000', 41: '#7f1d1d', 42: '#14532d', 43: '#713f12', 44: '#1e3a8a', 45: '#701a75', 46: '#083344', 47: '#d1d5db',
    100: '#374151', 101: '#b91c1c', 102: '#15803d', 103: '#a16207', 104: '#1d4ed8', 105: '#a21caf', 106: '#0e7490', 107: '#f3f4f6',
  };
  if (fgMap[code]) return { type: 'fg', value: fgMap[code] };
  if (bgMap[code]) return { type: 'bg', value: bgMap[code] };
  return null;
}
function ansiToHtml(text) {
  const input = String(text || '');
  const parts = input.split(/(\x1b\[[0-9;]*m)/g);
  let fg = '';
  let bg = '';
  let bold = false;
  const html = [];
  for (const part of parts) {
    if (!part) continue;
    const match = part.match(/^\x1b\[([0-9;]*)m$/);
    if (match) {
      const codes = match[1] ? match[1].split(';').map((v) => Number(v)).filter((v) => Number.isInteger(v)) : [0];
      if (!codes.length) codes.push(0);
      codes.forEach((code) => {
        if (code === 0) { fg = ''; bg = ''; bold = false; return; }
        if (code === 1) { bold = true; return; }
        if (code === 22) { bold = false; return; }
        if (code === 39) { fg = ''; return; }
        if (code === 49) { bg = ''; return; }
        const mapped = ansiColorToCss(code);
        if (!mapped) return;
        if (mapped.type === 'fg') fg = mapped.value;
        if (mapped.type === 'bg') bg = mapped.value;
      });
      continue;
    }
    const style = [`${fg ? `color:${fg};` : ''}${bg ? `background:${bg};` : ''}${bold ? 'font-weight:700;' : ''}`]
      .join('')
      .trim();
    const escaped = escapeHtml(part);
    if (!style) html.push(escaped);
    else html.push(`<span style="${style}">${escaped}</span>`);
  }
  return html.join('');
}
function renderUartOutput(preNode, lines, waitingText) {
  const normalized = Array.isArray(lines) ? lines : [];
  if (!normalized.length) {
    preNode.textContent = waitingText;
    preNode.scrollTop = preNode.scrollHeight;
    return;
  }
  preNode.innerHTML = normalized.map((line) => ansiToHtml(line)).join('\n');
  preNode.scrollTop = preNode.scrollHeight;
}
function appendUartLine(jobId, device, line, ts) {
  const jobKey = String(jobId || '');
  const devKey = String(device || 'unknown');
  const dedupKey = `${jobKey}::${devKey}`;
  const now = Date.now();
  const prev = uartLastLineSeen.get(dedupKey);
  if (prev && prev.line === line && (now - prev.at) < 700) return;
  uartLastLineSeen.set(dedupKey, { line, at: now });

  const list = ensureUartJobDevice(jobKey, devKey);
  list.push(`[${ts}] ${line}`);
  if (list.length > 500) list.shift();
}
function consumeUartSnapshot(jobs) {
  Object.entries(jobs || {}).forEach(([jobId, byDevice]) => {
    if (!uartBuffers.has(jobId)) uartBuffers.set(jobId, new Map());
    const devices = uartBuffers.get(jobId);
    Object.entries(byDevice || {}).forEach(([device, lines]) => {
      const normalized = (lines || []).map((item) => `[${item.ts || ''}] ${item.line || ''}`);
      devices.set(device, normalized.slice(-500));
    });
  });
}
function connectUartSocket() {
  if (uartSocket && (uartSocket.readyState === WebSocket.OPEN || uartSocket.readyState === WebSocket.CONNECTING)) return;
  if (uartReconnectTimer) {
    window.clearTimeout(uartReconnectTimer);
    uartReconnectTimer = null;
  }
  const clientId = `${currentUserId || '0'}-${window.location.pathname}`;
  const socket = new WebSocket(`${wsBaseUrl()}/ws/uart?client_id=${encodeURIComponent(clientId)}`);
  uartSocket = socket;
  socket.onopen = () => {
    if (uartSocket !== socket) return;
    if (uartPingTimer) window.clearInterval(uartPingTimer);
    uartPingTimer = window.setInterval(() => {
      if (uartSocket === socket && socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 15000);
  };
  socket.onmessage = (event) => {
    if (uartSocket !== socket) return;
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'snapshot') {
        consumeUartSnapshot(msg.jobs || {});
        refreshRecentJobs();
        return;
      }
      if (msg.type !== 'line' && msg.type !== 'status') return;
      appendUartLine(msg.job_id || '', msg.device || 'unknown', msg.line || '', msg.ts || '');
      const jobCard = findRecentJobCard(msg.job_id || '');
      if (!jobCard || !expandedUartJobs.has(String(msg.job_id || ''))) return;
      const panel = jobCard.querySelector('.uart-job-console');
      if (!panel) return;
      if (!patchUartPanelLine(panel, String(msg.job_id || ''), msg.device || 'unknown')) {
        renderUartPanel(panel, String(msg.job_id || ''), []);
      }
    } catch (_) {}
  };
  socket.onclose = () => {
    if (uartSocket !== socket) return;
    if (uartPingTimer) {
      window.clearInterval(uartPingTimer);
      uartPingTimer = null;
    }
    uartSocket = null;
    uartReconnectTimer = window.setTimeout(() => {
      uartReconnectTimer = null;
      connectUartSocket();
    }, 1500);
  };
}
function sendUartInput(jobId, device, content, appendNewline = true) {
  if (!uartSocket || uartSocket.readyState !== WebSocket.OPEN) return false;
  uartSocket.send(JSON.stringify({
    type: 'uart_input',
    job_id: String(jobId || ''),
    device: String(device || ''),
    content: String(content || ''),
    append_newline: !!appendNewline,
  }));
  return true;
}
function renderUartPanel(panel, jobId, uartPaths) {
  const devicesMap = uartBuffers.get(String(jobId)) || new Map();
  const sourceDevices = [...new Set([...(uartPaths || []), ...Array.from(devicesMap.keys())].map((v) => String(v || '').trim()).filter(Boolean))];
  panel.innerHTML = '';
  if (!sourceDevices.length) {
    panel.textContent = 'No UART device found in this job.';
    return;
  }
  const grid = document.createElement('div');
  grid.className = 'uart-columns';
  grid.style.gridTemplateColumns = `repeat(${Math.min(2, sourceDevices.length)}, minmax(300px, 1fr))`;
  sourceDevices.forEach((device, index) => {
    const column = document.createElement('div');
    column.className = 'uart-column';
    column.dataset.device = device;
    const isOddLast = sourceDevices.length > 1 && (sourceDevices.length % 2 === 1) && index === sourceDevices.length - 1;
    if (isOddLast) column.style.gridColumn = '1 / -1';
    const title = document.createElement('div');
    title.className = 'uart-column-title';
    title.textContent = device;
    const pre = document.createElement('pre');
    pre.className = 'uart-column-output';
    pre.dataset.device = device;
    const lines = devicesMap.get(device) || [];
    renderUartOutput(pre, lines, `Waiting output from ${device} ...`);
    const inputRow = document.createElement('div');
    inputRow.className = 'uart-column-input-row';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'uart-column-input';
    input.placeholder = 'Input and press Enter / Send';
    const sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'uart-column-send-btn';
    sendBtn.textContent = '\u23CE';
    const submitInput = () => {
      const value = input.value;
      if (!value) return;
      const sent = sendUartInput(jobId, device, value, true);
      if (!sent) {
        appendUartLine(String(jobId), device, '[UI] UART socket not connected; input not sent', new Date().toISOString().slice(0, 19));
        patchUartPanelLine(panel, String(jobId), device);
        return;
      }
      input.value = '';
    };
    sendBtn.addEventListener('click', submitInput);
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      submitInput();
    });
    inputRow.appendChild(input);
    inputRow.appendChild(sendBtn);
    column.appendChild(title);
    column.appendChild(pre);
    column.appendChild(inputRow);
    grid.appendChild(column);
  });
  panel.appendChild(grid);
  window.requestAnimationFrame(() => {
    panel.querySelectorAll('.uart-column-output').forEach((node) => {
      node.scrollTop = node.scrollHeight;
    });
  });
}

function patchUartPanelLine(panel, jobId, device) {
  const targetDevice = String(device || 'unknown');
  const pre = panel.querySelector(`.uart-column-output[data-device="${CSS.escape(targetDevice)}"]`);
  if (!pre) return false;
  const devicesMap = uartBuffers.get(String(jobId)) || new Map();
  const lines = devicesMap.get(targetDevice) || [];
  renderUartOutput(pre, lines, `Waiting output from ${targetDevice} ...`);
  return true;
}
function findRecentJobCard(jobId) {
  const targetId = String(jobId);
  const cards = recentJobs.querySelectorAll('.recent-card[data-job-id]');
  for (const card of cards) {
    if (card.dataset.jobId === targetId) return card;
  }
  return null;
}
function positionStopConfirmModal(jobId) {
  const modal = ensureStopConfirmModal();
  const card = findRecentJobCard(jobId);
  if (!card) {
    const modalRect = modal.modalBox.getBoundingClientRect();
    const top = Math.max(12, (window.innerHeight - modalRect.height) / 2);
    const left = Math.max(12, (window.innerWidth - modalRect.width) / 2);
    modal.modalBox.style.top = `${top}px`;
    modal.modalBox.style.left = `${left}px`;
    return;
  }
  card.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const place = () => {
    const rect = card.getBoundingClientRect();
    const modalRect = modal.modalBox.getBoundingClientRect();
    const gap = 10;
    let top = Math.max(12, Math.min(rect.top, window.innerHeight - modalRect.height - 12));
    let left = rect.right + gap;
    if (left + modalRect.width > window.innerWidth - 12) left = rect.left - modalRect.width - gap;
    if (left < 12) left = Math.max(12, Math.min(rect.left, window.innerWidth - modalRect.width - 12));
    modal.modalBox.style.top = `${top}px`;
    modal.modalBox.style.left = `${left}px`;
  };
  place();
  window.requestAnimationFrame(() => {
    place();
    window.setTimeout(place, 80);
  });
}
function ensureStopConfirmModal() {
  if (stopConfirmModal) return stopConfirmModal;
  const overlay = document.createElement('div');
  overlay.className = 'stop-confirm-overlay';
  overlay.innerHTML = `
    <div class="stop-confirm-modal">
      <div class="stop-confirm-title">Running Jobs Confirmation</div>
      <div class="stop-confirm-message"></div>
      <div class="stop-confirm-countdown"></div>
      <div class="stop-confirm-actions">
        <button type="button" class="finish-btn stop-confirm-ok">Confirm</button>
        <button type="button" class="copy-btn stop-confirm-cancel">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.style.display = 'none';
  stopConfirmModal = {
    overlay,
    modalBox: overlay.querySelector('.stop-confirm-modal'),
    message: overlay.querySelector('.stop-confirm-message'),
    countdown: overlay.querySelector('.stop-confirm-countdown'),
    okBtn: overlay.querySelector('.stop-confirm-ok'),
    cancelBtn: overlay.querySelector('.stop-confirm-cancel'),
    timerId: null,
    intervalId: null,
    handleViewportChange: null,
  };
  return stopConfirmModal;
}
function closeStopConfirmModal() {
  const modal = ensureStopConfirmModal();
  modal.overlay.style.display = 'none';
  modal.overlay.dataset.jobId = '';
  if (modal.timerId) {
    window.clearTimeout(modal.timerId);
    modal.timerId = null;
  }
  if (modal.intervalId) {
    window.clearInterval(modal.intervalId);
    modal.intervalId = null;
  }
  if (modal.handleViewportChange) {
    window.removeEventListener('resize', modal.handleViewportChange);
    window.removeEventListener('scroll', modal.handleViewportChange, true);
    modal.handleViewportChange = null;
  }
}
function resolveStopDeadline(job) {
  if (!job) return Date.now() + 5 * 60 * 1000;
  const payload = job.payload || {};
  const durationMinutes = Number(payload.duration_minutes || 0);
  const submitAt = Date.parse(job.submit_time || '');
  if (!Number.isFinite(durationMinutes) || durationMinutes <= 0 || Number.isNaN(submitAt)) {
    return Date.now() + 5 * 60 * 1000;
  }
  const timeoutAt = submitAt + durationMinutes * 60 * 1000;
  const messageText = String(job.message || '');
  if (messageText.includes('Unconfirmed Stop in 5 minutes')) return timeoutAt + 5 * 60 * 1000;
  return timeoutAt;
}
function getRemainingSecondsToTimeout(job) {
  if (!job) return null;
  const payload = job.payload || {};
  const durationMinutes = Number(payload.duration_minutes || 0);
  const submitAt = Date.parse(job.submit_time || '');
  if (!Number.isFinite(durationMinutes) || durationMinutes <= 0 || Number.isNaN(submitAt)) {
    return null;
  }
  const timeoutAt = submitAt + durationMinutes * 60 * 1000;
  return Math.floor((timeoutAt - Date.now()) / 1000);
}
function needsStopConfirmReminder(job) {
  if (!job || !isRunningStatus(job.status)) return false;
  if (job.stop_confirmed) return false;
  const payload = job.payload || {};
  if (Boolean(payload.auto_finish)) return false;
  const remainingSeconds = getRemainingSecondsToTimeout(job);
  if (remainingSeconds == null) return false;
  return remainingSeconds > 0 && remainingSeconds <= 5 * 60;
}
function showStopConfirmModal(job) {
  const modal = ensureStopConfirmModal();
  const jobId = job && job.id;
  const deadline = resolveStopDeadline(job);
  modal.overlay.dataset.jobId = String(jobId);
  modal.message.textContent = 'Running Jobs will finish in 5mins, PLS Confirm!!!';
  const updateCountdown = () => {
    const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
    const ss = String(seconds % 60).padStart(2, '0');
    modal.countdown.textContent = `Auto cancel in ${mm}:${ss}`;
  };
  updateCountdown();
  modal.overlay.style.display = 'block';
  positionStopConfirmModal(jobId);
  if (modal.handleViewportChange) {
    window.removeEventListener('resize', modal.handleViewportChange);
    window.removeEventListener('scroll', modal.handleViewportChange, true);
  }
  modal.handleViewportChange = () => positionStopConfirmModal(jobId);
  window.addEventListener('resize', modal.handleViewportChange);
  window.addEventListener('scroll', modal.handleViewportChange, true);
  modal.cancelBtn.onclick = () => closeStopConfirmModal();
  modal.okBtn.onclick = async () => {
    const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/confirm-stop`), { method: 'POST' });
    if (!response.ok) {
      alert(`Confirm Fail: ${await response.text()}`);
      return;
    }
    closeStopConfirmModal();
    refreshRecentJobs();
    refreshWaitingJobs();
  };
  modal.intervalId = window.setInterval(updateCountdown, 1000);
  modal.timerId = window.setTimeout(() => closeStopConfirmModal(), 5 * 60 * 1000);
}
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
  const options = Array.isArray(uartDevices) ? uartDevices.map((v) => String(v || '').trim()).filter(Boolean) : [];
  input.innerHTML = options.map((device) => `<option value="${device}">${device}</option>`).join('');
  if (!options.length) {
    input.innerHTML = '<option value="">No UART_DEVICE config</option>';
    input.value = '';
    input.disabled = true;
  } else {
    const normalized = String(value || '').trim();
    input.value = options.includes(normalized) ? normalized : options[0];
  }
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
  const url = buildApiUrl(`/api/fs?path=${encodeURIComponent(path || '')}&mode=${encodeURIComponent(mode)}`);
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
    updateDbConfigState(card, key, toggle.checked);
    toggle.addEventListener('change', () => updateDbConfigState(card, key, toggle.checked));
  });
}
function applyPlatformOptions(selectNode, selectedValue = '') {
  if (!selectNode) return;
  const options = Array.isArray(hapsPlatforms) ? hapsPlatforms.filter((v) => String(v || '').trim()) : [];
  selectNode.innerHTML = options.map((item) => `<option value="${item}">${item}</option>`).join('');
  if (!options.length) {
    selectNode.innerHTML = '<option value="">No platform config</option>';
    selectNode.value = '';
    return;
  }
  const normalizedSelected = String(selectedValue || '').trim();
  selectNode.value = options.includes(normalizedSelected) ? normalizedSelected : options[0];
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
  trimOldestCreateJobsIfNeeded(createJobsMaxNum);
  const node = template.content.firstElementChild.cloneNode(true);
  const normalizedUartPaths = normalizeUartPaths(prefill);
  node.querySelector('input[name="jobs_id"]').value = options.regenerateJobsId ? makeJobsId() : (prefill.jobs_id || makeJobsId());
  applyPlatformOptions(node.querySelector('select[name="haps_platform"]'), prefill.haps_platform || '');
  node.querySelector('input[name="database_path"]').value = prefill.database_path || '';
  node.querySelector('input[name="reset_script"]').value = prefill.reset_script || '';
  node.querySelector('input[name="imgload_script"]').value = prefill.imgload_script || '';
  node.querySelector('input[name="binfile"]').value = prefill.binfile || '';
  node.querySelector('input[name="img_file"]').value = prefill.img_file || '';
  const openocdCfg = prefill.openocd_cfg || {};
  node.querySelector('input[name="openocd_tool_path"]').value = openocdCfg.tool_path || '';
  node.querySelector('input[name="openocd_cfg_file"]').value = openocdCfg.cfg_file || '';
  (normalizedUartPaths.length ? normalizedUartPaths : ['']).forEach((val) => addUartItem(node, val));
  node.querySelector('.add-uart-btn').addEventListener('click', () => addUartItem(node));
  node.querySelector('.delete-btn').addEventListener('click', () => {
    node.remove();
    if (!newJobsList.children.length) createNewJobCard();
  });
  node.querySelector('.add-btn').addEventListener('click', () => {
    createNewJobCard({}, node);
  });
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
  options.push({ value: 'longtime', label: 'longtime' });
  jobsDurationMinutes.innerHTML = options
    .map((option) => {
      if (typeof option === 'number') return `<option value="${option}">${option} min</option>`;
      return `<option value="${option.value}">${option.label}</option>`;
    })
    .join('');
  jobsDurationMinutes.value = '10';
}
function parseSelectedDurationMinutes(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'longtime') return 0;
  const parsed = Number.parseInt(raw, 10);
  if (Number.isFinite(parsed) && parsed > 0) return parsed;
  return 10;
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
      database_path: dbPathEnabled ? card.querySelector('input[name="database_path"]').value.trim() : '',
      database_path_enabled: dbPathEnabled,
      reset_script: resetScriptEnabled ? card.querySelector('input[name="reset_script"]').value.trim() : '',
      reset_script_enabled: resetScriptEnabled,
      imgload_script: imgLoadScriptEnabled ? card.querySelector('input[name="imgload_script"]').value.trim() : '',
      imgload_script_enabled: imgLoadScriptEnabled,
      binfile: card.querySelector('input[name="binfile"]').value.trim(),
      img_file: card.querySelector('input[name="img_file"]').value.trim(),
      openocd_cfg: {
        tool_path: card.querySelector('input[name="openocd_tool_path"]').value.trim(),
        cfg_file: card.querySelector('input[name="openocd_cfg_file"]').value.trim(),
      },
      uart_paths: uartPaths,
      duration_minutes: parseSelectedDurationMinutes(jobsDurationMinutes.value),
      auto_finish: autoFinishEnabled.checked,
      user_id: currentUserId,
    };
  });
}
function validateJobsBeforeSubmit(jobs) {
  const duplicateUarts = new Set();
  const usedUarts = new Set();
  const tclRegex = /\.tcl$/i;
  const imgRegex = /\.(img|bin)$/i;

  jobs.forEach((job) => {
    if (job.database_path_enabled && !job.database_path) {
      throw new Error(`Job ${job.jobs_id || '-'}: DataBase Path is enabled but empty.`);
    }
    if (job.reset_script_enabled) {
      if (job.reset_script && !tclRegex.test(job.reset_script)) throw new Error(`Job ${job.jobs_id || '-'}: Reset Script must be a .tcl file.`);
    }
    if (job.imgload_script_enabled) {
      if (job.imgload_script && !tclRegex.test(job.imgload_script)) throw new Error(`Job ${job.jobs_id || '-'}: ImgLoad Script must be a .tcl file.`);
      if (!job.database_path_enabled) throw new Error(`Job ${job.jobs_id || '-'}: ImgLoad Script Path requires DataBase Path enabled.`);
      if (!job.reset_script_enabled) throw new Error(`Job ${job.jobs_id || '-'}: ImgLoad Script Path requires Reset Script Path enabled.`);
      if (!job.img_file) throw new Error(`Job ${job.jobs_id || '-'}: ImgLoad Script Path is enabled but IMG File path is empty.`);
      if (!imgRegex.test(job.img_file)) throw new Error(`Job ${job.jobs_id || '-'}: IMG File path must be a .img or .bin file.`);
    }

    const localSet = new Set();
    (job.uart_paths || []).forEach((uart) => {
      const path = String(uart || '').trim();
      if (!path) return;
      if (Array.isArray(uartDevices) && uartDevices.length && !uartDevices.includes(path)) {
        throw new Error(`Job ${job.jobs_id || '-'}: UART device not supported: ${path}`);
      }
      if (localSet.has(path)) duplicateUarts.add(path);
      localSet.add(path);
      if (usedUarts.has(path)) duplicateUarts.add(path);
      usedUarts.add(path);
    });
  });

  if (duplicateUarts.size) {
    throw new Error(`Duplicate UART path detected: ${Array.from(duplicateUarts).join(', ')}`);
  }
}
async function submitJobs(event) {
  event.preventDefault();
  const jobs = collectNewJobs();
  try {
    validateJobsBeforeSubmit(jobs);
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
    return;
  }
  const response = await fetch(buildApiUrl('/api/jobs'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jobs }),
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
  const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/stop`), { method: 'POST' });
  if (!response.ok) return alert('Finish failed');
  refreshRecentJobs();
  refreshWaitingJobs();
}
async function stopAndResubmitJob(jobId) {
  if (!window.confirm('Stop current submit and resubmit this job?')) return;
  const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/stop-and-resubmit`), { method: 'POST' });
  if (!response.ok) return alert(`Stop and Resubmit failed: ${await response.text()}`);
  refreshRecentJobs();
  refreshWaitingJobs();
}
async function openRunningJobTerminal(jobId) {
  const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/open-terminal`), { method: 'POST' });
  if (!response.ok) {
    try {
      const detail = await response.text();
      alert(`Open Terminal failed: ${detail}`);
    } catch (_) {}
    return;
  }
  let data = null;
  try {
    data = await response.json();
  } catch (_) {}
  const launchUrl = String((data && data.launch_url) || '').trim();
  if (!launchUrl) {
    alert('Open Terminal failed: launch_url is empty, please check TERMINAL in cfgshell.conf.');
    return;
  }
  const link = document.createElement('a');
  link.href = launchUrl;
  link.target = '_self';
  link.rel = 'noopener noreferrer';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  window.setTimeout(() => link.remove(), 0);
}
function formatWait(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(safe / 3600);
  const m = Math.floor((safe % 3600) / 60);
  const s = safe % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}
function buildLeftTimeText(job, payload) {
  if (!isRunningStatus(job.status)) return { text: '', isLongtime: false };
  const durationMinutes = Number.parseInt(payload.duration_minutes, 10) || 0;
  if (durationMinutes <= 0) return { text: 'longtime', isLongtime: true };
  const recentSince = Date.parse(job.submit_time || '');
  if (!Number.isFinite(recentSince)) return { text: '-', isLongtime: false };
  const endAt = recentSince + durationMinutes * 60 * 1000;
  const leftMs = endAt - Date.now();
  const leftMinutes = Math.max(0, Math.ceil(leftMs / 60000));
  return { text: `${leftMinutes} min`, isLongtime: false };
}
async function cancelWaitingJob(waitingId) {
  const response = await fetch(buildApiUrl(`/api/waiting-jobs/${waitingId}?user_id=${encodeURIComponent(currentUserId)}`), { method: 'DELETE' });
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
    `;
    if (String(payload.user_id || '') === String(currentUserId || '')) {
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'delete-btn waiting-delete-btn';
      delBtn.textContent = '×';
      delBtn.title = 'Delete waiting job';
      delBtn.addEventListener('click', () => cancelWaitingJob(job.id));
      item.appendChild(delBtn);
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
  const response = await fetch(buildApiUrl('/api/waiting-jobs'));
  if (!response.ok) return;
  const data = await response.json();
  renderWaitingJobs(data.jobs || []);
}
function renderRecentJobs(jobs) {
  recentJobs.innerHTML = '';
  if (!jobs.length) return (recentJobs.textContent = 'No jobs yet');
  jobs.forEach((job) => {
    const payload = job.payload || {};
    const running = isRunningStatus(job.status);
    const leftTime = running ? buildLeftTimeText(job, payload) : null;
    const leftTimeClass = leftTime && leftTime.isLongtime ? 'val lefttime-longtime' : 'val';
    const leftTimeHtml = running
      ? `<div class="kv lefttime-kv"><span class="key">Left Time</span><span class="${leftTimeClass}">${leftTime ? leftTime.text : ''}</span></div>`
      : '';
    const item = document.createElement('div');
    item.className = 'recent-card row-grid';
    if (!running) item.classList.add('no-lefttime');
    item.dataset.jobId = String(job.id);
    item.innerHTML = `
      <div class="kv jobid-kv"><span class="key">JobsID</span><span class="val jobid-val">${payload.jobs_id || '-'}</span></div>
      <div class="kv status-kv"><span class="key">Status</span><span class="val status ${statusClassName(job.status)}">${job.status}</span></div>
      <div class="kv"><span class="key">HAPS Platform</span><span class="val">${payload.haps_platform || '-'}</span></div>
      ${leftTimeHtml}
      <div class="kv endtime-kv"><span class="key">Endtime</span><span class="val">${job.end_time || '-'}</span></div>
      <div class="kv loginfo-kv"><span class="key">Log Info</span><span class="val">${payload.log_info || '-'}</span></div>
      <div class="actions"></div>
    `;
    const actions = item.querySelector('.actions');
    actions.style.display = 'flex';
    actions.style.flexDirection = 'column';
    actions.style.alignItems = 'stretch';
    actions.style.justifyContent = 'flex-start';
    actions.style.gap = '8px';
    actions.style.width = '180px';
    const iconActionRow = document.createElement('div');
    iconActionRow.className = 'action-icon-row';
    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn copy-icon-btn icon-only-btn';
    copyBtn.type = 'button';
    copyBtn.title = 'Copy to New Jobs';
    copyBtn.setAttribute('aria-label', 'Copy to New Jobs');
    copyBtn.innerHTML = `
      <svg class="copy-icon action-svg-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512" aria-hidden="true">
        <path d="M192 0c-35.3 0-64 28.7-64 64l0 256c0 35.3 28.7 64 64 64l192 0c35.3 0 64-28.7 64-64l0-200.6c0-17.4-7.1-34.1-19.7-46.2L370.6 17.8C358.7 6.4 342.8 0 326.3 0L192 0zM64 128c-35.3 0-64 28.7-64 64L0 448c0 35.3 28.7 64 64 64l192 0c35.3 0 64-28.7 64-64l0-16-64 0 0 16-192 0 0-256 16 0 0-64-16 0z"/>
      </svg>`;
    copyBtn.addEventListener('click', () => createNewJobCard(payload, null, { regenerateJobsId: true }));
    iconActionRow.appendChild(copyBtn);
    actions.appendChild(iconActionRow);
    const jobUartPaths = Array.isArray(payload.uart_paths) ? payload.uart_paths : [];
    const isOwner = String(payload.user_id || '') === String(currentUserId || '');
    if (jobUartPaths.length && isOwner) {
      const uartBtn = document.createElement('button');
      const expanded = expandedUartJobs.has(String(job.id));
      uartBtn.className = `copy-btn uart-icon-btn icon-only-btn${expanded ? ' active' : ''}`;
      uartBtn.type = 'button';
      const uartLabel = expanded ? 'Hide UART Console' : 'Open UART Console';
      uartBtn.title = uartLabel;
      uartBtn.setAttribute('aria-label', uartLabel);
      uartBtn.innerHTML = `
        <svg class="uart-icon action-svg-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
          <path d="M2.23177 10.3862C1.82042 8.65816 3.13073 6.99933 4.90701 6.99933H19.0915C20.8685 6.99933 22.1789 8.65932 21.7664 10.3877L20.6921 14.889C20.3966 16.1271 19.2901 17.0007 18.0172 17.0007H5.97854C4.70507 17.0007 3.5982 16.1263 3.30329 14.8875L2.23177 10.3862ZM7.00003 11.5C7.41424 11.5 7.75003 11.1642 7.75003 10.75C7.75003 10.3358 7.41424 10 7.00003 10C6.58582 10 6.25003 10.3358 6.25003 10.75C6.25003 11.1642 6.58582 11.5 7.00003 11.5ZM10.25 10.75C10.25 10.3358 9.91424 10 9.50003 10C9.08582 10 8.75003 10.3358 8.75003 10.75C8.75003 11.1642 9.08582 11.5 9.50003 11.5C9.91424 11.5 10.25 11.1642 10.25 10.75ZM8.25003 14C8.66424 14 9.00003 13.6642 9.00003 13.25C9.00003 12.8358 8.66424 12.5 8.25003 12.5C7.83582 12.5 7.50003 12.8358 7.50003 13.25C7.50003 13.6642 7.83582 14 8.25003 14ZM11.5 13.25C11.5 12.8358 11.1642 12.5 10.75 12.5C10.3358 12.5 10 12.8358 10 13.25C10 13.6642 10.3358 14 10.75 14C11.1642 14 11.5 13.6642 11.5 13.25ZM13.25 14C13.6642 14 14 13.6642 14 13.25C14 12.8358 13.6642 12.5 13.25 12.5C12.8358 12.5 12.5 12.8358 12.5 13.25C12.5 13.6642 12.8358 14 13.25 14ZM16.5 13.25C16.5 12.8358 16.1642 12.5 15.75 12.5C15.3358 12.5 15 12.8358 15 13.25C15 13.6642 15.3358 14 15.75 14C16.1642 14 16.5 13.6642 16.5 13.25ZM12 11.5C12.4142 11.5 12.75 11.1642 12.75 10.75C12.75 10.3358 12.4142 10 12 10C11.5858 10 11.25 10.3358 11.25 10.75C11.25 11.1642 11.5858 11.5 12 11.5ZM15.25 10.75C15.25 10.3358 14.9142 10 14.5 10C14.0858 10 13.75 10.3358 13.75 10.75C13.75 11.1642 14.0858 11.5 14.5 11.5C14.9142 11.5 15.25 11.1642 15.25 10.75ZM17 11.5C17.4142 11.5 17.75 11.1642 17.75 10.75C17.75 10.3358 17.4142 10 17 10C16.5858 10 16.25 10.3358 16.25 10.75C16.25 11.1642 16.5858 11.5 17 11.5Z" fill="#212121"/>
        </svg>`;
      uartBtn.addEventListener('click', () => {
        const key = String(job.id);
        if (expandedUartJobs.has(key)) expandedUartJobs.delete(key);
        else expandedUartJobs.add(key);
        refreshRecentJobs();
      });
      iconActionRow.appendChild(uartBtn);
    }
    if (running) {
      if (isOwner) {
        const terminalBtn = document.createElement('button');
        terminalBtn.className = 'copy-btn terminal-icon-btn icon-only-btn';
        terminalBtn.type = 'button';
        terminalBtn.title = 'Open Terminal';
        terminalBtn.setAttribute('aria-label', 'Open Terminal');
        terminalBtn.innerHTML = `
          <svg class="terminal-icon action-svg-icon" viewBox="0 0 512 512" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
            <rect x="32" y="64" width="448" height="384" rx="64" fill="#3F4E63"/>
            <polyline points="150,200 230,280 150,360" fill="none" stroke="#E5E7EB" stroke-width="32" stroke-linecap="round" stroke-linejoin="round"/>
            <line x1="260" y1="340" x2="360" y2="340" stroke="#E5E7EB" stroke-width="32" stroke-linecap="round"/>
          </svg>`;
        terminalBtn.addEventListener('click', () => openRunningJobTerminal(job.id));
        iconActionRow.appendChild(terminalBtn);
        const stopAndResubmitBtn = document.createElement('button');
        stopAndResubmitBtn.className = 'copy-btn stop-resubmit-icon-btn icon-only-btn';
        stopAndResubmitBtn.type = 'button';
        stopAndResubmitBtn.title = 'Stop and Resubmit';
        stopAndResubmitBtn.setAttribute('aria-label', 'Stop and Resubmit');
        stopAndResubmitBtn.innerHTML = `
          <svg class="stop-resubmit-icon action-svg-icon" viewBox="0 0 512 512" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
            <path d="M65.9 228.5c13.3-93 93.4-164.5 190.1-164.5 53 0 101 21.5 135.8 56.2 .2 .2 .4 .4 .6 .6l7.6 7.2-47.9 0c-17.7 0-32 14.3-32 32s14.3 32 32 32l128 0c17.7 0 32-14.3 32-32l0-128c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 53.4-11.3-10.7C390.5 28.6 326.5 0 256 0 127 0 20.3 95.4 2.6 219.5 .1 237 12.2 253.2 29.7 255.7s33.7-9.7 36.2-27.1zm443.5 64c2.5-17.5-9.7-33.7-27.1-36.2s-33.7 9.7-36.2 27.1c-13.3 93-93.4 164.5-190.1 164.5-53 0-101-21.5-135.8-56.2-.2-.2-.4-.4-.6-.6l-7.6-7.2 47.9 0c17.7 0 32-14.3 32-32s-14.3-32-32-32L32 320c-8.5 0-16.7 3.4-22.7 9.5S-.1 343.7 0 352.3l1 127c.1 17.7 14.6 31.9 32.3 31.7S65.2 496.4 65 478.7l-.4-51.5 10.7 10.1c46.3 46.1 110.2 74.7 180.7 74.7 129 0 235.7-95.4 253.4-219.5z"/>
          </svg>`;
        stopAndResubmitBtn.addEventListener('click', () => stopAndResubmitJob(job.id));
        iconActionRow.appendChild(stopAndResubmitBtn);
        const finishBtn = document.createElement('button');
        finishBtn.textContent = 'Finish';
        finishBtn.className = 'finish-btn';
        finishBtn.type = 'button';
        finishBtn.style.width = '100%';
        finishBtn.addEventListener('click', () => finishJob(job.id));
        actions.appendChild(finishBtn);
      }
      if (isOwner && needsStopConfirmReminder(job) && !promptedTimeoutConfirmJobs.has(job.id)) {
        promptedTimeoutConfirmJobs.add(job.id);
        window.setTimeout(async () => {
          showStopConfirmModal(job);
        }, 0);
      }
    }
    if (isRunningStatus(job.status) && String(job.message || '').includes('Unconfirmed Stop in 5 minutes')) {
      const alert = document.createElement('div');
      alert.className = 'job-alert';
      alert.textContent = 'Only 5 minutes left. Please confirm in popup whether jobs can end on time.';
      item.appendChild(alert);
    }
    if (isRunningStatus(job.status) && String(job.message || '').includes('Unconfirmed Stop in 5 minutes')) {
      const alert = document.createElement('div');
      alert.className = 'job-alert';
      alert.textContent = 'Unconfirmed Stop in 5 minutes';
      item.appendChild(alert);
    }
    if (isRunningStatus(job.status) && String(job.message || '').includes('pending finish')) {
      const alert = document.createElement('div');
      alert.className = 'job-alert';
      alert.textContent = 'Time is up: this Running Job is waiting for manual Finish.';
      item.appendChild(alert);
    }
    if (jobUartPaths.length && isOwner && expandedUartJobs.has(String(job.id))) {
      const panel = document.createElement('div');
      panel.className = 'uart-job-console';
      panel.style.gridColumn = '1 / -1';
      renderUartPanel(panel, String(job.id), jobUartPaths);
      item.appendChild(panel);
    }
    recentJobs.appendChild(item);
  });
}
async function refreshRecentJobs() {
  const response = await fetch(buildApiUrl('/api/jobs'));
  if (!response.ok) return;
  const data = await response.json();
  const jobs = data.jobs || [];
  const runningIds = new Set(jobs.filter((job) => isRunningStatus(job.status)).map((job) => job.id));
  Array.from(promptedTimeoutConfirmJobs).forEach((jobId) => {
    if (!runningIds.has(jobId)) promptedTimeoutConfirmJobs.delete(jobId);
  });
  const modal = ensureStopConfirmModal();
  const currentModalJobId = modal.overlay.dataset.jobId;
  if (modal.overlay.style.display !== 'none' && currentModalJobId) {
    const targetJob = jobs.find((job) => String(job.id) === currentModalJobId);
    const stillNeedsConfirm = Boolean(targetJob && needsStopConfirmReminder(targetJob));
    if (!stillNeedsConfirm) closeStopConfirmModal();
  }
  if (isEditingUartInput()) return;
  renderRecentJobs(jobs);
}
async function bootstrap() {
  try {
    const cfgResp = await fetch('/api/client-config');
    if (cfgResp.ok) {
      const cfg = await cfgResp.json();
      const parsedPort = Number.parseInt(cfg.service_port, 10);
      if (Number.isFinite(parsedPort) && parsedPort > 0) servicePort = parsedPort;
      const parsedCreateMax = Number.parseInt(cfg.create_jobs_max_num, 10);
      if (Number.isFinite(parsedCreateMax) && parsedCreateMax > 0) createJobsMaxNum = parsedCreateMax;
    }
  } catch (_) {}
  try {
    const sessionResp = await fetch(buildApiUrl('/api/session'));
    if (sessionResp.ok) {
      const session = await sessionResp.json();
      currentUser = session.user || 'user';
      currentUserId = String(session.user_id || currentUserId);
    }
  } catch (_) {}
  try {
    const platformResp = await fetch(buildApiUrl('/api/platform-options'));
    if (!platformResp.ok) {
      alert(`Failed to load HAPS platform config: ${await platformResp.text()}`);
      return;
    }
    const platformData = await platformResp.json();
    hapsPlatforms = Array.isArray(platformData.haps_platforms) ? platformData.haps_platforms : [];
    uartDevices = Array.isArray(platformData.uart_devices)
      ? platformData.uart_devices.map((item) => String(item || '').trim()).filter(Boolean)
      : [];
  } catch (error) {
    alert(`Failed to load HAPS platform config: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }
  initJobsTimingSettings();
  createNewJobCard();
  connectUartSocket();
  refreshRecentJobs();
  refreshWaitingJobs();
  setInterval(() => { refreshRecentJobs(); refreshWaitingJobs(); }, 2000);
}
form.addEventListener('submit', submitJobs);
bootstrap();
