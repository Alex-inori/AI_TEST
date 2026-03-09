const newJobsList = document.getElementById('newJobsList');
const template = document.getElementById('newJobTemplate');
const uartTemplate = document.getElementById('uartTemplate');
const recentJobs = document.getElementById('recentJobs');
const addJobBtn = document.getElementById('addJobBtn');
const form = document.getElementById('newJobsForm');
let currentUser = 'user';

function makeJobsId() {
  const now = new Date();
  const pad = (v) => String(v).padStart(2, '0');
  const ts = `${pad(now.getFullYear() % 100)}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `${currentUser}_${ts}`;
}

function addUartItem(card, value = '') {
  const uartList = card.querySelector('.uart-list');
  const item = uartTemplate.content.firstElementChild.cloneNode(true);
  item.querySelector('.uart-input').value = value;
  item.querySelector('.remove-uart-btn').addEventListener('click', () => item.remove());
  uartList.appendChild(item);
}

function bindBitfileLogic(card, prefill = {}) {
  const input = card.querySelector('.bitfile-path');
  const latestBtn = card.querySelector('.latest-btn');
  let mode = prefill.bitfile === 'GET_LATEST' ? 'latest' : (prefill.bitfile_mode || 'path');

  const render = () => {
    const isLatest = mode === 'latest';
    latestBtn.classList.toggle('active', isLatest);
    latestBtn.textContent = isLatest ? 'Using Latest' : 'Get Latest';
    input.disabled = isLatest;
    if (isLatest) input.value = '';
  };

  latestBtn.addEventListener('click', () => {
    mode = mode === 'latest' ? 'path' : 'latest';
    render();
  });

  card._getBitfileMode = () => mode;
  render();
}

function bindBinfileBrowse(card) {
  const btn = card.querySelector('.browse-btn');
  const target = card.querySelector('.binfile-path');
  btn.addEventListener('click', () => {
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.onchange = () => {
      if (picker.files?.[0]) target.value = picker.files[0].name;
    };
    picker.click();
  });
}

function createNewJobCard(prefill = {}, insertAfterNode = null) {
  const node = template.content.firstElementChild.cloneNode(true);
  node.querySelector('input[name="jobs_id"]').value = prefill.jobs_id || makeJobsId();
  node.querySelector('select[name="haps_platform"]').value = prefill.haps_platform || 'BJ-HAPS80';
  node.querySelector('input[name="bitfile"]').value = prefill.bitfile && prefill.bitfile !== 'GET_LATEST' ? prefill.bitfile : '';
  node.querySelector('input[name="binfile"]').value = prefill.binfile || '';
  node.querySelector('input[name="log_path"]').value = prefill.log_path || '';

  const openocdCfg = prefill.openocd_cfg || {};
  node.querySelector('input[name="openocd_tool_path"]').value = openocdCfg.tool_path || '';
  node.querySelector('input[name="openocd_cfg_file"]').value = openocdCfg.cfg_file || '';

  (prefill.uart_paths || ['']).forEach((val) => addUartItem(node, val));

  node.querySelector('.add-uart-btn').addEventListener('click', () => addUartItem(node));
  node.querySelector('.minus-btn').addEventListener('click', () => {
    node.remove();
    if (!newJobsList.children.length) createNewJobCard();
  });
  node.querySelector('.plus-btn').addEventListener('click', () => createNewJobCard({}, node));

  bindBitfileLogic(node, prefill);
  bindBinfileBrowse(node);

  if (insertAfterNode?.parentNode) {
    insertAfterNode.insertAdjacentElement('afterend', node);
  } else {
    newJobsList.appendChild(node);
  }
}

function collectNewJobs() {
  return Array.from(newJobsList.querySelectorAll('.job-card')).map((card) => ({
    jobs_id: card.querySelector('input[name="jobs_id"]').value.trim(),
    haps_platform: card.querySelector('select[name="haps_platform"]').value,
    bitfile_mode: card._getBitfileMode ? card._getBitfileMode() : 'path',
    bitfile: card.querySelector('input[name="bitfile"]').value.trim(),
    binfile: card.querySelector('input[name="binfile"]').value.trim(),
    log_path: card.querySelector('input[name="log_path"]').value.trim(),
    openocd_cfg: {
      tool_path: card.querySelector('input[name="openocd_tool_path"]').value.trim(),
      cfg_file: card.querySelector('input[name="openocd_cfg_file"]').value.trim(),
    },
    uart_paths: Array.from(card.querySelectorAll('.uart-input')).map((i) => i.value.trim()).filter(Boolean),
  }));
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
  createNewJobCard();
  refreshRecentJobs();
}

async function finishJob(jobId) {
  if (!window.confirm('Finish this running job?')) return;
  const response = await fetch(`/api/jobs/${jobId}/stop`, { method: 'POST' });
  if (!response.ok) return alert('Finish failed');
  refreshRecentJobs();
}

function renderRecentJobs(jobs) {
  recentJobs.innerHTML = '';
  if (!jobs.length) return (recentJobs.textContent = 'No jobs yet');

  jobs.forEach((job) => {
    const payload = job.payload || {};
    const item = document.createElement('div');
    item.className = 'recent-card recent-grid';
    item.innerHTML = `
      <div class="kv jobsid-cell"><span class="key">JobsID</span><span class="val jobsid-val">${payload.jobs_id || '-'}</span></div>
      <div class="kv status-cell"><span class="key">Status</span><span class="val status ${job.status}">${job.status}</span></div>
      <div class="kv"><span class="key">Endtime</span><span class="val">${job.end_time || '-'}</span></div>
      <div class="kv"><span class="key">Log Info</span><span class="val">${job.log_info || '-'}</span></div>
      <div class="actions"></div>
    `;

    const actions = item.querySelector('.actions');
    const copyBtn = document.createElement('button');
    copyBtn.textContent = 'Copy to New Jobs';
    copyBtn.className = 'copy-btn';
    copyBtn.type = 'button';
    copyBtn.addEventListener('click', () => createNewJobCard(payload));
    actions.appendChild(copyBtn);

    if (job.status === 'Runing') {
      const finishBtn = document.createElement('button');
      finishBtn.textContent = 'Finish';
      finishBtn.className = 'finish-btn';
      finishBtn.type = 'button';
      finishBtn.addEventListener('click', () => finishJob(job.id));
      actions.appendChild(finishBtn);
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
    const resp = await fetch('/api/session');
    if (resp.ok) currentUser = (await resp.json()).user || 'user';
  } catch (_) {}
  createNewJobCard();
  refreshRecentJobs();
  setInterval(refreshRecentJobs, 2000);
}

addJobBtn.addEventListener('click', () => createNewJobCard());
form.addEventListener('submit', submitJobs);
bootstrap();
