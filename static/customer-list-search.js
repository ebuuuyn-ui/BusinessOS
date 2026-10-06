// Search the complete customer list without resetting the new-customer form.
(()=>{
  const form=document.getElementById('customer-list-search');
  if(!form) return;
  const input=form.elements.q;
  const count=document.getElementById('customer-result-count');
  let timer, pending, generation=0;
  async function search(){
    const version=++generation;
    if(pending) pending.abort();
    pending=new AbortController();
    const url=new URL(form.action,location.href);
    url.searchParams.set('q',input.value.trim());
    url.searchParams.set('page','1');
    count.textContent='Aranıyor…';
    try{
      const response=await fetch(url,{signal:pending.signal});
      if(!response.ok) throw new Error('search');
      const page=new DOMParser().parseFromString(await response.text(),'text/html');
      if(version!==generation) return;
      const ids=['customer-result-count','customer-list-results','customer-list-pagination'];
      if(ids.some(id=>!page.getElementById(id))) throw new Error('search');
      ids.forEach(id=>document.getElementById(id).innerHTML=page.getElementById(id).innerHTML);
      document.dispatchEvent(new Event('bos:customer-results'));
      const oldClear=form.querySelector('a');
      const newClear=page.querySelector('#customer-list-search a');
      if(oldClear) oldClear.remove();
      if(newClear) form.append(newClear.cloneNode(true));
      if(window.top!==window) url.searchParams.set('_workspace_tab','1');
      history.replaceState(history.state,'',url.pathname+url.search);
    }catch(error){
      if(error.name!=='AbortError'&&version===generation) count.textContent='Arama tamamlanamadı. Ara düğmesiyle tekrar deneyin.';
    }
  }
  input.addEventListener('input',()=>{
    clearTimeout(timer); ++generation; if(pending) pending.abort();
    timer=setTimeout(search,350);
  });
  form.addEventListener('submit',event=>{event.preventDefault();clearTimeout(timer);search();});
})();
