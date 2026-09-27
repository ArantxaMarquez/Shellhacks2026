const SERVICE_URL = 'http://192.168.1.114:5050'; // <-- point this at the Pi's IP, e.g. 'http://192.168.1.50:5050'

const POLL_MS = 1000;          // how often to refresh status and thumbnail
const FETCH_TIMEOUT_MS = 2500; // give up on a request after this long
const STALE_AFTER_MS = 4000;   // status timestamp not advancing for this long => offline

const card = document.getElementById('card');
const frame = document.getElementById('frame');
const pct = document.getElementById('pct');
const unit = document.getElementById('unit');
const bar = document.getElementById('bar');
const barTrack = document.getElementById('bar-track');
const badge = document.getElementById('badge');
const caption = document.getElementById('caption');

// -- driving scene elements --
const sceneCard = document.getElementById('sceneCard');
const sceneRoad = document.getElementById('scene-road');
const car = document.getElementById('car');
const sceneBanner = document.getElementById('scene-banner');
const sceneBg = document.getElementById('scene-bg');
const sceneWeather = document.getElementById('scene-weather');

// Vehicle stops on these statuses. Add 'degraded' here too if you want it to
// react earlier/more cautiously instead of only on full obstruction.
const STOP_ON = ['obstructed'];

let lastTimestamp = null;   // service's timestamp from the last successful poll
let lastAdvanceAt = 0;      // local time when that timestamp last changed
let pollInFlight = false;
let wasOffline = false;     // so we log the outage once, not every second

// The service's timestamp comes from the Pi's clock, so we only check that it
// keeps changing, never compare it to this machine's clock.

function render(state, confidence, text) {
  card.dataset.state = state;
  badge.textContent = state;
  caption.textContent = text;
  if (confidence === null) {
    pct.textContent = '--';
    unit.textContent = '';
    bar.style.width = '0%';
    barTrack.removeAttribute('aria-valuenow');
  } else {
    pct.textContent = confidence;
    unit.textContent = '%';
    bar.style.width = confidence + '%';
    barTrack.setAttribute('aria-valuenow', confidence);
  }
}

function renderScene(state, confidence = null) {
  sceneCard.dataset.state = state;
  const stopped = STOP_ON.includes(state) || state === 'offline';
  const fogIntensity = typeof confidence === 'number'
    ? Math.max(0, Math.min(1, (100 - confidence) / 100))
    : 0;

  sceneRoad.classList.toggle('paused', stopped);
  car.classList.toggle('stopped', stopped);
  sceneCard.classList.toggle('weather-active', fogIntensity > 0.05);

  if (sceneWeather) {
    const opacity = Math.min(1, fogIntensity * 1.2);
    sceneWeather.classList.toggle('visible', fogIntensity > 0.05);
    sceneWeather.style.opacity = String(opacity);
    sceneWeather.style.filter = `blur(${fogIntensity * 1.5}px) saturate(${1 + fogIntensity * 0.65})`;
  }

  sceneBanner.classList.toggle('show', stopped && state !== 'offline');
  if (sceneBg) {
    if (stopped) sceneBg.pause();
    else sceneBg.play().catch(() => {}); // autoplay can reject before first user interaction
  }
}

function captionFor(s) {
  if (s.status === 'clear') return 'Camera view is clear.';
  // Say which check is dragging the score down.
  const cause = s.obstruction_score < s.fog_score
    ? 'Something may be covering part of the lens.'
    : 'The image looks blurry or foggy.';
  return s.status === 'obstructed' ? `Camera is obstructed. ${cause}` : `Image quality is reduced. ${cause}`;
}

function showOffline(reason) {
  if (!wasOffline) console.warn(`Sensor offline: ${reason}`);
  wasOffline = true;
  render('offline', null, `Sensor offline: ${reason}`);
  renderScene('offline');
}

async function pollStatus() {
  if (pollInFlight) return; // don't stack requests if the network is slow
  pollInFlight = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${SERVICE_URL}/status`, { cache: 'no-store', signal: controller.signal });
    if (res.status === 503) throw new Error('camera not producing frames');
    if (!res.ok) throw new Error(`service returned HTTP ${res.status}`);
    const s = await res.json();
    if (typeof s.confidence !== 'number') throw new Error('unexpected response from service');

    // Detect a frozen capture loop (service is up but the camera stopped).
    const now = Date.now();
    if (s.timestamp !== lastTimestamp) {
      lastTimestamp = s.timestamp;
      lastAdvanceAt = now;
    } else if (now - lastAdvanceAt > STALE_AFTER_MS) {
      throw new Error('camera stopped sending frames');
    }

    if (wasOffline) console.info('Sensor back online');
    wasOffline = false;
    render(s.status, s.confidence, captionFor(s));
    renderScene(s.status, s.confidence);
  } catch (err) {
    const reason = err.name === 'AbortError' ? 'no response from service' : err.message === 'Failed to fetch' ? 'cannot reach service' : err.message;
    showOffline(reason);
  } finally {
    clearTimeout(timer);
    pollInFlight = false;
  }
}

function refreshFrame() {
  frame.src = `${SERVICE_URL}/frame.jpg?t=${Date.now()}`;
}

render('offline', null, 'Connecting to sensor…');
renderScene('offline');
pollStatus();
refreshFrame();
setInterval(pollStatus, POLL_MS);
setInterval(refreshFrame, POLL_MS);