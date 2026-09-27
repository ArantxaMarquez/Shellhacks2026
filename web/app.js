const SERVICE_URL = 'http://192.168.1.109:5050'; // <-- point this at the Pi's IP, e.g. 'http://192.168.1.50:5050'

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
const hudStatus = document.getElementById('hud-status');

let lastTimestamp = null;   // service's timestamp from the last successful poll
let lastAdvanceAt = 0;      // local time when that timestamp last changed
let pollInFlight = false;
let wasOffline = false;     // so we log the outage once, not every second

// Latest values for the car scene. render() writes them on every status poll;
// the animation loop below only reads them and never fetches anything.
let sceneState = 'offline';     // same clear/degraded/obstructed/offline string the badge shows
let sceneConfidence = null;     // 0-100, or null when offline
const countUp = { value: 0, velocity: 0, target: null };   // spring behind the big % number

// The service's timestamp comes from the Pi's clock, so we only check that it
// keeps changing, never compare it to this machine's clock.

function render(state, confidence, text) {
  sceneState = state;            // the car scene reads these; see the animation loop
  sceneConfidence = confidence;  // (null when offline)
  document.body.dataset.state = state;   // drives the accent colour everywhere
  card.dataset.state = state;
  badge.textContent = state;
  hudStatus.textContent = state;
  caption.textContent = text;
  countUp.target = confidence;   // the loop animates the number toward this
  if (confidence === null) {
    unit.textContent = '';
    bar.style.width = '0%';
    barTrack.removeAttribute('aria-valuenow');
  } else {
    unit.textContent = '%';
    bar.style.width = confidence + '%';
    barTrack.setAttribute('aria-valuenow', confidence);
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

// ---- Car scene ---------------------------------------------------------------
// Confidence sets the road speed, the fog and the brake light. The clear /
// degraded / obstructed cutoffs are NOT redefined here: the brake light follows
// the state string the service already computed for the badge.

const SPEED_MIN_PX = 20;        // road speed (px/sec) at confidence 0...
const SPEED_RANGE_PX = 220;     // ...plus this much at confidence 100
const MAX_MPH = 65;             // readout at full speed; scales down with the road
const FOG_START = 60;           // fog begins below this confidence, full strength at 0
const FOG_MAX_OPACITY = 0.85;   // keep the car faintly visible even at 0
const FOG_MAX_BLUR_PX = 6;
const EASE_PER_SEC = 8;         // how fast shown values chase the data (higher = snappier)
const WHEEL_RADIUS_UNITS = 19;  // wheel radius in the car SVG's own units

const BRAKE = {                 // tail light per badge state
  clear:      { color: 'var(--brake-off)', glow: 'none',                                  halo: 0 },
  degraded:   { color: 'var(--warn)',      glow: 'drop-shadow(0 0 5px var(--warn))',      halo: 0.35 },
  obstructed: { color: 'var(--bad)',       glow: 'drop-shadow(0 0 9px var(--bad))',       halo: 0.8 },
  offline:    { color: 'var(--brake-off)', glow: 'none',                                  halo: 0 },
};

const scene = document.getElementById('scene');
const road = document.getElementById('road');
const fog = document.getElementById('fog');
const car = document.getElementById('car');
const brake = document.getElementById('brake');
const tailGlow = document.getElementById('tailGlow');
const beam = document.getElementById('beam');
const wheels = [...document.querySelectorAll('.wheel-spin')];
const mph = document.getElementById('mph');

// Parallax layers (stars, skylines, speed streaks): each scrolls at a multiple of the road speed.
const layers = [...document.querySelectorAll('[data-parallax]')].map(el => (
  { el, mult: parseFloat(el.dataset.parallax), tile: parseFloat(el.dataset.tile), offset: 0 }));

const roadTile = parseFloat(getComputedStyle(road).getPropertyValue('--road-tile')) || 90;
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

let carScale = 1;             // on-screen px per SVG unit, so wheels roll without slipping
new ResizeObserver(() => { carScale = car.offsetWidth / 300 || 1; }).observe(car);

let shownSpeed = 0;   // px/sec actually being drawn, eased toward the target
let shownFog = 0;     // 0-1
let roadOffset = 0;
let wheelAngle = 0;   // degrees
let brakeState = null;
let lastFrameAt = null;

function animateScene(now, dt) {
  const c = sceneConfidence;   // latest poll result; null = offline, so stop and clear the fog
  const targetSpeed = c === null ? 0 : SPEED_MIN_PX + (c / 100) * SPEED_RANGE_PX;
  const targetFog = c === null ? 0 : clamp((FOG_START - c) / FOG_START, 0, 1);

  const k = 1 - Math.exp(-EASE_PER_SEC * dt);   // same easing at any frame rate
  shownSpeed += (targetSpeed - shownSpeed) * k;
  shownFog += (targetFog - shownFog) * k;
  const speedNorm = shownSpeed / (SPEED_MIN_PX + SPEED_RANGE_PX);   // 0-1

  if (!reduceMotion) {
    // Road: continuous scroll leftwards (the car faces right).
    roadOffset = (roadOffset + shownSpeed * dt) % roadTile;
    road.style.backgroundPositionX = `${-roadOffset}px, 0`;
    for (const l of layers) {
      l.offset = (l.offset + shownSpeed * l.mult * dt) % l.tile;
      l.el.style.backgroundPositionX = `${-l.offset}px`;
    }

    // Wheels turn in step with the road; the body bobs, and dips forward as it slows.
    wheelAngle = (wheelAngle + (shownSpeed * dt) / (WHEEL_RADIUS_UNITS * carScale) * 57.2958) % 360;
    for (const w of wheels) w.setAttribute('transform', `rotate(${wheelAngle.toFixed(1)})`);
    const bob = Math.sin(now / 1000 * (7 + speedNorm * 11)) * (0.3 + speedNorm * 1.1);
    const pitch = (1 - speedNorm) * 1.6;
    car.style.transform = `translateY(${bob.toFixed(2)}px) rotate(${pitch.toFixed(2)}deg)`;
  }
  scene.style.setProperty('--speedlines', clamp((speedNorm - 0.45) / 0.55, 0, 1).toFixed(3));

  // Headlight beam glows brighter in fog (light scatters).
  beam.setAttribute('opacity', (0.32 + shownFog * 0.45).toFixed(2));

  // Fog: opacity and blur both scale with how far below FOG_START we are.
  fog.style.opacity = (shownFog * FOG_MAX_OPACITY).toFixed(3);
  const blur = `blur(${(shownFog * FOG_MAX_BLUR_PX).toFixed(2)}px)`;
  fog.style.backdropFilter = blur;
  fog.style.webkitBackdropFilter = blur;

  // Tail light: only touch the DOM when the badge state changes.
  if (sceneState !== brakeState) {
    brakeState = sceneState;
    const b = BRAKE[sceneState] || BRAKE.offline;
    brake.style.fill = b.color;
    brake.style.filter = b.glow;
    tailGlow.style.color = b.color;
    tailGlow.style.opacity = b.halo;
  }

  // Speed readout comes from the same shownSpeed as the road, so they always agree.
  const text = c === null ? '--' : String(Math.round(speedNorm * MAX_MPH));
  if (mph.textContent !== text) mph.textContent = text;
}

// ---- Effects ported from React Bits (reactbits.dev) ---------------------------
// Plain JS versions of ShinyText, GradientText, CountUp and SpotlightCard. Their
// markup/CSS is in style.css; the per-frame maths lives here, in the same loop.

// ShinyText: highlight sweeps across the text. speed = seconds per sweep.
const SHINY_SPEED_S = 3.2;
const shinyEls = [...document.querySelectorAll('.shiny-text')];
function updateShinyText(now) {
  const p = ((now / 1000) % SHINY_SPEED_S) / SHINY_SPEED_S * 100;   // 0 -> 100
  const pos = `${150 - p * 2}% center`;
  for (const el of shinyEls) el.style.backgroundPosition = pos;
}

// GradientText: colours slide back and forth (yoyo). speed = seconds per direction.
const GRADIENT_SPEED_S = 8;
const gradientEls = [...document.querySelectorAll('.gradient-text')];
function updateGradientText(now) {
  const cycle = (now / 1000) % (GRADIENT_SPEED_S * 2);
  const p = (cycle < GRADIENT_SPEED_S ? cycle : GRADIENT_SPEED_S * 2 - cycle) / GRADIENT_SPEED_S * 100;
  for (const el of gradientEls) el.style.backgroundPosition = `${p}% 50%`;
}

// CountUp: the big number springs toward its target instead of jumping.
// Same spring constants as the original: duration 1.2s -> damping / stiffness below.
const COUNT_DURATION_S = 1.2;
const COUNT_DAMPING = 20 + 40 / COUNT_DURATION_S;
const COUNT_STIFFNESS = 100 / COUNT_DURATION_S;
function updateCountUp(dt) {
  if (countUp.target === null) {
    countUp.value = 0;           // counts up from 0 again when the sensor returns
    countUp.velocity = 0;
    if (pct.textContent !== '--') pct.textContent = '--';
    return;
  }
  // Small fixed sub-steps keep the spring stable if a frame is slow.
  for (let left = dt; left > 0; left -= 1 / 120) {
    const h = Math.min(left, 1 / 120);
    countUp.velocity += (-COUNT_STIFFNESS * (countUp.value - countUp.target) - COUNT_DAMPING * countUp.velocity) * h;
    countUp.value += countUp.velocity * h;
  }
  const text = String(Math.round(clamp(countUp.value, 0, 100)));
  if (pct.textContent !== text) pct.textContent = text;
}

// SpotlightCard: a glow follows the cursor over the card.
for (const el of document.querySelectorAll('.card-spotlight')) {
  el.addEventListener('mousemove', e => {
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mouse-x', `${e.clientX - r.left}px`);
    el.style.setProperty('--mouse-y', `${e.clientY - r.top}px`);
  });
}

// ---- One animation loop for everything (started once, never restarted per poll) ----
function frameLoop(now) {
  // Cap dt so a background tab coming back doesn't teleport the road.
  const dt = lastFrameAt === null ? 0 : Math.min((now - lastFrameAt) / 1000, 0.1);
  lastFrameAt = now;

  animateScene(now, dt);
  updateCountUp(dt);
  if (!reduceMotion) {
    updateShinyText(now);
    updateGradientText(now);
  }
  requestAnimationFrame(frameLoop);
}

render('offline', null, 'Connecting to sensor…');
pollStatus();
refreshFrame();
setInterval(pollStatus, POLL_MS);
setInterval(refreshFrame, POLL_MS);
requestAnimationFrame(frameLoop);
