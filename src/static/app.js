/* Shared helpers + per-page initialisation. Each page sets <body data-page="…">. */
const $ = (id) => document.getElementById(id);

let cameraEnabled = true;
let streamOn = false;
let lastSeq = -1;
let quitting = false;

let studentsCache = [];
let presentToday = new Set();
let expandedName = null;
const detailCache = {};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'X-Requested-With': 'fetch', ...(opts.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = data && data.detail ? data.detail : res.statusText;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function cell(tag, text, cls) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function markActiveNav() {
  const page = document.body.dataset.page;
  document.querySelectorAll('.nav a').forEach((a) => {
    a.classList.toggle('active', a.dataset.nav === page);
  });
}

/* ---------- shared: students list ---------- */
async function loadStudents() {
  try {
    studentsCache = await api('/api/students');
    renderStudents();
  } catch (e) {
    const body = $('studentBody');
    if (!body) return;
    body.textContent = '';
    const tr = document.createElement('tr');
    const td = cell('td', 'Could not load students: ' + e.message, 'empty');
    td.colSpan = 5;
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

function renderStudents() {
  const body = $('studentBody');
  if (!body) return;
  body.textContent = '';
  const countEl = $('studentCount');
  if (countEl) {
    countEl.textContent = studentsCache.length
      ? studentsCache.length + (studentsCache.length === 1 ? ' student' : ' students')
      : '0 students';
  }
  if (!studentsCache.length) {
    const tr = document.createElement('tr');
    const td = cell('td', 'No students yet. Use the “Add student” page to enroll the first one.', 'empty');
    td.colSpan = 5;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  const groups = new Map();
  studentsCache.forEach((s) => {
    const key = s.class_name || 'No class';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  });

  const keys = [...groups.keys()].sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));

  keys.forEach((key) => {
    const kids = groups.get(key).slice().sort((a, b) => {
      const r = String(a.roll_no || '').localeCompare(String(b.roll_no || ''), undefined, { numeric: true });
      return r !== 0 ? r : a.name.localeCompare(b.name);
    });

    const gh = document.createElement('tr');
    gh.className = 'group-row';
    const gth = document.createElement('th');
    gth.colSpan = 5;
    gth.textContent = key + ' (' + kids.length + ')';
    gh.appendChild(gth);
    body.appendChild(gh);

    kids.forEach((s) => {
      const here = presentToday.has(s.name);
      const tr = document.createElement('tr');
      tr.className = 'student-row' + (expandedName === s.name ? ' open' : '');
      tr.append(
        cell('td', s.name, 'name'),
        cell('td', s.class_name || '—'),
        cell('td', s.year || '—'),
        cell('td', s.roll_no || '—'),
      );
      tr.appendChild(cell('td', here ? 'Present' : 'Absent', here ? 'tag ok' : 'tag'));
      tr.addEventListener('click', () => toggleStudent(s.name));
      body.appendChild(tr);

      if (expandedName === s.name) {
        const dr = document.createElement('tr');
        dr.className = 'detail-row';
        const td = document.createElement('td');
        td.colSpan = 5;
        if (detailCache[s.name]) {
          td.appendChild(renderDetail(detailCache[s.name]));
        } else {
          td.appendChild(cell('span', 'Loading attendance…', 'muted'));
          loadDetail(s.name);
        }
        dr.appendChild(td);
        body.appendChild(dr);
      }
    });
  });
}

function renderDetail(d) {
  const box = document.createElement('div');

  const head = document.createElement('div');
  head.className = 'detail-head';
  head.append(
    cell('span', d.name, 'name'),
    cell('span',
      'Class ' + (d.class_name || '—') + ' · Year ' + (d.year || '—') + ' · Roll no ' + (d.roll_no || '—'),
      'detail-meta'),
  );
  box.appendChild(head);

  const summary = document.createElement('div');
  summary.className = 'detail-summary';
  summary.appendChild(cell('span',
    (d.present_today ? 'Present today' : 'Not present today'),
    'tag ' + (d.present_today ? 'ok' : '')));
  summary.appendChild(cell('span', d.total_checkins + ' total check-ins', 'tag'));
  summary.appendChild(cell('span', d.days_present.length + ' days present', 'tag'));
  box.appendChild(summary);

  if (!d.entries.length) {
    box.appendChild(cell('p', 'No attendance recorded yet for this student.', 'muted'));
  } else {
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    hr.append(cell('th', 'Date'), cell('th', 'Time'));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tb = document.createElement('tbody');
    d.entries.forEach((ts) => {
      const tr = document.createElement('tr');
      const [date, ...rest] = ts.split(' ');
      tr.append(cell('td', date), cell('td', rest.join(' '), 'time'));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
    box.appendChild(wrap);
    if (d.entries_hidden) {
      box.appendChild(cell('p', 'And ' + d.entries_hidden + ' earlier entries not shown here.', 'muted'));
    }
    if (d.days_present.length) {
      box.appendChild(cell('p', 'Present on: ' + d.days_present.join(', '), 'muted'));
    }
  }

  const actions = document.createElement('div');
  actions.className = 'detail-actions';
  const del = document.createElement('button');
  del.className = 'btn danger';
  del.type = 'button';
  del.textContent = 'Delete student';
  del.addEventListener('click', () => removeStudent(d.name));
  actions.appendChild(del);
  box.appendChild(actions);
  return box;
}

async function loadDetail(name) {
  try {
    detailCache[name] = await api('/api/students/' + encodeURIComponent(name));
  } catch (e) {
    detailCache[name] = { name, class_name: '', year: '', roll_no: '', present_today: false,
      days_present: [], total_checkins: 0, entries: [], entries_hidden: 0, error: e.message };
  }
  if (expandedName === name) renderStudents();
}

function toggleStudent(name) {
  expandedName = expandedName === name ? null : name;
  renderStudents();
}

async function removeStudent(name) {
  if (!confirm('Delete ' + name + ' and all their face samples? Their past attendance entries stay in the log.')) return;
  try {
    await api('/api/people/' + encodeURIComponent(name), { method: 'DELETE' });
    if (expandedName === name) expandedName = null;
    delete detailCache[name];
    loadStudents();
  } catch (e) {
    alert(e.message);
  }
}

/* ---------- shared: status polling ---------- */
function applyStatus(s) {
  cameraEnabled = s.camera_enabled;
  const live = s.camera_enabled && s.camera_ok;
  const pill = $('camPill');
  if (pill) {
    pill.className = 'pill ' + (live ? 'live' : (s.error ? 'bad' : ''));
    pill.textContent = live ? 'Camera live' : (!s.camera_enabled ? 'Camera off' : (s.error ? 'Camera problem' : 'Starting…'));
    $('camBtn').textContent = s.camera_enabled ? 'Stop camera' : 'Start camera';
    $('placeholder').textContent = s.error || (s.camera_enabled ? 'Waiting for the camera…' : 'Camera is off');
  }
  setStream(s.camera_enabled);
  if ($('faces')) renderFaces(s.camera_enabled, s.faces);
  if (s.log_seq !== lastSeq) {
    lastSeq = s.log_seq;
    if ($('logBody')) loadAttendance();
  }
}

async function pollStatus() {
  try {
    applyStatus(await api('/api/status'));
  } catch (e) {
    const pill = $('camPill');
    if (pill) {
      pill.className = 'pill bad';
      pill.textContent = 'Server unreachable';
    }
  }
  if (!quitting) setTimeout(pollStatus, 600);
}

/* ---------- page: students ---------- */
function initStudentsPage() {
  async function refresh() {
    try {
      const d = await api('/api/attendance');
      presentToday = new Set(d.present_today);
    } catch (e) { /* keep the old set */ }
    loadStudents();
  }
  $('refreshStudents').addEventListener('click', refresh);
  refresh();
  setInterval(refresh, 5000);   // keep the Today column fresh
  pollStatus();
}

/* ---------- page: camera ---------- */
function setStream(on) {
  const img = $('feed');
  if (!img) return;
  if (on && !streamOn) {
    img.src = '/video_feed?t=' + Date.now();   // fresh connection each time the camera starts
    img.hidden = false;
    streamOn = true;
  } else if (!on && streamOn) {
    img.removeAttribute('src');                // drops the connection and the frozen last frame
    img.hidden = true;
    streamOn = false;
  }
}

function renderFaces(enabled, faces) {
  const box = $('faces');
  box.textContent = '';
  if (!enabled || !faces.length) {
    const s = document.createElement('span');
    s.className = 'muted';
    s.textContent = enabled ? 'No face in view' : 'Camera is off';
    box.appendChild(s);
    return;
  }
  faces.forEach((f) => {
    const chip = document.createElement('span');
    chip.className = 'chip' + (f.name === 'Unknown' ? ' unknown' : '');
    chip.textContent = f.name;
    if (f.distance !== null) {
      const d = document.createElement('small');
      d.textContent = f.distance.toFixed(2);
      chip.appendChild(d);
    }
    box.appendChild(chip);
  });
}

function loadAttendance() {
  api('/api/attendance').then((d) => {
    $('present').textContent = d.present_today.length
      ? 'Present today (' + d.present_today.length + '): ' + d.present_today.join(', ')
      : 'Nobody marked present yet today.';
    const body = $('logBody');
    body.textContent = '';
    if (!d.entries.length) {
      const tr = document.createElement('tr');
      const td = cell('td', 'No entries yet.', 'empty');
      td.colSpan = 2;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    d.entries.forEach((e) => {
      const tr = document.createElement('tr');
      tr.append(cell('td', e.name), cell('td', e.time, 'time'));
      body.appendChild(tr);
    });
  }).catch(() => {
    lastSeq = -1;   // try again on the next status poll
  });
}

function initCameraPage() {
  $('camBtn').addEventListener('click', async () => {
    const btn = $('camBtn');
    btn.disabled = true;
    try {
      await api('/api/camera/' + (cameraEnabled ? 'stop' : 'start'), { method: 'POST' });
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false;
      pollStatus();
    }
  });

  $('clearBtn').addEventListener('click', async () => {
    if (!confirm('Clear the whole attendance log? This cannot be undone.')) return;
    try {
      await api('/api/attendance', { method: 'DELETE' });
      Object.keys(detailCache).forEach((k) => delete detailCache[k]);
      loadAttendance();
    } catch (e) {
      alert(e.message);
    }
  });

  loadAttendance();
  pollStatus();
}

/* ---------- page: enroll ---------- */
function setMsg(text, kind) {
  const el = $('enrollMsg');
  el.textContent = text;
  el.className = kind || '';
}

function initEnrollPage() {
  $('enrollForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const name = $('name').value.trim();
    if (!name) { setMsg('Enter the full name first.', 'bad'); return; }

    const btn = $('enrollBtn');
    btn.disabled = true;
    setMsg('Capturing… look at the camera and hold still for a few seconds.', '');
    try {
      const r = await api('/api/enroll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          class_name: $('className').value.trim(),
          year: $('year').value.trim(),
          roll_no: $('rollNo').value.trim(),
          replace: $('replace').checked,
        }),
      });
      setMsg('Saved ' + r.enrolled + ' samples for ' + r.name + '.', 'ok');
      $('name').value = '';
      $('className').value = '';
      $('year').value = '';
      $('rollNo').value = '';
      $('replace').checked = false;
    } catch (e) {
      setMsg(e.message, 'bad');
    } finally {
      btn.disabled = false;
    }
  });
  pollStatus();
}

/* ---------- boot ---------- */
$('quitBtn').addEventListener('click', async () => {
  if (!confirm('Stop the camera and shut the app down?')) return;
  try {
    await api('/api/quit', { method: 'POST' });
  } catch (e) {
    alert(e.message);
    return;
  }
  quitting = true;
  setStream(false);
  $('stopped').hidden = false;
});

markActiveNav();
const page = document.body.dataset.page;
if (page === 'students') initStudentsPage();
else if (page === 'camera') initCameraPage();
else if (page === 'enroll') initEnrollPage();
