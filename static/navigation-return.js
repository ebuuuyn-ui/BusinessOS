/* Keep the source list's filters and viewport while visiting a record. */
(()=>{
 const prefix='bos-navigation-v1:',clean=raw=>{const u=new URL(raw,location.href);u.searchParams.delete('_workspace_tab');return u.pathname+u.search;},here=clean(location.href);
 const read=k=>{try{return JSON.parse(sessionStorage.getItem(prefix+k)||'null')}catch{return null}},write=(k,v)=>{try{sessionStorage.setItem(prefix+k,JSON.stringify(v))}catch{}};
 const origin=read('from:'+here);
 const save=()=>write('position:'+here,{x:scrollX,y:scrollY,containers:[...document.querySelectorAll('main,.table-wrap,.workspace-source')].map(e=>({x:e.scrollLeft,y:e.scrollTop}))});
 document.addEventListener('click',event=>{
  const a=event.target.closest('a[href]');if(!a||event.button!==0||event.ctrlKey||event.metaKey||a.target||a.download||a.dataset.nativeDownload!==undefined||a.dataset.nativeShare!==undefined)return;
  const u=new URL(a.href,location.href);if(u.origin!==location.origin||u.pathname.startsWith('/static/')||/indir|export|pdf|logout|cikis/.test(u.pathname))return;
  if(origin&&a.matches('[data-return-origin]'))a.href=origin;
  const target=clean(a.href);save();
  if(a.closest('nav,aside')){write('from:'+target,null);return;}
  if(target!==here&&target!==origin)write('from:'+target,here);
 },true);
 document.addEventListener('submit',event=>{
  save();const form=event.target;if(!origin||form.method.toLowerCase()!=='post')return;
  let field=form.querySelector('[name="return_to"]');if(!field){field=document.createElement('input');field.type='hidden';field.name='return_to';form.append(field);field.value=origin}
 },true);
 function ready(){
  // The desktop shell contains a duplicate source; only its iframe restores content.
  if(window.top===window&&document.body.classList.contains('workspace-web-shell'))return;
  if(origin){
   const bar=document.querySelector('.topbar');
   document.querySelectorAll('a').forEach(a=>{if(/^(Vazgeç|.*Dön)$/.test(a.textContent.trim())&&!a.closest('nav,aside')){a.href=origin;a.dataset.returnOrigin='1';if(a.textContent.trim()!=='Vazgeç')a.textContent='← Önceki Listeye Dön'}});
   if(bar&&!bar.querySelector('[data-return-origin]')){const a=document.createElement('a');a.href=origin;a.className='button no-print';a.dataset.returnOrigin='1';a.textContent='← Önceki Listeye Dön';bar.append(a)}
  }
  const position=read('position:'+here);if(!position)return;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{scrollTo(position.x,position.y);document.querySelectorAll('main,.table-wrap,.workspace-source').forEach((e,i)=>{const p=position.containers[i];if(p){e.scrollLeft=p.x;e.scrollTop=p.y}})}));
 }
 document.addEventListener('DOMContentLoaded',ready);window.addEventListener('pagehide',save);
})();
