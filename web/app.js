'use strict';
const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);
const MODES = {
  normal: ['Normal', 'Fixed position – the camera stays where you put it.'],
  tracking: ['Tracking', 'The camera follows you. Fine-tune it in the Tracking card.'],
  deskview: ['DeskView', 'Tilts down to your desk and straightens the image – for documents and sketches.'],
  whiteboard: ['Whiteboard', 'Finds the whiteboard, straightens it and boosts the contrast.'],
  overhead: ['Overhead', 'Looks straight down – for hands, products and objects on the desk.'],
};
const deg = (v, d = 1) => v.toFixed(d).replace('-', '−') + '°';
const times = z => (z / 100).toFixed(1) + '×';
let S, hold, toastTimer, step = 0;

async function api(body) {
  try {
    const r = await fetch(body ? '/api' : '/state', body ? {method: 'POST', body: JSON.stringify(body)} : {});
    const j = await r.json();
    if (!r.ok) throw Object.assign(Error(j.error), {status: r.status});
    render(j);
    return true;
  } catch (e) {
    connected(e.status == 400, e.message);  // 400 = bad command, the camera itself is fine
    if (body) toast(e.message);
  }
}

function connected(ok, why = '') {
  $('#status').classList.toggle('off', !ok);
  $('#status span').textContent = ok ? 'Connected' : 'Not connected';
  $('#status').title = ok ? '' : why;
}

function render(j) {
  S = j;
  connected(true);
  const tracking = j.mode == 'tracking', s = j.settings, [label, hint] = MODES[j.mode] || ['Switching…', ''];
  $$('.modes [data-mode]').forEach(b => b.setAttribute('aria-pressed', b.dataset.mode == j.mode));
  $('#mode-chip').textContent = label;
  $('#mode-hint').textContent = hint;
  $('#pan').textContent = deg(j.pan);
  $('#tilt').textContent = deg(j.tilt);
  $('#pt').textContent = `${deg(j.pan)} · ${deg(j.tilt)}`;
  $('#zoom-chip').textContent = $('#zoom-val').textContent = times(j.zoom);
  setRange($('#zoom'), j.zoom);

  $('#track-on').hidden = !tracking;
  $('#track-btn').hidden = $('#track-note').hidden = tracking;
  $('#sliders').classList.toggle('off', !tracking);
  for (const k of ['offset', 'calm']) {
    $('#' + k).disabled = !tracking;
    setRange($('#' + k), s[k]);
  }

  $$('.preset').forEach(b => {
    const n = b.dataset.preset, p = s[n];
    const here = p && Math.abs(p[0] / 3600 - j.pan) < .5 && Math.abs(p[1] / 3600 - j.tilt) < .5 && Math.abs(p[2] - j.zoom) < 2;
    b.classList.toggle('empty', !p);
    b.classList.toggle('active', !!here);
    b.querySelector('.sub').textContent = p ? `P ${deg(p[0] / 3600, 0)} · T ${deg(p[1] / 3600, 0)} · ${times(p[2])}` : 'Empty · ⇧-click';
    b.setAttribute('aria-label', p ? `Go to position ${n}` : `Position ${n} is empty, shift-click to save`);
  });

  const box = tracking && j.person, el = $('#person');
  el.hidden = !box;
  if (box) Object.assign(el.style, {left: box[0] * 100 + '%', top: box[1] * 100 + '%', width: box[2] * 100 + '%', height: box[3] * 100 + '%'});
}

function setRange(el, v) {
  if (document.activeElement != el) el.value = v;
  fill(el);
}

function fill(el) {
  el.style.setProperty('--p', (el.value - el.min) / (el.max - el.min) * 100 + '%');
  if (el.id == 'offset') $('#offset-val').textContent = (el.value > 0 ? '+' : '') + deg(+el.value, 0);
  if (el.id == 'calm') $('#calm-val').textContent = deg(+el.value, 0);
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.hidden = true, 2200);
}

document.addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b || !S) return;
  const d = b.dataset;
  if (d.mode) api({cmd: 'mode', m: d.mode});
  else if ('home' in d) api({cmd: 'home'});
  else if (d.zoom) api({cmd: 'zoom', z: S.zoom + +d.zoom});
  else if (d.dx && !e.detail) api({cmd: 'move', dx: +d.dx, dy: +d.dy});  // keyboard: one step per press
  else if (d.preset) {
    if (e.shiftKey) (await api({cmd: 'save', i: +d.preset})) && toast(`Position ${d.preset} saved`);
    else if (S.settings[d.preset]) api({cmd: 'go', i: +d.preset});
    else toast(`Position ${d.preset} is empty – ⇧-click to save`);
  }
});

// Pan and tilt keep moving while an arrow is held down
const stop = () => clearInterval(hold);
$$('[data-dx]').forEach(b => {
  b.onpointerdown = () => {
    stop();
    const go = () => api({cmd: 'move', dx: +b.dataset.dx, dy: +b.dataset.dy});
    go();
    hold = setInterval(go, 150);
  };
  b.onpointerup = b.onpointerleave = b.onpointercancel = stop;
});
addEventListener('blur', stop);

$('#zoom').oninput = e => { fill(e.target); api({cmd: 'zoom', z: +e.target.value}) };
for (const k of ['offset', 'calm']) {
  $('#' + k).oninput = e => fill(e.target);
  $('#' + k).onchange = e => api({cmd: k, v: +e.target.value});
}

async function preview() {
  const md = navigator.mediaDevices, msg = $('#preview-msg');
  const find = async () => (await md.enumerateDevices()).find(d => d.kind == 'videoinput' && /insta360 link/i.test(d.label));
  try {
    let cam = await find();
    if (!cam) {  // device names only show up after camera permission was granted
      (await md.getUserMedia({video: true})).getTracks().forEach(t => t.stop());
      cam = await find();
    }
    if (!cam) throw Error('Insta360 Link not found');
    // 1080p: apps share one camera format on macOS, so a smaller preview could pull a call down to 720p
    const stream = await md.getUserMedia({video: {deviceId: {exact: cam.deviceId}, width: {ideal: 1920}, height: {ideal: 1080}}});
    $('#video').srcObject = stream;
    $('#res').textContent = `Live · ${stream.getVideoTracks()[0].getSettings().height}p`;
    msg.hidden = true;
  } catch (e) {
    msg.textContent = `Preview unavailable: ${e.message}`;
    msg.hidden = false;
    $('#res').textContent = 'No preview';
  }
}

// Tutorial: shown on first start, and again from the ? button
const tour = $('#tour'), pages = tour.querySelectorAll('.tour-page');
function showStep(i) {
  step = i;
  const last = i == pages.length - 1;
  pages.forEach((p, k) => p.hidden = k != i);
  tour.querySelectorAll('.tour-icon svg').forEach((p, k) => p.hidden = k != i);
  tour.querySelectorAll('.dots i').forEach((p, k) => p.classList.toggle('on', k == i));
  $('#tour-step').textContent = `Step ${i + 1} of ${pages.length}`;
  $('#tour-back').hidden = i == 0;
  $('#tour-skip').hidden = i != 0;
  $('#tour-next-label').textContent = last ? 'Let’s go' : 'Next';
}
function openTour() {
  showStep(0);
  tour.showModal();
}
$('#help').onclick = openTour;
$('#tour-next').onclick = () => step < pages.length - 1 ? showStep(step + 1) : tour.close();
$('#tour-back').onclick = () => showStep(step - 1);
$('#tour-skip').onclick = $('#tour-close').onclick = () => tour.close();
tour.onclose = () => { try { localStorage.setItem('toured', '1') } catch {} };

let toured = '1';
try { toured = localStorage.getItem('toured') } catch {}
if (!toured) openTour();
api();
setInterval(api, 1000);  // hand gestures and our own tracking change the state too
preview();
