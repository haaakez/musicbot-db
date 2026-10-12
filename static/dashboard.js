'use strict';

const state = JSON.parse(document.getElementById('dashboard-data').textContent);
const text = (key, values = {}) => (state.messages?.[key] || key.toLowerCase()).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ''));
const filters = document.getElementById('filters');
const statusLine = document.getElementById('save-status');
const users = new Map(state.users.map(user => [user.id, user]));
const rows = [...document.querySelectorAll('tr[data-song-id]')];
const valueOf = score => score === null || score === undefined ? 'unrated' : String(score);

async function postForm(form, data) {
  const response = await fetch(form.action, {
    method: 'POST', body: data, headers: {Accept: 'application/json'}, credentials: 'same-origin'
  });
  if (!response.headers.get('Content-Type')?.includes('application/json')) {
    throw new Error(text('Could not save. Reload the page to check your session and current ratings.'));
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || text('Could not save. Try again.'));
  return result;
}

function previewSelection() {
  const listeners = [...filters.querySelectorAll('[name=listener]:checked')].map(input => input.value);
  const threshold = Math.max(0, ...listeners.map(id => users.get(id).threshold));
  return {listeners, threshold, ignoreUnrated: filters.elements.ignore_unrated.value === 'on'};
}

function updatePreviews() {
  const {listeners, threshold, ignoreUnrated} = previewSelection();
  document.getElementById('preview-rule').textContent = listeners.length
    ? text('Every rating must be ≥ {threshold}; unrated {rule}', {threshold, rule: text(ignoreUnrated ? 'allowed' : 'blocked')})
    : text('Select friends to preview');
  for (const row of rows) {
    const cell = row.querySelector('[data-preview]');
    const scores = state.ratings[row.dataset.songId] || {};
    cell.replaceChildren();
    if (!listeners.length) {
      cell.textContent = text('Select friends');
      continue;
    }
    const blockers = [];
    for (const id of listeners) {
      const score = scores[id];
      const name = users.get(id).name;
      if (score === -1) blockers.push(text('{name}: veto', {name}));
      else if (score === undefined && !ignoreUnrated) blockers.push(text('{name}: unrated', {name}));
      else if (score !== undefined && score < threshold) blockers.push(text('{name}: {score} below {threshold}', {name, score, threshold}));
    }
    const label = document.createElement('span');
    label.className = blockers.length ? 'blocked' : 'qualifies';
    label.textContent = blockers.length ? text('Blocked') : text('Qualifies');
    cell.append(label);
    if (blockers.length) {
      const explanation = document.createElement('span');
      explanation.className = 'blockers';
      explanation.textContent = blockers.join('; ');
      cell.append(explanation);
    }
  }
}

function updateNavigation() {
  const {listeners, ignoreUnrated} = previewSelection();
  for (const link of document.querySelectorAll('.page-link, .sort-link')) {
    const url = new URL(link.href);
    url.searchParams.delete('listener');
    listeners.forEach(id => url.searchParams.append('listener', id));
    url.searchParams.set('ignore_unrated', ignoreUnrated ? 'on' : 'off');
    link.href = url.toString();
  }
}

for (const input of filters.querySelectorAll('[name=listener], [name=ignore_unrated]')) {
  input.addEventListener('change', () => { updatePreviews(); updateNavigation(); });
}

function updateRow(row) {
  const scores = Object.values(state.ratings[row.dataset.songId] || {});
  const positive = scores.filter(score => score >= 0);
  row.querySelector('[data-average]').textContent = positive.length
    ? String(Math.round(positive.reduce((a, b) => a + b, 0) / positive.length * 100) / 100) : '—';
  row.querySelector('[data-coverage]').textContent = `${scores.length} / ${users.size}`;
}

for (const form of document.querySelectorAll('.rating-form')) {
  const select = form.elements.score;
  const row = form.closest('[data-song-id]');
  const cell = form.closest('.rating-cell');
  const feedback = cell.querySelector('.cell-status');
  const undo = cell.querySelector('.undo-rating');
  let saved = select.value;
  let undoValue = null;
  let busy = false;
  async function save(value, isUndo = false) {
    if (busy) return;
    busy = true;
    select.disabled = true;
    undo.disabled = true;
    feedback.textContent = text('Saving…');
    feedback.classList.remove('error');
    // FormData omits disabled selects, so set the intended value explicitly.
    const data = new FormData(form);
    data.set('score', value);
    data.set('expected', saved);
    try {
      const result = await postForm(form, data);
      saved = valueOf(result.score);
      select.value = saved;
      select.dataset.score = saved;
      const scores = state.ratings[row.dataset.songId];
      if (result.score === null) delete scores[form.dataset.userId];
      else scores[form.dataset.userId] = result.score;
      undoValue = isUndo ? null : valueOf(result.previous);
      undo.hidden = isUndo || undoValue === saved;
      feedback.textContent = isUndo ? text('Undone') : text('Saved');
      const count = result.progress[form.dataset.userId] || 0;
      for (const element of document.querySelectorAll('[data-progress-user]')) {
        if (element.dataset.progressUser === form.dataset.userId) element.textContent = `${count} / ${result.library_total}`;
      }
      for (const meter of document.querySelectorAll('[data-meter-user]')) {
        if (meter.dataset.meterUser === form.dataset.userId) meter.value = count;
      }
      updateRow(row);
      updatePreviews();
      statusLine.textContent = text('Rating saved. Apply the view again to refresh filtering or sort order.');
      statusLine.classList.remove('error');
    } catch (error) {
      select.value = saved;
      feedback.textContent = text('Not saved');
      feedback.classList.add('error');
      statusLine.textContent = error.message;
      statusLine.classList.add('error');
    } finally {
      select.disabled = false;
      undo.disabled = false;
      busy = false;
    }
  }
  select.addEventListener('change', () => save(select.value));
  form.addEventListener('submit', event => { event.preventDefault(); save(select.value); });
  undo.addEventListener('click', () => { if (undoValue !== null) save(undoValue, true); });
}

for (const form of document.querySelectorAll('.threshold-form')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const select = form.elements.min_rating;
    const data = new FormData(form);
    select.disabled = true;
    try {
      const result = await postForm(form, data);
      users.get(result.user_id).threshold = result.min_rating;
      updatePreviews();
      statusLine.textContent = text('Minimum saved. The listening preview has been updated.');
      statusLine.classList.remove('error');
    } catch (error) {
      statusLine.textContent = error.message;
      statusLine.classList.add('error');
      // Restore the persisted value rather than displaying an unsaved minimum.
      const id = form.dataset.userId;
      select.value = String(users.get(id).threshold);
    } finally {
      select.disabled = false;
    }
  });
}

const namesForm = document.getElementById('refresh-names');
namesForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = namesForm.querySelector('button');
  button.disabled = true;
  statusLine.textContent = text('Looking up Discord usernames…');
  try {
    const result = await postForm(namesForm, new FormData(namesForm));
    if (result.updated) window.location.reload();
    else {
      statusLine.textContent = text('No names could be refreshed. Check the bot token and Discord connection. Cached names are kept.');
      statusLine.classList.add('error');
    }
  } catch (error) {
    statusLine.textContent = error.message;
    statusLine.classList.add('error');
  } finally {
    button.disabled = false;
  }
});

// Saved custom views belong to this browser; the built-in views work without storage.
const storageKey = 'musicbot.library.views.v1';
const savedSelect = document.getElementById('saved-view');
const viewStatus = document.getElementById('view-status');
let savedViews = [];
try {
  const stored = JSON.parse(localStorage.getItem(storageKey) || '[]');
  if (Array.isArray(stored)) savedViews = stored.filter(view => typeof view.name === 'string' && typeof view.query === 'string');
} catch (_) { viewStatus.textContent = text('Browser storage unavailable.'); }
function renderSavedViews() {
  savedSelect.replaceChildren(new Option(text('Choose a saved view'), ''));
  savedViews.forEach((view, index) => savedSelect.add(new Option(view.name, String(index))));
}
function persistViews() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(savedViews));
    renderSavedViews();
    return true;
  } catch (_) {
    viewStatus.textContent = text('Could not save: browser storage unavailable.');
    return false;
  }
}
document.getElementById('save-view').addEventListener('click', () => {
  const input = document.getElementById('view-name');
  const name = input.value.trim();
  if (!name) { viewStatus.textContent = text('Enter a name first.'); input.focus(); return; }
  const query = new URLSearchParams(new FormData(filters));
  const view = {name, query: query.toString()};
  const existing = savedViews.findIndex(item => item.name === name);
  if (existing >= 0) savedViews[existing] = view;
  else savedViews.push(view);
  if (persistViews()) { viewStatus.textContent = text('View saved in this browser.'); input.value = ''; }
});
savedSelect.addEventListener('change', () => {
  const view = savedViews[Number(savedSelect.value)];
  if (savedSelect.value !== '' && view) window.location.assign(`${filters.action}?${view.query}`);
});
document.getElementById('delete-view').addEventListener('click', () => {
  if (savedSelect.value === '') { viewStatus.textContent = text('Choose a saved view first.'); return; }
  savedViews.splice(Number(savedSelect.value), 1);
  if (persistViews()) viewStatus.textContent = text('Saved view deleted.');
});
renderSavedViews();
updatePreviews();
