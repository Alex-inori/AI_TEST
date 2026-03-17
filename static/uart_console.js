const params = new URLSearchParams(window.location.search);
const jobId = params.get('job_id') || '';
const jobsId = params.get('jobs_id') || '';
const uartHintRaw = params.get('uart_paths') || '[]';

const titleNode = document.getElementById('uartPageTitle');
const statusNode = document.getElementById('uartPageStatus');
const tabsNode = document.getElementById('uartPageTabs');
const outputNode = document.getElementById('uartPageOutput');
const connectCurrentBtn = document.getElementById('connectCurrentBtn');
const disconnectCurrentBtn = document.getElementById('disconnectCurrentBtn');

let selectedIndex = 0;
const streams = [];

function setStatus(text) {
  statusNode.textContent = text;
}

function safeDecodeUartHints(text) {
  try {
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((item) => String(item || '').trim()).filter(Boolean);
  } catch (_) {
    return [];
  }
}

function appendOutput(text) {
  outputNode.textContent += text;
  outputNode.scrollTop = outputNode.scrollHeight;
}

function streamTitle(stream, index) {
  const hint = stream.hintPath || `UART${index + 1}`;
  const connected = stream.port ? 'Connected' : 'Disconnected';
  return `${index + 1}: ${hint} (${connected})`;
}

function renderTabs() {
  tabsNode.innerHTML = '';
  streams.forEach((stream, index) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `uart-tab-btn${index === selectedIndex ? ' active' : ''}`;
    btn.textContent = streamTitle(stream, index);
    btn.addEventListener('click', () => {
      selectedIndex = index;
      renderTabs();
      setStatus(streamTitle(streams[selectedIndex], selectedIndex));
    });
    tabsNode.appendChild(btn);
  });

  if (!streams.length) {
    const empty = document.createElement('div');
    empty.textContent = 'No UART hints from job submission.';
    tabsNode.appendChild(empty);
  }
}

async function closeStream(stream) {
  if (!stream) return;
  if (stream.reader) {
    try { await stream.reader.cancel(); } catch (_) {}
    try { stream.reader.releaseLock(); } catch (_) {}
  }
  if (stream.inputDone) {
    try { await stream.inputDone.catch(() => {}); } catch (_) {}
  }
  if (stream.port) {
    try { await stream.port.close(); } catch (_) {}
  }
  stream.port = null;
  stream.reader = null;
  stream.inputDone = null;
}

async function connectCurrentPort() {
  if (!('serial' in navigator)) {
    setStatus('Current browser does not support Web Serial API.');
    return;
  }
  const stream = streams[selectedIndex];
  if (!stream) {
    setStatus('No stream selected.');
    return;
  }

  await closeStream(stream);

  try {
    const port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });

    const decoder = new TextDecoderStream();
    const inputDone = port.readable.pipeTo(decoder.writable).catch(() => {});
    const reader = decoder.readable.getReader();

    stream.port = port;
    stream.reader = reader;
    stream.inputDone = inputDone;

    setStatus(`Connected ${stream.hintPath || `UART${selectedIndex + 1}`}`);
    renderTabs();

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) appendOutput(value);
    }
  } catch (error) {
    setStatus(`Connect failed: ${error.message || error}`);
    await closeStream(stream);
    renderTabs();
  }
}

async function disconnectCurrentPort() {
  const stream = streams[selectedIndex];
  if (!stream) return;
  await closeStream(stream);
  renderTabs();
  setStatus(`Disconnected ${stream.hintPath || `UART${selectedIndex + 1}`}`);
}

function initStreams() {
  const hints = safeDecodeUartHints(uartHintRaw);
  hints.forEach((path) => {
    streams.push({ hintPath: path, port: null, reader: null, inputDone: null });
  });
}

function bindEvents() {
  connectCurrentBtn.addEventListener('click', connectCurrentPort);
  disconnectCurrentBtn.addEventListener('click', disconnectCurrentPort);
}

function bootstrap() {
  const head = jobsId || jobId || '-';
  titleNode.textContent = `UART Web Serial Console - ${head}`;
  initStreams();
  renderTabs();
  bindEvents();
}

bootstrap();
