'use strict';
const $ = id => document.getElementById(id);
let csrf = document.querySelector('meta[name="csrf-token"]').content;
let current = null, busy = false, loading = false;
const labels = {queued: 'Wartet', claimed: 'Wird übertragen', sent: 'Gesendet', uncertain: 'Ausdruck prüfen'};
function notice(text, error = false) {
  $('notice').textContent = text;
  $('notice').classList.toggle('error', error);
}
async function fetchJSON(url, options) {
  const response = await fetch(url, {cache: 'no-store', credentials: 'same-origin', ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Anfrage fehlgeschlagen (${response.status}). Bitte neu laden.`);
  return data;
}
async function action(name, data) {
  if (busy) return false;
  busy = true;
  try {
    const result = await fetchJSON('/admin/api/' + name, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(data)
    });
    if (result.csrf) csrf = result.csrf;
    notice(result.message);
    await refresh();
    return true;
  } catch (error) { notice(error.message, true); return false; }
  finally { busy = false; }
}
function cell(row, value, className = '') {
  const td = document.createElement('td'); td.textContent = value; td.className = className; if (className === 'confession') td.dir = 'auto'; row.append(td); return td;
}
function draw(data) {
  current = data; csrf = data.csrf;
  $('total').textContent = data.jobs.length;
  $('queued').textContent = data.jobs.filter(j => j.state === 'queued').length;
  $('sent').textContent = data.jobs.filter(j => j.state === 'sent').length;
  $('next').textContent = data.nextNumber;
  $('print-status').textContent = data.paused ? 'Druck pausiert' : 'Druck freigegeben';
  $('print-status').classList.toggle('paused', data.paused);
  $('pause-note').hidden = !data.paused;
  $('pause').textContent = data.paused ? 'Druck fortsetzen' : 'Druck pausieren';
  $('pause').disabled = false;
  $('open-reset').disabled = !data.paused || data.jobs.some(j => j.state === 'claimed');
  const fragment = document.createDocumentFragment();
  for (const job of data.jobs) {
    const row = document.createElement('tr');
    cell(row, job.number, 'number');
    cell(row, job.confession ?? 'Text aus altem Stand nicht gespeichert.', 'confession');
    const status = cell(row, labels[job.state] || job.state);
    if (job.reprints) { const note = document.createElement('small'); note.textContent = `${job.reprints} Nachdruckauftrag/-aufträge`; status.append(note); }
    const container = document.createElement('div'); container.className = 'actions';
    const preview = document.createElement('button'); preview.textContent = 'Vorschau'; preview.disabled = !job.canPreview;
    preview.addEventListener('click', () => {
      $('preview-title').textContent = 'Confession no. ' + job.number;
      $('preview-image').src = `/admin/receipt/${job.id}.png?run=${encodeURIComponent(data.runId)}`;
      $('preview').showModal();
    });
    const reprint = document.createElement('button'); reprint.textContent = 'Nachdrucken'; reprint.disabled = !job.canReprint;
    reprint.title = job.state === 'queued' || job.state === 'claimed' ? 'Bereits in der Druckwarteschlange.' : '';
    reprint.addEventListener('click', async () => {
      const caution = job.state === 'uncertain' ? 'Bitte zuerst am Drucker prüfen, ob der Bon bereits gedruckt wurde.\n\n' : '';
      if (!confirm(`${caution}Bon ${job.number} erneut drucken? Die Nummer bleibt gleich.`)) return;
      reprint.disabled = true;
      await action('reprint', {id: job.id, revision: job.revision});
      reprint.disabled = !job.canReprint;
    });
    container.append(preview, reprint); cell(row, '').append(container); fragment.append(row);
  }
  if (!data.jobs.length) { const row = document.createElement('tr'); cell(row, 'Noch keine Beichten in diesem Durchlauf.').colSpan = 4; fragment.append(row); }
  $('jobs').replaceChildren(fragment);
  $('updated').textContent = 'Stand ' + new Date().toLocaleTimeString('de-AT');
}
async function refresh() {
  if (loading) return;
  loading = true;
  try { draw(await fetchJSON('/admin/api/state')); }
  catch (error) { notice(error.message, true); }
  finally { loading = false; }
}
$('pause').addEventListener('click', () => current && action('pause', {paused: !current.paused}));
$('refresh').addEventListener('click', refresh);
$('close-preview').addEventListener('click', () => $('preview').close());
$('preview-image').addEventListener('error', () => { $('preview').close(); notice('Vorschau nicht verfügbar. Bitte aktualisieren.', true); });
$('open-reset').addEventListener('click', () => { $('reset-confirm').value = ''; $('reset-error').textContent = ''; $('reset-dialog').showModal(); });
$('cancel-reset').addEventListener('click', () => $('reset-dialog').close());
$('reset-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('reset-confirm').value !== 'RESET 001') { $('reset-error').textContent = 'Bitte exakt RESET 001 eingeben.'; return; }
  $('confirm-reset').disabled = true;
  if (await action('reset', {confirmation: $('reset-confirm').value})) $('reset-dialog').close();
  else $('reset-error').textContent = $('notice').textContent;
  $('confirm-reset').disabled = false;
});
refresh();
setInterval(() => { if (!busy && !$('reset-dialog').open) refresh(); }, 10000);
