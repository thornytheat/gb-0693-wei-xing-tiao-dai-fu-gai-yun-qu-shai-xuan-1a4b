const identity=JSON.parse(document.getElementById('recheck-identity').textContent);
const log=document.getElementById('recheck-log');
async function postRecheck(payload){
  log.textContent='$ running…';
  document.querySelectorAll('button').forEach(el=>el.disabled=true);
  try{
    const args={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)};
    if(payload instanceof FormData){delete args.headers;args.body=payload;}
    const r=await fetch('/api/runs/'+encodeURIComponent(identity.run_id)+'/recheck',args);
    const v=await r.json();
    if(!r.ok)throw new Error(v.message||('HTTP '+r.status));
    location.reload();
  }catch(e){log.textContent='$ error / '+e.message;}
  finally{document.querySelectorAll('button').forEach(el=>el.disabled=false);}
}
const submit=document.getElementById('submit-recheck');
if(submit)submit.addEventListener('click',()=>postRecheck({recheck_csv:document.getElementById('recheck-csv').value}));
const attach=document.getElementById('recheck-attach');
if(attach)attach.addEventListener('click',()=>{
  const file=document.getElementById('recheck-file').files[0];
  if(!file){log.textContent='选择复查记录文件后上传。';return;}
  const form=new FormData();form.append('file',file);
  postRecheck(form);
});
