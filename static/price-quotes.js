(()=>{'use strict';const cfg=JSON.parse(document.getElementById('quote-config').textContent),form=document.getElementById('quote-form'),container=document.getElementById('quote-lines'),err=document.getElementById('quote-error');const money=x=>Number(x).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₺';const round=x=>Math.round((x+Number.EPSILON)*100)/100;const normalize=x=>String(x).toLocaleLowerCase('tr-TR').replaceAll('ı','i');let lines=[];
function image(el,row){const img=el.querySelector('img'),missing=el.querySelector('.image-missing');img.hidden=false;missing.hidden=true;img.onload=()=>{img.hidden=false;missing.hidden=true};img.onerror=()=>{img.hidden=true;missing.hidden=false};img.src=row.image?'data:image/jpeg;base64,'+row.image:'/fiyat-teklifleri/urun/'+row.product_id+'/gorsel?v='+Date.now();}
function recalc(){
 let net=0,tax=0,valid=true;
 for(const {el,row} of lines){
  el.querySelectorAll('[data-line]').forEach(x=>row[x.dataset.line]=x.value);
  let rowValid=true;
  for(const key of ['quantity','list_price',row.mode==='discount'?'discount':'price','vat']){
   const input=el.querySelector('[data-line='+key+']'),n=quoteNumber(input.value);
   const ok=Number.isFinite(n)&&n>=0&&n<=999999999&&(key!=='quantity'||(Number.isInteger(n)&&n>=1&&n<=100000))&&(key!=='discount'||n<=100);
   input.setCustomValidity(ok?'':'Geçerli bir sayı girin. Örnek: 12.290,50');
   if(!ok)rowValid=false;else row[key]=String(n);
  }
  const priceInput=el.querySelector('[data-line=price]'),discountInput=el.querySelector('[data-line=discount]');
  priceInput.disabled=row.mode==='discount';discountInput.disabled=row.mode==='price';
  (row.mode==='discount'?priceInput:discountInput).setCustomValidity('');
  if(!rowValid){valid=false;el.querySelector('.line-total').textContent='Fiyat, adet veya oran alanını kontrol edin.';continue;}
  const lp=Number(row.list_price),q=Number(row.quantity);
  if(row.mode==='discount'){row.price=round(lp*(1-Number(row.discount)/100)).toFixed(2);priceInput.value=row.price;}
  else{row.discount=lp?round((lp-Number(row.price))*100/lp).toFixed(2):'0';discountInput.value=row.discount;}
  const total=round(Number(row.price)*q),vat=round(total*Number(row.vat)/100);net+=total;tax+=vat;
  el.querySelector('.line-total').textContent='KDV Hariç '+money(total)+' · KDV '+money(vat)+' · Toplam '+money(total+vat);
 }
 const box=document.getElementById('quote-totals');
 if(!valid){box.textContent='Toplam hesaplanamadı. İşaretli sayı alanlarını düzeltin.';return false;}
 box.textContent='Ara Toplam '+money(net)+' · KDV '+money(tax);const strong=document.createElement('strong');strong.textContent='Genel Toplam '+money(net+tax);box.append(strong);return true;
}
function add(source={}){const row={quantity:1,list_price:'0',price:'0',discount:'0',mode:'discount',vat:'10',name:'',description:'',...source};const el=document.getElementById('quote-line-template').content.firstElementChild.cloneNode(true);container.append(el);lines.push({row,el});el.querySelectorAll('[data-line]').forEach(x=>x.value=row[x.dataset.line]??'');const picker=el.querySelector('.product-picker');const p=cfg.products.find(p=>p.id===Number(row.product_id));if(p){picker.value=p.id+' · '+p.code+' · '+p.name;image(el,row);}else{el.querySelector('img').hidden=true;}
picker.addEventListener('input',()=>{const v=picker.value;const found=cfg.products.find(p=>v===p.id+' · '+p.code+' · '+p.name);if(found){row.product_id=found.id;row.refresh_image=true;row.image=null;for(const [k,v]of Object.entries({name:found.name,description:found.description,list_price:found.price})){row[k]=v;el.querySelector('[data-line='+k+']').value=v;}picker.setCustomValidity('');image(el,row);recalc();}else{row.product_id=null;picker.setCustomValidity('Listeden bir stok kartı seçin.');}});
el.querySelector('.remove').onclick=()=>{lines=lines.filter(x=>x.el!==el);el.remove();recalc();};el.querySelectorAll('[data-line]').forEach(x=>x.addEventListener('input',recalc));el.querySelector('.upload').onchange=async event=>{if(!row.product_id){err.textContent='Önce stok kartı seçin.';return;}const file=event.target.files[0];if(!file)return;if(file.size>5*1024*1024){err.textContent='Görsel en fazla 5 MB olabilir.';return;}const body=new FormData();body.append('image',file);event.target.disabled=true;try{const r=await fetch('/fiyat-teklifleri/urun/'+row.product_id+'/gorsel',{method:'POST',body});if(!r.ok)throw Error('Görsel kaydedilemedi. Dosya türünü ve boyutunu kontrol edin.');row.image=null;row.refresh_image=true;image(el,row);err.textContent='';}catch(e){err.textContent=e.message;}finally{event.target.disabled=false;}};recalc();}
document.getElementById('quote-add').onclick=()=>add();(cfg.data.lines.length?cfg.data.lines:[{}]).forEach(add);document.getElementById('quote-customer-picker').oninput=e=>{const c=cfg.customers.find(x=>e.target.value===x.id+' · '+x.code+' · '+x.name);if(c)for(const [k,v]of Object.entries({customer_name:c.name,customer_code:c.code,address:c.address,recipient:c.recipient,phone:c.phone}))form.querySelector('[data-field='+k+']').value=v;};
function serialize(){recalc();const data={lines:lines.map(x=>{const {image,...rest}=x.row;return rest;}),conditions:{}};form.querySelectorAll('[data-field]').forEach(x=>data[x.dataset.field]=x.value);form.querySelectorAll('[data-condition]').forEach(x=>data.conditions[x.dataset.condition]=x.value);document.getElementById('quote-payload').value=JSON.stringify(data);return data;}
form.addEventListener('submit',e=>{if(!recalc()){e.preventDefault();form.reportValidity();return;}if(!lines.length){e.preventDefault();err.textContent='En az bir ürün ekleyin.';return;}serialize();document.getElementById('quote-save').disabled=true;});document.getElementById('quote-preview').onclick=async()=>{if(!recalc()||!form.reportValidity()||!lines.length){form.reportValidity();return;}serialize();const btn=document.getElementById('quote-preview');btn.disabled=true;try{const r=await fetch(cfg.preview,{method:'POST',body:new FormData(form)});if(!r.ok)throw Error('Önizleme oluşturulamadı. Ürün, fiyat ve müşteri alanlarını kontrol edin.');const u=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=u;a.download='Abika-Teklif-Onizleme.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(u),60000);}catch(e){err.textContent=e.message;}finally{btn.disabled=false;}};
})();
