// Small helpers only — no framework. The dashboard is internal and low-traffic.

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 9000 : 3500);
}

async function post(url, body) {
  const res = await fetch(url, {
    method: 'POST',
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

// Forms: serialize to JSON, coerce numeric inputs, reload on success.
document.querySelectorAll('form[data-post]').forEach((form) => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = form.querySelector('button');
    const payload = {};
    new FormData(form).forEach((value, key) => {
      const field = form.elements[key];
      payload[key] = field && field.type === 'number' ? Number(value) : value;
    });
    btn.disabled = true;
    try {
      await post(form.dataset.post, payload);
      location.reload();
    } catch (err) {
      toast(err.message, 'err');
      btn.disabled = false;
    }
  });
});

// Action buttons. Anything that spends money confirms first, and the button is
// disabled the instant it is clicked so a double-click cannot fire two renders.
document.querySelectorAll('[data-action]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'working…';
    try {
      await post(btn.dataset.action, {});
      location.reload();
    } catch (err) {
      toast(err.message, 'err');
      btn.disabled = false;
      btn.textContent = original;
    }
  });
});

// A job that is mid-render refreshes itself so the operator sees it land.
const poll = document.querySelector('[data-poll]');
if (poll) setTimeout(() => location.reload(), Number(poll.dataset.poll) * 1000);
