'use strict';
const $ = id => document.getElementById(id);
const element = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
const chip = (text, kind = 'neutral') => element('span', text, 'chip ' + kind);
const S = {data: null, choices: new Map(), selected: new Set(), conflicts: [], busy: false, page: 'mapping', preview: null, previewId: null, detail: null, detailTab: 'packages'};
let token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
try { token ||= sessionStorage.getItem('inventory-token') || ''; if (token) sessionStorage.setItem('inventory-token', token); if (location.hash) history.replaceState(null, '', location.pathname); } catch (_) {}
const date = value => { if (!value) return 'Noch keine'; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('de-DE', {dateStyle:'medium', timeStyle:'short'}); };
const targetKey = c => c.type + ':' + c.id;
const prettyType = type => type === 'devices' ? 'Device' : 'VM';
const candidateFor = row => row.candidates.find(c => targetKey(c) === S.choices.get(row.node_id));
const available = (row,c) => (!c.identity_key || c.identity_key === S.data.source_id + ':' + row.node_id) && (!c.bound_node_id || (c.bound_node_id === row.node_id && (!c.bound_source_id || c.bound_source_id === S.data.source_id)));
const mappingConflicts = () => [...InventoryMapping.selectionConflicts(S.data?.nodes || [], S.choices, S.selected), ...S.conflicts.filter(c => c.code !== 'duplicate_target' && c.node_ids.some(id => S.selected.has(id)))];

async function api(path, body) {
  const response = await fetch(path, {method:body == null ? 'GET':'POST', headers:{Authorization:'Bearer '+token, ...(body == null ? {} : {'Content-Type':'application/json'})}, ...(body == null ? {} : {body:JSON.stringify(body)}), cache:'no-store'});
  const result = await response.json();
  if (response.status === 401 || (response.status === 403 && !S.data)) showAuth();
  if (!response.ok) throw Error(result.error || 'Anfrage fehlgeschlagen.');
  return result;
}
function showAuth(){ $('auth-panel').classList.remove('hidden'); $('app-panel').classList.add('hidden'); }
function notify(message, kind=''){ const panel=$('notice'); panel.className='notice'+(kind?' '+kind:''); panel.textContent=message; }
function setBusy(value) {
  S.busy=value;
  for(const id of ['import-button','refresh-button','select-clear','preview-button']) $(id).disabled=value;
  $('save-button').disabled=value || !S.selected.size || S.data?.demo || mappingConflicts().length > 0;
  $('apply-button').disabled=value || !S.preview?.some(row=>row.action==='would_update') || S.preview?.some(row=>row.action==='failed') || S.data?.demo;
}
async function loadState(){
  S.data=await api('/api/state');
  $('auth-panel').classList.add('hidden');$('app-panel').classList.remove('hidden');
  for(const row of S.data.nodes) {
    if(!S.choices.has(row.node_id) && row.candidates.length===1) S.choices.set(row.node_id,targetKey(row.candidates[0]));
    if(row.existing_binding) S.selected.delete(row.node_id);
  }
  render();
}
function stats(){
  const rows=S.data.nodes, bound=rows.filter(r=>r.existing_binding).length, conflict=rows.filter(r=>!r.existing_binding&&r.candidates.length>1).length;
  const values=[
    ['Erfasste Nodes',rows.length,'Aus OpenNMS übernommen','▤',''],
    ['Noch zuzuordnen',rows.length-bound,'Auswahl wird lokal gespeichert','⇄',''],
    ['Mehrere Kandidaten',conflict,'Name, DNS und IP vergleichen','◇','conflict'],
    ['Bestätigt',bound,'Dauerhaft mit NetBox verknüpft','✓','active']
  ];
  $('stats').replaceChildren(...values.map(([label,number,sub,icon,cls])=>{const box=element('div',null,'stat '+cls);box.append(element('span',label,'stat-label'),element('span',icon,'stat-icon'),element('strong',String(number),'stat-number'),element('span',sub,'stat-sub'));return box;}));
}
function render(){
  $('source-sidebar').textContent=S.data.source_id;
  $('endpoint-label').textContent=S.data.netbox_url?.replace(/^https?:\/\//,'') || 'NetBox nicht konfiguriert';
  $('mode-badge').textContent=S.data.demo?'DEMO · SYNTHETISCH':'LOKAL';
  $('node-total').textContent=String(S.data.nodes.length);
  $('matched-at').textContent=S.data.matched_at?'Letzter Abgleich: '+date(S.data.matched_at):'NetBox noch nicht abgeglichen · Erfassung bleibt unverändert';
  stats();renderMapping();renderSoftware();setBusy(S.busy);
}
function visibleRows(){
  const query=$('search').value.toLocaleLowerCase(), filter=$('filter').value, eligible=new Set(S.data.bulk_eligible);
  const conflicts=new Set(mappingConflicts().flatMap(c=>c.node_ids));
  return S.data.nodes.filter(row=>{
    const text=[row.label,row.node_id,row.address,row.location,...row.candidates.flatMap(c=>[c.name,...(c.dns_names||[]),...(c.addresses||[]).map(ip=>ip.address)])].join(' ').toLocaleLowerCase();
    return (!query||text.includes(query)) && (filter==='all'||filter==='conflicts'&&conflicts.has(row.node_id)||filter==='open'&&!row.existing_binding||filter==='bound'&&row.existing_binding||filter==='clear'&&eligible.has(row.node_id)||filter==='ambiguous'&&!row.existing_binding&&row.candidates.length>1||filter==='missing'&&!row.existing_binding&&!row.candidates.length);
  });
}
function evidence(row,c){
  const group=element('div',null,'chips');
  if(row.existing_binding) group.append(chip('✓ Bestätigt','green'));
  else if(c){
    if(c.matched_name) group.append(chip('✓ Name','green'));
    if(c.matched_dns) group.append(chip('✓ DNS','blue'));
    if(c.matched_ip) group.append(chip('✓ IP','green'));
    if(!available(row,c)) group.append(chip('Belegt','red'));
    if(!S.data.bulk_eligible.includes(row.node_id)) group.append(chip('Prüfen','amber'));
  }else group.append(chip(row.candidates.length?'Auswahl nötig':'Offen',row.candidates.length?'amber':'neutral'));
  return group;
}
function renderMapping(){
  if(!S.data)return;
  const rows=visibleRows();
  const conflicts=mappingConflicts();
  const rendered=rows.map(row=>{
    const rowConflicts=conflicts.filter(c=>c.node_ids.includes(row.node_id));
    const tr=element('tr',null,rowConflicts.length?'mapping-conflict':S.selected.has(row.node_id)?'selected':'');
    const checkCell=element('td',null,'check-col'), checkbox=element('input');checkbox.type='checkbox';checkbox.setAttribute('aria-label',(row.label||'Node '+row.node_id)+' auswählen');
    const chosen=candidateFor(row);
    checkbox.checked=S.selected.has(row.node_id);checkbox.disabled=S.busy||(!checkbox.checked&&(!!row.existing_binding||!chosen||!available(row,chosen)||row.no_snapshot));
    checkbox.addEventListener('change',()=>{checkbox.checked?S.selected.add(row.node_id):S.selected.delete(row.node_id);renderMapping();setBusy(S.busy);});checkCell.append(checkbox);
    const nameCell=element('td'), name=element('button',row.label||'Node '+row.node_id,'node-name');name.addEventListener('click',()=>openDetail(row));nameCell.append(name,element('span',(row.address||'Keine SNMP-IP')+' · #'+row.node_id,'subline'));
    if(row.collection_status==='failed')nameCell.append(element('span','Letzte Erfassung fehlgeschlagen','candidate-subline warning'));
    const loc=element('td');loc.append(element('span',row.location||'—','location'));
    const candidateCell=element('td');
    if(row.existing_binding){
      const target=row.existing_binding, match=row.candidates.find(c=>c.type===target.object_type&&c.id===target.object_id);
      const link=element('a',match?.name||prettyType(target.object_type)+' #'+target.object_id);
      link.href=S.data.netbox_url.replace(/\/$/,'')+(target.object_type==='devices'?'/dcim/devices/':'/virtualization/virtual-machines/')+target.object_id+'/';link.target='_blank';link.rel='noreferrer';candidateCell.append(link,element('div',prettyType(target.object_type)+' · feste Objekt-ID '+target.object_id,'candidate-subline'));
    }else if(row.candidates.length){
      const select=element('select',null,'candidate-select');select.setAttribute('aria-label','NetBox-Ziel für '+(row.label||row.node_id));
      const placeholder=element('option',row.candidates.length+' Kandidat'+(row.candidates.length>1?'en':'')+' · bitte wählen');placeholder.value='';select.append(placeholder);
      for(const c of row.candidates){const option=element('option',c.name+' · '+prettyType(c.type)+' #'+c.id+(available(row,c)?'':' · bereits belegt'));option.value=targetKey(c);option.disabled=!available(row,c);select.append(option);}
      select.value=S.choices.get(row.node_id)||'';select.disabled=S.busy;
      select.addEventListener('change',()=>{S.choices.set(row.node_id,select.value);S.selected.delete(row.node_id);S.conflicts=S.conflicts.filter(c=>!c.node_ids.includes(row.node_id));S.preview=null;renderMapping();setBusy(S.busy);});
      candidateCell.append(select);
      if(chosen){
        const scope=[chosen.site||('Site '+(chosen.site_id??'—')),chosen.tenant|| (chosen.tenant_id?'Tenant '+chosen.tenant_id:null)].filter(Boolean).join(' · ');
        candidateCell.append(element('div',scope,'candidate-subline'));
        const ipHint=(chosen.addresses||[]).map(item=>item.address+(item.vrf_id?' [VRF '+item.vrf_id+']':'')).join(', ');
        if(ipHint)candidateCell.append(element('div',ipHint,'candidate-subline'));
        if(chosen.dns_names?.length)candidateCell.append(element('div','DNS: '+chosen.dns_names.join(', '),'candidate-subline'));
      }
      if(row.warning)candidateCell.append(element('div','Location-Scope nicht konfiguriert','candidate-subline warning'));
    }else{
      candidateCell.append(element('span',S.data.matched_at?'Kein passendes Ziel gefunden':'Abgleich ausstehend','status-text'));
      if(row.no_snapshot)candidateCell.append(element('div','Noch kein erfolgreicher Softwarestand','candidate-subline warning'));
    }
    for(const conflict of rowConflicts)candidateCell.append(element('div',conflict.message,'mapping-error'));
    const matchCell=element('td');matchCell.append(evidence(row,chosen));
    if(rowConflicts.length)matchCell.append(chip('Konflikt','red'));
    const actionCell=element('td',null,'right'), details=element('button','↗','details-button');details.setAttribute('aria-label','Software und Historie für '+(row.label||row.node_id));details.addEventListener('click',()=>openDetail(row));actionCell.append(details);
    tr.append(checkCell,nameCell,loc,candidateCell,matchCell,actionCell);return tr;
  });
  if(!rendered.length){const tr=element('tr',null,'empty-row'),td=element('td',S.data.nodes.length?'Keine Nodes für diesen Filter.':'Noch keine Erfassung eingelesen. Nutze „Erfassung einlesen“.');td.colSpan=6;tr.append(td);rendered.push(tr);}
  $('node-table').replaceChildren(...rendered);
  $('visible-count').textContent=rows.length+' von '+S.data.nodes.length+' Nodes';
  $('selected-count').textContent=(S.selected.size?S.selected.size+' Zuordnung'+(S.selected.size===1?'':'en')+' ausgewählt':'Keine Auswahl')+(conflicts.length?' · Konflikte zuerst auflösen (Filter „Konflikte“)':'');
}
function renderSoftware(){
  const rows=S.data.nodes.map(row=>{
    const tr=element('tr'),nameCell=element('td'),name=element('button',row.label||'Node '+row.node_id,'node-name');name.addEventListener('click',()=>openDetail(row));nameCell.append(name,element('span',row.address||'—','subline'));
    const status=element('td');status.append(chip(row.collection_status==='complete'?'Erfolgreich':row.collection_status==='failed'?'Fehlgeschlagen':'Nicht erfasst',row.collection_status==='complete'?'green':'amber'));
    const last=element('td'),button=element('button','Ansehen →','button');button.addEventListener('click',()=>openDetail(row));last.append(button);
    tr.append(nameCell,element('td',row.location||'—'),element('td',String(row.package_count)),element('td',date(row.observed_at)),status,last);return tr;
  });$('software-table').replaceChildren(...rows);
}
async function runJob(action,body={}){
  if(S.busy)return;
  setBusy(true);notify(action==='refresh'?'NetBox wird gelesen. Kandidaten werden abgeglichen …':'Aktion läuft …','busy');
  try{const started=await api('/api/'+action,body);await pollJob(started.job_id);}catch(error){notify(error.message,'error');setBusy(false);}
}
async function pollJob(identity){
  while(true){
    const job=await api('/api/jobs/'+identity);
    if(job.status==='failed'){
      if(job.action==='bind'&&job.conflicts){S.conflicts=job.conflicts;$('filter').value='conflicts';$('search').value='';renderMapping();}
      notify(job.error,'error');setBusy(false);return;
    }
    if(job.status==='done'){
      if(job.action==='refresh'||job.action==='import'){S.selected.clear();S.choices.clear();S.conflicts=[];S.preview=null;if($('filter').value==='conflicts')$('filter').value='all';}
      if(job.action==='bind'){S.selected.clear();S.conflicts=[];S.preview=null;if($('filter').value==='conflicts')$('filter').value='open';}
      if(job.action==='sync-preview'||job.action==='sync-apply'){
        S.preview=job.action==='sync-preview'?job.result.results:null;
        S.previewId=job.result.preview_id||null;
        renderExport(job.result.results,job.action==='sync-apply');
      }
      await loadState();setBusy(false);
      notify(job.result.message||'Aktion abgeschlossen.');return;
    }
    await new Promise(resolve=>setTimeout(resolve,900));
  }
}
function renderExport(results,applied){
  const root=$('export-results');root.className='table-scroll';
  const table=element('table'),head=element('thead'),hr=element('tr');['NODE','AKTION','BETROFFENE FELDER / HINWEIS'].forEach(text=>hr.append(element('th',text)));head.append(hr);table.append(head);
  const body=element('tbody');
  for(const row of results){
    const host=S.data.nodes.find(n=>n.node_id===row.node_id),tr=element('tr'),action=element('td');action.append(chip(({unchanged:'Unverändert',would_update:'Würde aktualisieren',confirmed:'Übertragen',failed:'Fehler'})[row.action]||row.action,row.action==='failed'?'red':row.action==='would_update'?'blue':'green'));
    tr.append(element('td',host?.label||'Node '+row.node_id),action,element('td',row.error||row.fields?.join(', ')||'Keine Änderungen'));body.append(tr);
  }
  if(!results.length){const tr=element('tr'),td=element('td','Noch keine bestätigten Zuordnungen.');td.colSpan=3;tr.append(td);body.append(tr);}
  table.append(body);root.replaceChildren(table);
  $('export-caption').textContent=(applied?'Ergebnis':'Vorschau')+' · '+results.length+' bestätigte Nodes';
}
function confirmation(title,description,button,action){
  $('confirm-title').textContent=title;$('confirm-description').textContent=description;$('accept-confirm').textContent=button;
  $('accept-confirm').onclick=()=>{$('confirm-dialog').close();action();};
  $('confirm-dialog').showModal();
}
async function openDetail(row){
  $('detail-node-id').textContent='OPENNMS NODE #'+row.node_id;$('detail-title').textContent=row.label||'Node '+row.node_id;$('detail-subtitle').textContent=(row.address||'—')+' · '+(row.location||'—');
  $('detail-content').replaceChildren(element('div','Softwarestand wird geladen …','empty'));$('detail-dialog').showModal();
  try{S.detail=await api('/api/node?id='+encodeURIComponent(row.node_id));S.detailTab='packages';renderDetail();}catch(error){$('detail-content').replaceChildren(element('div',error.message,'empty'));}
}
function renderDetail(){
  document.querySelectorAll('[data-detail]').forEach(b=>b.classList.toggle('active',b.dataset.detail===S.detailTab));
  const root=$('detail-content');root.replaceChildren();
  if(S.detailTab==='packages'){
    const search=element('input',null,'package-search');search.type='search';search.placeholder='Paket oder Version suchen …';search.setAttribute('aria-label','Software durchsuchen');root.append(search);
    const wrap=element('div',null,'table-scroll');root.append(wrap);
    const draw=()=>{const query=search.value.toLocaleLowerCase(),table=element('table',null,'package-table'),head=element('thead'),header=element('tr');['PAKET','VERSION','ARCHITEKTUR'].forEach(text=>header.append(element('th',text)));head.append(header);table.append(head);const body=element('tbody');
      for(const item of S.detail.snapshot.software.filter(p=>(p.name+' '+p.version+' '+p.raw).toLocaleLowerCase().includes(query))){const tr=element('tr');tr.append(element('td',item.name),element('td',item.version||'Unbekannt'),element('td',item.architecture||'—'));body.append(tr);}table.append(body);wrap.replaceChildren(table);};
    search.addEventListener('input',draw);draw();
  }else{
    for(const entry of [...S.detail.timeline].reverse()){
      const section=element('section',null,'timeline-entry');section.append(element('h3',date(entry.first_observed_at)),element('p',entry.observations+' Beobachtung(en) · zuletzt '+date(entry.last_observed_at)));
      for(const change of entry.changes){const line=element('div',null,'timeline-item'),detail=element('div'),label=({baseline:'Erster Stand',version_changed:'Version geändert',newly_observed:'Neu beobachtet',no_longer_observed:'Nicht mehr beobachtet'})[change.kind]||change.kind;detail.append(chip(label,change.kind==='version_changed'?'blue':'neutral'),element('div',(change.before.join(', ')||'—')+' → '+(change.after.join(', ')||'—'),'candidate-subline'));line.append(element('code',change.name),detail);section.append(line);}root.append(section);
    }
    if(!S.detail.timeline.length)root.append(element('p','Noch keine Versionshistorie vorhanden.'));
  }
}
function setPage(page){
  S.page=page;const labels={mapping:['Hosts sicher zuordnen','Das richtige Ziel für jeden Node. Namen, DNS und IP-Adressen im direkten Vergleich.','Zuordnungen'],software:['Software im Blick','Installierte Pakete und beobachtete Versionsänderungen.','Software & Historie'],export:['Aktuellen Stand übertragen','Nur bestätigte Verknüpfungen werden nach NetBox synchronisiert.','NetBox-Export']};
  const [title,description,crumb]=labels[page];$('page-title').replaceChildren(document.createTextNode(title),element('span','.','accent'));$('page-description').textContent=description;$('breadcrumb-current').textContent=crumb;
  for(const id of ['mapping','software','export'])$(id+'-page').classList.toggle('hidden',page!==id);
  document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
}
$('auth-form').addEventListener('submit',async event=>{event.preventDefault();token=$('token').value.trim();try{sessionStorage.setItem('inventory-token',token);}catch(_){}try{await loadState();}catch(error){alert(error.message);}});
$('search').addEventListener('input',renderMapping);$('filter').addEventListener('change',renderMapping);
$('refresh-button').addEventListener('click',()=>runJob('refresh'));$('import-button').addEventListener('click',()=>runJob('import'));
$('select-clear').addEventListener('click',()=>{let count=0;for(const row of visibleRows()){if(!row.existing_binding&&S.data.bulk_eligible.includes(row.node_id)){const c=row.candidates[0];S.choices.set(row.node_id,targetKey(c));S.selected.add(row.node_id);count++;}}renderMapping();setBusy(S.busy);notify(count?count+' eindeutige Treffer vorausgewählt. Prüfe die Auswahl und bestätige sie anschließend.':'Keine Treffer für die Sammelauswahl. Erforderlich: ein Kandidat, Name/DNS + IP, kein Konflikt und bei privaten IPs ein konfigurierter Location-Scope.','');});
$('save-button').addEventListener('click',()=>{const conflicts=mappingConflicts();if(conflicts.length){$('filter').value='conflicts';$('search').value='';renderMapping();notify(conflicts[0].message,'error');return;}const selections=S.data.nodes.filter(row=>S.selected.has(row.node_id)).map(row=>({node_id:row.node_id,target:{type:candidateFor(row).type,id:candidateFor(row).id}}));confirmation(selections.length+' Zuordnung(en) speichern?', 'Die ausgewählten Ziele werden erneut live geprüft und lokal dauerhaft verknüpft. NetBox bleibt unverändert.','Zuordnungen speichern',()=>runJob('bind',{selections}));});
$('preview-button').addEventListener('click',()=>runJob('sync-preview'));
$('apply-button').addEventListener('click',()=>confirmation('Änderungen nach NetBox schreiben?','Die bestätigten Objekte werden erneut geprüft. Nur die eigenen Software-Inventarfelder werden aktualisiert.','Nach NetBox schreiben',()=>runJob('sync-apply',{preview_id:S.previewId})));
$('cancel-confirm').addEventListener('click',()=>$('confirm-dialog').close());$('close-detail').addEventListener('click',()=>$('detail-dialog').close());
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>setPage(button.dataset.page)));
document.querySelectorAll('[data-detail]').forEach(button=>button.addEventListener('click',()=>{S.detailTab=button.dataset.detail;renderDetail();}));
(async()=>{if(!token){showAuth();return;}try{await loadState();if(S.data.active_job?.status==='running'){setBusy(true);await pollJob(S.data.active_job.id);}else if(S.data.startup_message)notify(S.data.startup_message,'error');else if(S.data.demo)notify('Demo mit synthetischen Daten. Zuordnungen und NetBox-Schreibzugriffe sind deaktiviert.');}catch(error){if(S.data)notify(error.message,'error');else{showAuth();$('auth-panel').append(element('p',error.message));}}})();
