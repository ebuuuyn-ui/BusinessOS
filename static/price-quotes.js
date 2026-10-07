(()=>{'use strict';const cfg=JSON.parse(document.getElementById('quote-config').textContent),form=document.getElementById('quote-form'),container=document.getElementById('quote-lines'),err=document.getElementById('quote-error');const money=x=>Number(x).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2})+(document.getElementById('quote-currency').value==='USD'?' USD':' ₺');const round=x=>Math.round((x+Number.EPSILON)*100)/100;const normalize=x=>String(x).toLocaleLowerCase('tr-TR').replaceAll('ı','i');let lines=[];
function image(el,row){const img=el.querySelector('img'),missing=el.querySelector('.image-missing');img.hidden=false;missing.hidden=true;img.onload=()=>{img.hidden=false;missing.hidden=true};img.onerror=()=>{img.hidden=true;missing.hidden=false};img.src=row.image?'data:image/jpeg;base64,'+row.image:'/fiyat-teklifleri/urun/'+row.product_id+'/gorsel?v='+Date.now();}
function recalc(){
 const currency=document.getElementById('quote-currency').value,rateInput=document.getElementById('quote-rate'),rate=currency==='USD'?quoteNumber(rateInput.value):1;
 rateInput.required=currency==='USD';rateInput.disabled=currency!=='USD';document.getElementById('quote-rate-label').hidden=currency!=='USD';
 const rateValid=Number.isFinite(rate)&&rate>0&&rate<=999999999;rateInput.setCustomValidity(rateValid?'':'Sıfırdan büyük bir USD kuru girin.');
 let net=0,tax=0,valid=rateValid;
 for(const {el,row} of lines){
  el.querySelectorAll('[data-line]').forEach(x=>row[x.dataset.line]=x.value);
  let rowValid=true;
  for(const key of ['quantity','list_price',row.mode==='discount'?'discount':row.mode==='usd'?'usd_price':'price','vat']){
   const input=el.querySelector('[data-line='+key+']'),n=quoteNumber(input.value);
   const ok=Number.isFinite(n)&&n>=0&&n<=999999999&&(key!=='quantity'||(Number.isInteger(n)&&n>=1&&n<=100000))&&(key!=='discount'||n<=100);
   input.setCustomValidity(ok?'':'Geçerli bir sayı girin. Örnek: 12.290,50');
   if(!ok)rowValid=false;else row[key]=String(n);
  }
  const priceInput=el.querySelector('[data-line=price]'),discountInput=el.querySelector('[data-line=discount]');
  const usdInput=el.querySelector('[data-line=usd_price]'),modeInput=el.querySelector('[data-line=mode]');
  modeInput.querySelector('option[value=usd]').disabled=currency!=='USD';
  modeInput.setCustomValidity(row.mode==='usd'&&currency!=='USD'?'USD fiyatı için teklif para birimini USD seçin.':'');
  priceInput.disabled=row.mode!=='price';discountInput.disabled=row.mode!=='discount';usdInput.disabled=row.mode!=='usd';
  for(const input of [priceInput,discountInput,usdInput])if(input.disabled)input.setCustomValidity('');
  if(!rateValid||(row.mode==='usd'&&currency!=='USD'))rowValid=false;
  if(!rowValid){valid=false;el.querySelector('.line-total').textContent='Fiyat, adet veya oran alanını kontrol edin.';continue;}
  const lp=Number(row.list_price),q=Number(row.quantity);
  if(row.mode==='discount'){row.price=round(lp*(1-Number(row.discount)/100)).toFixed(2);priceInput.value=row.price;}
  else if(row.mode==='usd'){row.price=round(Number(row.usd_price)*rate).toFixed(2);priceInput.value=row.price;row.discount=lp?round((lp-Number(row.price))*100/lp).toFixed(2):'0';discountInput.value=row.discount;}
  else{row.discount=lp?round((lp-Number(row.price))*100/lp).toFixed(2):'0';discountInput.value=row.discount;}
  if(row.mode!=='usd'){row.usd_price=currency==='USD'?round(Number(row.price)/rate).toFixed(2):'';usdInput.value=row.usd_price;}
  const effectivePrice=currency==='USD'?Number(row.usd_price):Number(row.price);
  const total=round(effectivePrice*q),vat=round(total*Number(row.vat)/100);net+=total;tax+=vat;
  el.querySelector('.line-total').textContent='KDV Hariç '+money(total)+' · KDV '+money(vat)+' · Toplam '+money(total+vat);
 }
 const box=document.getElementById('quote-totals');
 if(!valid){box.textContent='Toplam hesaplanamadı. İşaretli sayı alanlarını düzeltin.';return false;}
 box.textContent='Ara Toplam '+money(net)+' · KDV '+money(tax);const strong=document.createElement('strong');strong.textContent='Genel Toplam '+money(net+tax);box.append(strong);return true;
}
function add(source={}){if(cfg.data.currency==='USD'&&source.source_list_price!==undefined)source={...source,list_price:source.source_list_price,price:source.source_price};const row={quantity:1,list_price:'0',price:'0',discount:'0',mode:'discount',vat:'10',name:'',description:'',...source};const el=document.getElementById('quote-line-template').content.firstElementChild.cloneNode(true);container.append(el);lines.push({row,el});el.querySelectorAll('[data-line]').forEach(x=>x.value=row[x.dataset.line]??'');const picker=el.querySelector('.product-picker');const p=cfg.products.find(p=>p.id===Number(row.product_id));if(p){picker.value=p.id+' · '+p.code+' · '+p.name;image(el,row);}else{el.querySelector('img').hidden=true;}
picker.addEventListener('input',()=>{const v=picker.value;const found=cfg.products.find(p=>v===p.id+' · '+p.code+' · '+p.name);if(found){row.product_id=found.id;row.refresh_image=true;row.image=null;for(const [k,v]of Object.entries({name:found.name,description:found.description,list_price:found.price})){row[k]=v;el.querySelector('[data-line='+k+']').value=v;}picker.setCustomValidity('');image(el,row);recalc();}else{row.product_id=null;picker.setCustomValidity('Listeden bir stok kartı seçin.');}});
el.querySelector('.remove').onclick=()=>{lines=lines.filter(x=>x.el!==el);el.remove();recalc();};el.querySelectorAll('[data-line]').forEach(x=>x.addEventListener('input',recalc));el.querySelector('.upload').onchange=async event=>{if(!row.product_id){err.textContent='Önce stok kartı seçin.';return;}const file=event.target.files[0];if(!file)return;if(file.size>5*1024*1024){err.textContent='Görsel en fazla 5 MB olabilir.';return;}const body=new FormData();body.append('image',file);event.target.disabled=true;try{const r=await fetch('/fiyat-teklifleri/urun/'+row.product_id+'/gorsel',{method:'POST',body});if(!r.ok)throw Error('Görsel kaydedilemedi. Dosya türünü ve boyutunu kontrol edin.');row.image=null;row.refresh_image=true;image(el,row);err.textContent='';}catch(e){err.textContent=e.message;}finally{event.target.disabled=false;}};recalc();}
document.getElementById('quote-currency').addEventListener('change',()=>{if(document.getElementById('quote-currency').value==='TRY')for(const {row,el} of lines)if(row.mode==='usd'){row.mode='price';el.querySelector('[data-line=mode]').value='price';}recalc();});document.getElementById('quote-rate').addEventListener('input',recalc);
document.getElementById('quote-add').onclick=()=>add();(cfg.data.lines.length?cfg.data.lines:[{}]).forEach(add);const customerPicker=document.getElementById('quote-customer-picker'),customerResults=document.getElementById('quote-customer-results'),customerStatus=document.getElementById('quote-customer-status');
let customerMatches=[],activeCustomer=-1;
function closeCustomers(){customerResults.hidden=true;customerPicker.setAttribute('aria-expanded','false');customerPicker.removeAttribute('aria-activedescendant');activeCustomer=-1;}
function selectCustomer(c){
 customerPicker.value=c.name+' · '+c.code;
 for(const [k,v]of Object.entries({customer_name:c.name,customer_code:c.code,address:c.address,recipient:c.recipient,phone:c.phone}))form.querySelector('[data-field='+k+']').value=v;
 closeCustomers();customerStatus.textContent='Cari seçildi: '+c.name;
}
function searchCustomers(){
 const matches=matchQuoteCustomers(cfg.customers,customerPicker.value);customerMatches=matches.slice(0,40);activeCustomer=-1;customerResults.replaceChildren();customerPicker.removeAttribute('aria-activedescendant');
 for(const [i,c]of customerMatches.entries()){
  const option=document.createElement('button');option.type='button';option.role='option';option.id='quote-customer-option-'+i;option.setAttribute('aria-selected','false');option.textContent=c.name+' · '+c.code;option.onclick=()=>selectCustomer(c);customerResults.append(option);
 }
 customerResults.hidden=!customerMatches.length;customerPicker.setAttribute('aria-expanded',String(customerMatches.length>0));
 customerStatus.textContent=!customerPicker.value.trim()?'':!matches.length?'Eşleşen cari bulunamadı.':matches.length>40?matches.length+' cari bulundu. İlk 40 sonuç gösteriliyor; aramayı daraltabilirsiniz.':matches.length+' cari bulundu.';
}
customerPicker.addEventListener('input',searchCustomers);
customerPicker.addEventListener('keydown',e=>{
 if(e.key==='Escape'){closeCustomers();return;}
 if(customerResults.hidden)return;
 if(e.key==='ArrowDown'||e.key==='ArrowUp'){
  e.preventDefault();activeCustomer=(activeCustomer+(e.key==='ArrowDown'?1:-1)+customerMatches.length)%customerMatches.length;
  [...customerResults.children].forEach((el,i)=>el.setAttribute('aria-selected',String(i===activeCustomer)));
  const option=customerResults.children[activeCustomer];customerPicker.setAttribute('aria-activedescendant',option.id);option.scrollIntoView({block:'nearest'});
 }else if(e.key==='Enter'){e.preventDefault();if(activeCustomer<0)activeCustomer=0;selectCustomer(customerMatches[activeCustomer]);}
});
document.addEventListener('click',e=>{if(!e.target.closest('.quote-customer-search'))closeCustomers();});

function serialize(){recalc();const data={lines:lines.map(x=>{const {image,source_list_price,source_price,...rest}=x.row;return rest;}),conditions:{}};form.querySelectorAll('[data-field]').forEach(x=>data[x.dataset.field]=x.value);form.querySelectorAll('[data-condition]').forEach(x=>data.conditions[x.dataset.condition]=x.value);document.getElementById('quote-payload').value=JSON.stringify(data);return data;}
form.addEventListener('submit',e=>{if(!recalc()){e.preventDefault();form.reportValidity();return;}if(!lines.length){e.preventDefault();err.textContent='En az bir ürün ekleyin.';return;}serialize();document.getElementById('quote-save').disabled=true;});document.getElementById('quote-preview').onclick=async()=>{if(!recalc()||!form.reportValidity()||!lines.length){form.reportValidity();return;}serialize();const btn=document.getElementById('quote-preview');btn.disabled=true;try{const r=await fetch(cfg.preview,{method:'POST',body:new FormData(form)});if(!r.ok)throw Error('Önizleme oluşturulamadı. Ürün, fiyat ve müşteri alanlarını kontrol edin.');const u=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=u;a.download='Abika-Teklif-Onizleme.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(u),60000);}catch(e){err.textContent=e.message;}finally{btn.disabled=false;}};
})();
