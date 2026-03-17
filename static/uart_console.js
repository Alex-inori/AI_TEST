const params = new URLSearchParams(window.location.search);
const jobId = params.get('job_id') || '';
const titleNode = document.getElementById('uartPageTitle');
const statusNode = document.getElementById('uartPageStatus');
const tabsNode = document.getElementById('uartPageTabs');
const outputNode = document.getElementById('uartPageOutput');

let selectedIndex = 0;
let pollTimer = null;
let streamPollTimer = null;
let streams = [];

function setStatus(text) {
  statusNode.textContent = text;
}

function renderTabs() {
  tabsNode.innerHTML = '';
  if (!streams.length) {
    const empty = document.createElement('div');
    empty.textContent = 'No UART streams.';
    tabsNode.appendChild(empty);
    return;
  }

  streams.forEach((stream) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `uart-tab-btn${stream.index === selectedIndex ? ' active' : ''}`;
    btn.textContent = `${stream.index + 1}: ${stream.path || '-'}`;
    btn.addEventListener('click', () => {
      selectedIndex = stream.index;
      renderTabs();
      refreshOutput();
    });
    tabsNode.appendChild(btn);
  });
}

async function refreshStreams() {
  if (!jobId) {
    setStatus('Missing job_id in URL');
    return;
  }

  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/uart-streams`);
  if (!response.ok) {
    setStatus(`Load stream list failed: ${await response.text()}`);
    return;
  }

  const data = await response.json();
  streams = Array.isArray(data.streams) ? data.streams : [];
  if (streams.length && !streams.some((item) => item.index === selectedIndex)) {
    selectedIndex = streams[0].index;
  }

  setStatus(`Job ${jobId} · ${data.status || '-'} · ${streams.length} UART`);
  renderTabs();
}

async function refreshOutput() {
  if (!streams.length) {
    outputNode.textContent = '';
    return;
  }

  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/uart/${selectedIndex}?lines=300`);
  if (!response.ok) {
    outputNode.textContent = `Load UART failed: ${await response.text()}`;
    return;
  }

  const data = await response.json();
  outputNode.textContent = data.text || '';
  outputNode.scrollTop = outputNode.scrollHeight;
}

async function bootstrap() {
  titleNode.textContent = `UART Console - ${jobId || '-'}`;
  await refreshStreams();
  await refreshOutput();

  if (pollTimer) window.clearInterval(pollTimer);
  if (streamPollTimer) window.clearInterval(streamPollTimer);
  pollTimer = window.setInterval(refreshOutput, 1200);
  streamPollTimer = window.setInterval(refreshStreams, 5000);
}

bootstrap();
