// Small helpers only — no framework. The dashboard is internal and low-traffic.

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 9000 : 3500);
}

async function post(url, body, method) {
  const res = await fetch(url, {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body is fine */ }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

// Forms: serialize to JSON, coerce numeric inputs, reload on success. A form with
// data-goto navigates to that template instead, with {id} filled from the response.
document.querySelectorAll('form[data-post]').forEach((form) => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = form.querySelector('button[type=submit], button:not([type])');
    const payload = {};
    new FormData(form).forEach((value, key) => {
      const field = form.elements[key];
      const el = field instanceof RadioNodeList ? field[0] : field;
      payload[key] = el && el.type === 'number' ? Number(value) : value;
    });
    // Radio groups (the persona picker) submit a string; the API wants an int.
    if (payload.persona_id) payload.persona_id = Number(payload.persona_id);
    if (payload.client_id) payload.client_id = Number(payload.client_id);
    if (payload.target_duration) payload.target_duration = Number(payload.target_duration);

    // A page can rewrite the payload before it goes out — e.g. the Image Studio
    // collapses its target radio group into persona_id OR client_id.
    form.dispatchEvent(new CustomEvent('kalvid:payload', { detail: payload }));

    if (btn) btn.disabled = true;
    try {
      const data = await post(form.dataset.post, payload);
      if (form.dataset.goto && data && data.id != null) {
        location.href = form.dataset.goto.replace('{id}', data.id);
      } else {
        location.reload();
      }
    } catch (err) {
      toast(err.message, 'err');
      if (btn) btn.disabled = false;
    }
  });
});

// Action buttons. Anything that spends money confirms first, and the button is
// disabled the instant it is clicked so a double-click cannot fire two renders.
document.querySelectorAll('[data-action]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="w-4 h-4 rounded-full border-2 border-current/30 border-t-current animate-spin"></span> working…';
    try {
      await post(btn.dataset.action, {}, btn.dataset.method);
      location.reload();
    } catch (err) {
      toast(err.message, 'err');
      btn.disabled = false;
      btn.innerHTML = original;
    }
  });
});

// A job that is mid-render refreshes itself so the operator sees it land.
const poll = document.querySelector('[data-poll]');
if (poll) setTimeout(() => location.reload(), Number(poll.dataset.poll) * 1000);

// Show/hide a panel (the "Add influencer" / "Add client" forms).
document.querySelectorAll('[data-toggle]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const target = document.querySelector(btn.dataset.toggle);
    if (!target) return;
    target.classList.toggle('hidden');
    if (!target.classList.contains('hidden')) {
      target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      const first = target.querySelector('input:not([type=hidden]), select, textarea');
      if (first) first.focus();
    }
  });
});

// Client-side search over a list. Matches against each row's data-name.
document.querySelectorAll('[data-filter]').forEach((input) => {
  const scope = document.querySelector(input.dataset.filter);
  if (!scope) return;
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    scope.querySelectorAll('[data-name]').forEach((row) => {
      row.classList.toggle('hidden', q !== '' && !row.dataset.name.toLowerCase().includes(q));
    });
  });
});

// Filter chips over a grid. 'all' clears the filter.
document.querySelectorAll('[data-tab]').forEach((tab) => {
  tab.addEventListener('click', () => {
    const group = tab.dataset.tabGroup;
    const scope = document.querySelector(group);
    if (!scope) return;
    document.querySelectorAll(`[data-tab-group="${group}"]`).forEach((t) => {
      t.classList.toggle('chip-primary', t === tab);
    });
    const key = tab.dataset.tab;
    scope.querySelectorAll('[data-tab-item]').forEach((item) => {
      item.classList.toggle('hidden', key !== 'all' && item.dataset.tabItem !== key);
    });
  });
});

// Mobile side rail.
const rail = document.getElementById('rail');
const scrim = document.getElementById('rail-scrim');
const railToggle = document.getElementById('rail-toggle');
function setRail(open) {
  if (!rail) return;
  rail.classList.toggle('-translate-x-full', !open);
  if (scrim) scrim.classList.toggle('hidden', !open);
}
if (railToggle) railToggle.addEventListener('click', () => setRail(rail.classList.contains('-translate-x-full')));
if (scrim) scrim.addEventListener('click', () => setRail(false));
