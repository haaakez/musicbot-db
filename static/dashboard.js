'use strict';

const state = JSON.parse(document.getElementById('dashboard-data').textContent);
const text = (key, values = {}) => (state.messages?.[key] || key.toLowerCase()).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ''));
const filters = document.getElementById('filters');
const statusLine = document.getElementById('save-status');
const users = new Map(state.users.map(user => [user.id, user]));
let liveEncoded = null;
const playbackForm = document.getElementById('playback-controls');
let controlsBusy = false;
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
    feedback.textContent = '';
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
      feedback.textContent = '';
      const count = result.progress[form.dataset.userId] || 0;
      for (const element of document.querySelectorAll('[data-progress-user]')) {
        if (element.dataset.progressUser === form.dataset.userId) element.textContent = `${count} / ${result.library_total}`;
      }
      for (const meter of document.querySelectorAll('[data-meter-user]')) {
        if (meter.dataset.meterUser === form.dataset.userId) meter.value = count;
      }
      updateRow(row);
      updatePreviews();
      statusLine.textContent = '';
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

updatePreviews();

const botSettingsForm = document.getElementById('bot-settings');
if (botSettingsForm) {
  const feedback = document.getElementById('bot-settings-status');
  const controls = [...botSettingsForm.querySelectorAll('select, button')];
  function busy(value) { controls.forEach(control => { control.disabled = value; }); }
  botSettingsForm.elements.guild_id.addEventListener('change', async () => {
    const url = new URL(botSettingsForm.action);
    url.searchParams.set('guild_id', botSettingsForm.elements.guild_id.value);
    busy(true);
    try {
      const response = await fetch(url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || text('Could not save. Try again.'));
      for (const [name, value] of Object.entries(result.settings)) botSettingsForm.elements[name].value = value ? 'on' : 'off';
      feedback.textContent = '';
      feedback.classList.remove('error');
      delete botSettingsForm.querySelector('button').dataset.loadFailed;
    } catch (error) {
      feedback.textContent = error.message;
      feedback.classList.add('error');
      // A failed server switch must not leave another server's settings ready to save.
      botSettingsForm.querySelector('button').dataset.loadFailed = 'true';
    } finally {
      busy(false);
      if (botSettingsForm.querySelector('button').dataset.loadFailed === 'true') botSettingsForm.querySelector('button').disabled = true;
    }
  });
  botSettingsForm.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(botSettingsForm);
    busy(true);
    feedback.textContent = text('Saving…');
    try {
      await postForm(botSettingsForm, data);
      feedback.textContent = text('Saved');
      feedback.classList.remove('error');
    } catch (error) {
      feedback.textContent = error.message;
      feedback.classList.add('error');
    } finally { busy(false); }
  });
}

// Keep the native select available when JavaScript is disabled.
document.querySelector('.view-shortcuts').hidden = false;
document.getElementById('view-select-label').hidden = true;
// Quick views keep the current search, listener selection and other filters.
for (const button of document.querySelectorAll('[data-view]')) {
  button.addEventListener('click', () => {
    filters.elements.view.value = button.dataset.view;
    if (button.dataset.view === 'missing' && !filters.elements.viewer.value) {
      filters.elements.viewer.focus();
      filters.elements.viewer.required = true;
      filters.elements.viewer.reportValidity();
      return;
    }
    filters.elements.viewer.required = false;
    filters.requestSubmit();
  });
}
function highlightIdentity() {
  const id = filters.elements.viewer.value;
  for (const cell of document.querySelectorAll('[data-rating-user]')) {
    cell.classList.toggle('my-rating', cell.dataset.ratingUser === id);
  }
}
filters.elements.viewer.addEventListener('change', () => {
  highlightIdentity();
  if (filters.elements.view.value === 'missing') filters.requestSubmit();
});
highlightIdentity();
// Slash focuses search without interfering with typing or browser shortcuts.
document.addEventListener('keydown', event => {
  if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey &&
      !event.target.closest('input, select, textarea, [contenteditable]')) {
    event.preventDefault();
    filters.elements.q.focus();
    filters.elements.q.select();
  }
});

const backupForm = document.getElementById('backup-settings');
backupForm.addEventListener('submit', async event => {
  event.preventDefault();
  const feedback = document.getElementById('backup-settings-status');
  const button = backupForm.querySelector('button');
  const data = new FormData(backupForm);
  button.disabled = true;
  try {
    await postForm(backupForm, data);
    feedback.textContent = text('Saved');
    feedback.classList.remove('error');
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add('error');
  } finally { button.disabled = false; }
});

const runtimePanel = document.querySelector('.runtime-panel');
async function refreshRuntime() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const url = new URL(runtimePanel.dataset.statusUrl, window.location.href);
    url.searchParams.set('guild_id', botSettingsForm?.elements.guild_id.value || '');
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error('status unavailable');
    const result = await response.json();
    const connection = document.getElementById('connection-status');
    const current = document.getElementById('current-track');
    current.replaceChildren();
    liveEncoded = !result.stale && result.status?.voice ? result.status.encoded : null;
    playbackForm.querySelectorAll('button').forEach(button => { button.disabled = controlsBusy || !liveEncoded; });
    if (result.stale) {
      connection.textContent = text('Bot offline or status outdated');
      connection.classList.add('error');
    } else {
      const status = result.status;
      connection.classList.toggle('error', !status.discord || !status.lavalink);
      connection.textContent = `${text('Discord')}: ${text(status.discord ? 'Connected' : 'Disconnected')} · ` +
        `${text('Lavalink')}: ${text(status.lavalink ? 'Connected' : 'Disconnected')} · ` +
        `${text('Voice')}: ${status.voice ? status.channel : text('Disconnected')}`;
      if (status.title) {
        const label = document.createElement('span');
        label.textContent = `${text(status.paused ? 'Paused' : 'Playing')}: `;
        const title = document.createElement(/^https?:\/\//i.test(status.url || '') ? 'a' : 'span');
        title.textContent = status.title;
        if (title.tagName === 'A') { title.href = status.url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
        current.append(label, title, document.createTextNode(` · ${status.source || ''} · ${text('Listeners')}: ${status.listeners}`));
      } else {
        current.textContent = text(status.waiting || 'Nothing playing');
      }
    }
    const backup = result.backup;
    document.getElementById('backup-status').textContent = backup.last_error
      ? `${text('Backup failed')}: ${backup.last_error}`
      : backup.last_success
        ? `${text('Last backup')}: ${new Date(backup.last_success * 1000).toLocaleString()} · ${text('Interval')}: ${backup.interval_hours} ${text('hours')}`
        : text('No automatic backup recorded yet');
  } catch (_) {
    liveEncoded = null;
    playbackForm.querySelectorAll('button').forEach(button => { button.disabled = true; });
    document.getElementById('connection-status').textContent = text('Status unavailable');
    document.getElementById('connection-status').classList.add('error');
    document.getElementById('current-track').replaceChildren();
  } finally {
    clearTimeout(timeout);
    refreshHistory();
    setTimeout(refreshRuntime, 5000);
  }
}
refreshRuntime();


playbackForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!liveEncoded || controlsBusy) return;
  const action = event.submitter?.value;
  const feedback = document.getElementById('playback-control-status');
  const data = new FormData(playbackForm);
  data.set('action', action);
  data.set('expected', liveEncoded);
  data.set('guild_id', botSettingsForm?.elements.guild_id.value || '');
  controlsBusy = true;
  playbackForm.querySelectorAll('button').forEach(button => { button.disabled = true; });
  feedback.textContent = text('Sending…');
  feedback.classList.remove('error');
  try {
    const result = await postForm(playbackForm, data);
    const deadline = Date.now() + 22000;
    let completed = false;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      const url = playbackForm.dataset.resultUrl.replace(/0$/, String(result.id));
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
      const status = await response.json();
      if (!response.ok || status.result === 'error') throw new Error(text(status.error || 'Request expired.'));
      if (status.result === 'done') { completed = true; break; }
    }
    if (!completed) throw new Error(text('Request expired.'));
    feedback.textContent = text('Done');
    // Wait for the next heartbeat before allowing another action on an old track.
    liveEncoded = null;
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add('error');
  } finally { controlsBusy = false; }
});

const historyPanel = document.getElementById('recent-history');
let historyBusy = false;
async function refreshHistory() {
  if (!historyPanel.open || historyBusy) return;
  historyBusy = true;
  try {
    const url = new URL(historyPanel.dataset.url, window.location.href);
    url.searchParams.set('guild_id', botSettingsForm?.elements.guild_id.value || '');
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error();
    const result = await response.json();
    const body = document.getElementById('history-rows');
    body.replaceChildren();
    for (const item of result.history) {
      const row = document.createElement('tr');
      for (const value of [item.title, item.source, item.requester, new Date(item.started_at * 1000).toLocaleString(), text(item.result)]) {
        const cell = document.createElement('td');
        cell.textContent = value || '—';
        row.append(cell);
      }
      body.append(row);
    }
    if (!result.history.length) {
      const row = body.insertRow();
      const cell = row.insertCell(); cell.colSpan = 5; cell.textContent = text('No history yet.');
    }
  } catch (_) {
    const body = document.getElementById('history-rows'); body.replaceChildren();
    const cell = body.insertRow().insertCell(); cell.colSpan = 5; cell.textContent = text('History unavailable.');
  } finally { historyBusy = false; }
}
historyPanel.addEventListener('toggle', refreshHistory);


// Reload library data while keeping applied filters and the selected server.
document.getElementById('dashboard-refresh').addEventListener('click', event => {
  if (document.querySelector('.rating-form select:disabled, .threshold-form select:disabled') || controlsBusy) {
    event.preventDefault();
    statusLine.textContent = text('Wait for the current save or playback action to finish.');
    return;
  }
  const url = new URL(window.location.href);
  if (botSettingsForm) url.searchParams.set('settings_guild', botSettingsForm.elements.guild_id.value);
  event.currentTarget.href = url.toString();
});
