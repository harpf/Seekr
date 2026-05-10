let token = localStorage.getItem('documentSearchToken');

async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Auth-Token': token ?? '' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  return await res.json();
}

function setText(id, message, isError = false) {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = message;
  node.classList.toggle('error', isError);
}

function showAuthedPanels() {
  document.getElementById('appPanel')?.classList.remove('hidden');
  document.getElementById('configPanel')?.classList.remove('hidden');
  document.getElementById('statusPanel')?.classList.remove('hidden');
}

function formatBytes(bytes) {
  const size = Number(bytes || 0);
  if (size <= 0) return '0 MB';
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

async function loadStatus() {
  try {
    const status = await api('/api/status');
    document.getElementById('welcomeText') && (document.getElementById('welcomeText').textContent = 'Du bist angemeldet. Hier ist der aktuelle Verarbeitungsstatus.');
    document.getElementById('statDocuments') && (document.getElementById('statDocuments').textContent = String(status.documents ?? 0));
    document.getElementById('statBlocks') && (document.getElementById('statBlocks').textContent = String(status.content_blocks ?? 0));
    document.getElementById('statStorage') && (document.getElementById('statStorage').textContent = formatBytes(status.total_file_size_bytes ?? 0));
  } catch (_) {}
}

async function login(options = { redirectToApp: true }) {
  try {
    const data = await api('/api/login', 'POST', { username: username.value, password: password.value });
    token = data.token;
    localStorage.setItem('documentSearchToken', token);
    setText('loginResult', `Logged in as ${data.username}`);
    showAuthedPanels();
    await loadStatus();
    if (options.redirectToApp === false && document.getElementById('configPanel')) await loadConfig();
  } catch (error) {
    setText('loginResult', `Login failed: ${error.message}`, true);
  }
}

async function startIndex(){ try {
  const data = await api('/api/index/start','POST',{paths:paths.value.split(',').map(s=>s.trim()).filter(Boolean)});
  const id = data.job_id; indexResult.textContent = `Job ${id} started`;
  const interval = setInterval(async()=>{ const j = await api(`/api/index/jobs/${id}`); indexResult.textContent = JSON.stringify(j,null,2); if(j.status==='finished') clearInterval(interval);},1200);
} catch (e) { indexResult.textContent = e.message; } }

async function saveTags(documentId){ const input = document.getElementById(`tags-${documentId}`); const tags = input.value.split(',').map(s=>s.trim()).filter(Boolean); await api('/api/documents/tags','POST',{document_id:documentId,tags}); }
async function toggleMark(documentId, current){ await api('/api/documents/mark','POST',{document_id:documentId,is_marked:!current}); await runSearch(); }

async function runSearch(){ try {
  const payload = {query:query.value,limit:25,filetype:filetype.value||null,path:pathFilter.value||null,block_type:blockType.value||null,modified_from:modifiedFrom.value||null,modified_to:modifiedTo.value||null};
  const data = await api('/api/search','POST',payload);
  results.innerHTML = data.map(r=>`<div class='result'><b>${r.filename}</b> · ${r.block_type} ${r.block_number} ${r.is_marked ? '⭐' : ''}<br/><small>${r.path}</small><p>${r.snippet_html ?? ''}</p><div><input id='tags-${r.document_id}' value='${(r.tags||[]).join(', ')}' placeholder='tag1,tag2'/><button class='btn' onclick='saveTags(${r.document_id})'>Save tags</button><button class='btn btn-secondary' onclick='toggleMark(${r.document_id}, ${r.is_marked ? 'true':'false'})'>${r.is_marked ? 'Unmark':'Mark'}</button> <a href='${r.open_url}' target='_blank'>Open file</a></div></div>`).join('');
} catch (e) { results.textContent = e.message; } }

async function loadConfig(){
  try {
    const c = await api('/api/config');
    cfgDb.value = c.database_path ?? '';
    cfgExt.value = (c.supported_extensions || []).join(',');
    cfgExcludeDirs.value = (c.exclude_dirs || []).join(',');
    cfgExcludePatterns.value = (c.exclude_patterns || []).join(',');
    cfgMaxSize.value = c.max_file_size_mb ?? 100;
    configResult.textContent = 'Loaded config';
  } catch (e) { configResult.textContent = e.message; }
}

async function saveConfig(){
  try {
    const payload = {
      database_path: cfgDb.value,
      supported_extensions: cfgExt.value.split(',').map(s=>s.trim()).filter(Boolean),
      exclude_dirs: cfgExcludeDirs.value.split(',').map(s=>s.trim()).filter(Boolean),
      exclude_patterns: cfgExcludePatterns.value.split(',').map(s=>s.trim()).filter(Boolean),
      max_file_size_mb: Number(cfgMaxSize.value || 100),
    };
    const res = await api('/api/config','POST',payload);
    configResult.textContent = JSON.stringify(res,null,2);
  } catch (e) { configResult.textContent = e.message; }
}

async function uploadDocument(){
  const f = uploadFile.files[0]; if(!f){ uploadResult.textContent = 'Select a file first'; return; }
  const fd = new FormData(); fd.append('file', f); fd.append('target_subpath', uploadPath.value || ''); fd.append('tags', uploadTags.value || ''); fd.append('metadata_json', uploadMeta.value || '{}');
  const res = await fetch('/api/upload', {method:'POST', headers:{'X-Auth-Token': token??''}, body: fd});
  uploadResult.textContent = JSON.stringify(await res.json(), null, 2);
}

async function runUpdate(){ try { const res = await api('/api/update/run','POST',{}); updateResult.textContent = JSON.stringify(res,null,2);} catch(e){updateResult.textContent=e.message;} }

function initNav() {
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config' };
  const activeHref = map[document.body?.dataset?.page || ''];
  document.querySelectorAll('.nav-links a').forEach((link) => {
    if (link.getAttribute('href') === activeHref) link.classList.add('active');
  });
}

async function bootstrap() {
  initNav();
  if (token) {
    showAuthedPanels();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
  }
}

bootstrap();
