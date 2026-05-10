let token = null;
async function api(path, method='GET', body=null){
  const res = await fetch(path,{method,headers:{'Content-Type':'application/json','X-Auth-Token':token??''},body:body?JSON.stringify(body):null});
  if(!res.ok) throw new Error(await res.text());
  return await res.json();
}
async function login(){
  const data = await api('/api/login','POST',{username:username.value,password:password.value});
  token = data.token; loginResult.textContent = `Logged in as ${data.username}`;
}
async function startIndex(){
  const data = await api('/api/index/start','POST',{paths:paths.value.split(',').map(s=>s.trim()).filter(Boolean)});
  const id = data.job_id; indexResult.textContent = `Job ${id} started`;
  const interval = setInterval(async()=>{ const j = await api(`/api/index/jobs/${id}`); indexResult.textContent = JSON.stringify(j,null,2); if(j.status==='finished') clearInterval(interval);},1200);
}
async function saveTags(documentId){
  const input = document.getElementById(`tags-${documentId}`);
  const tags = input.value.split(',').map(s=>s.trim()).filter(Boolean);
  await api('/api/documents/tags','POST',{document_id:documentId,tags});
}
async function toggleMark(documentId, current){
  await api('/api/documents/mark','POST',{document_id:documentId,is_marked:!current});
  await runSearch();
}
async function runSearch(){
  const payload = {query:query.value,limit:25,filetype:filetype.value||null,path:pathFilter.value||null,block_type:blockType.value||null,modified_from:modifiedFrom.value||null,modified_to:modifiedTo.value||null};
  const data = await api('/api/search','POST',payload);
  results.innerHTML = data.map(r=>`<div class='result'><b>${r.filename}</b> · ${r.block_type} ${r.block_number} ${r.is_marked ? '⭐' : ''}<br/><small>${r.path}</small><p>${r.snippet_html ?? ''}</p><div><input id='tags-${r.document_id}' value='${(r.tags||[]).join(', ')}' placeholder='tag1,tag2'/><button onclick='saveTags(${r.document_id})'>Save tags</button><button onclick='toggleMark(${r.document_id}, ${r.is_marked ? 'true':'false'})'>${r.is_marked ? 'Unmark':'Mark'}</button> <a href='${r.open_url}' target='_blank'>Open file</a></div></div>`).join('');
}


async function loadConfig(){
  const c = await api('/api/config');
  cfgDb.value = c.database_path ?? '';
  cfgExt.value = (c.supported_extensions || []).join(',');
  cfgExcludeDirs.value = (c.exclude_dirs || []).join(',');
  cfgExcludePatterns.value = (c.exclude_patterns || []).join(',');
  cfgMaxSize.value = c.max_file_size_mb ?? 100;
  configResult.textContent = 'Loaded config';
}
async function saveConfig(){
  const payload = {
    database_path: cfgDb.value,
    supported_extensions: cfgExt.value.split(',').map(s=>s.trim()).filter(Boolean),
    exclude_dirs: cfgExcludeDirs.value.split(',').map(s=>s.trim()).filter(Boolean),
    exclude_patterns: cfgExcludePatterns.value.split(',').map(s=>s.trim()).filter(Boolean),
    max_file_size_mb: Number(cfgMaxSize.value || 100),
  };
  const res = await api('/api/config','POST',payload);
  configResult.textContent = JSON.stringify(res,null,2);
}


async function uploadDocument(){
  const f = uploadFile.files[0];
  if(!f){ uploadResult.textContent = "Select a file first"; return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("target_subpath", uploadPath.value || "");
  fd.append("tags", uploadTags.value || "");
  fd.append("metadata_json", uploadMeta.value || "{}");
  const res = await fetch("/api/upload", {method:"POST", headers:{"X-Auth-Token": token??""}, body: fd});
  uploadResult.textContent = JSON.stringify(await res.json(), null, 2);
}

async function runUpdate(){
  const res = await api("/api/update/run", "POST", {});
  updateResult.textContent = JSON.stringify(res, null, 2);
}
