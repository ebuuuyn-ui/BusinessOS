(()=>{'use strict';
const cfg=JSON.parse(document.getElementById('document-language-config').textContent),field=document.getElementById('document-language'),eng=document.getElementById('document-english'),tr=document.getElementById('document-turkish');
const originals=new WeakMap(),attrs=new WeakMap();let english=field.value==='en';
function render(){
 const walk=document.createTreeWalker(document.getElementById('workspace-source')||document.querySelector('.content')||document.body,NodeFilter.SHOW_TEXT);
 let n;while(n=walk.nextNode()){
  if(n.parentElement.closest('script,style,#document-language-note,#document-english,#document-turkish'))continue;
  if(n.parentElement.closest('option')&&!n.parentElement.closest('#quote-currency,#quote-document-type,[data-line=mode]'))continue;
  if(!originals.has(n))originals.set(n,n.nodeValue);const original=originals.get(n),key=original.trim();
  if(cfg.labels[key])n.nodeValue=english?original.replace(key,cfg.labels[key]):original;
 }
 document.querySelectorAll('[placeholder]').forEach(el=>{if(!attrs.has(el))attrs.set(el,el.getAttribute('placeholder'));const key=attrs.get(el);el.setAttribute('placeholder',english?(cfg.labels[key]||key):key);});
 field.value=english?'en':'tr';document.dispatchEvent(new Event('document-language-change'));tr.hidden=!english;eng.hidden=english;
 document.getElementById('document-language-note').textContent=english?'English document selected. Headings and standard text are translated. Review product names, descriptions and custom terms in English before saving.':'Başlıkları ve sabit metinleri İngilizce hazırlayabilirsiniz. Ürün adlarını ve kendi koşul metinlerinizi kontrol edin.';
}
function enable(){const kind=document.getElementById('quote-document-type');if(kind)kind.value='proforma';english=true;render();const currency=document.getElementById('quote-currency');if(currency){currency.value='USD';currency.dispatchEvent(new Event('change',{bubbles:true}));}render();const rate=document.getElementById('quote-rate');if(document.getElementById('quote-document-type')?.value==='proforma')document.getElementById('document-language-note').textContent+=' PROFORMA INVOICE output shows final prices only; discounts remain internal.';if(rate&&!rate.value){rate.focus();document.getElementById('document-language-note').textContent+=' Enter the USD exchange rate to calculate dollar prices.';}}
eng.addEventListener('click',enable);document.getElementById('quote-document-type')?.addEventListener('change',e=>{if(e.target.value==='proforma')enable();});tr.addEventListener('click',()=>{english=false;render();document.getElementById('quote-rate')?.dispatchEvent(new Event('input',{bubbles:true}));});
document.addEventListener('DOMContentLoaded',()=>{render();document.getElementById('quote-rate')?.dispatchEvent(new Event('input',{bubbles:true}));if(cfg.english)enable();new MutationObserver(()=>{if(english)render();}).observe(document.getElementById('quote-lines')||document.getElementById('packing-rows'),{childList:true});});
})();
