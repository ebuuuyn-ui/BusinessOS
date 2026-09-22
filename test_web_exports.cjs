const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/web-exports.js', 'utf8');
function fixture({native=false, supported=true, response={}, shareError=null}={}) {
  const elements=[], downloads=[], shared=[], alerts=[]; let handler, fetches=0;
  class Element {
    constructor(tag){this.tag=tag;this.children=[];this.events={};this.dataset={};this.attrs={};elements.push(this);}
    append(...items){this.children.push(...items);}
    addEventListener(type,fn){this.events[type]=fn;}
    setAttribute(k,v){this.attrs[k]=v;}
    removeAttribute(k){delete this.attrs[k];}
    hasAttribute(k){return Object.hasOwn(this.attrs,k);}
    remove(){this.removed=true;}
    showModal(){this.open=true;}
    close(){this.open=false;this.events.close?.();}
    click(){if(this.tag==='a')downloads.push(this);return this.events.click?.();}
  }
  const document={body:new Element('body'),createElement:tag=>new Element(tag),addEventListener:(type,fn)=>handler=fn};
  const TestURL=class extends URL {};
  TestURL.createObjectURL=()=> 'blob:synthetic'; TestURL.revokeObjectURL=()=>{};
  const navigator={canShare:()=>supported,share:async data=>{shared.push(data);if(shareError)throw shareError;}};
  vm.runInNewContext(source,{document,navigator,window:native?{pywebview:{api:{save_export(){},share_export(){},whatsapp_export(){}}}}:{},location:{href:'https://example.test/siparisler/1',origin:'https://example.test'},URL:TestURL,File,AbortController,setTimeout:()=>1,clearTimeout:()=>{},alert:m=>alerts.push(m),fetch:async()=>{fetches++;return {ok:true,redirected:false,status:200,headers:{get:()=> 'application/pdf'},blob:async()=>new Blob(['%PDF-1.4 synthetic'],{type:'application/pdf'}),...response};}});
  async function click(kind){const link=new Element('a');link.attrs['data-native-'+kind]='';link.href='https://example.test/siparisler/1/pdf';link.dataset.exportName='SA-TEST.pdf';link.textContent='Original';let prevented=false;await handler({target:{closest:()=>link},defaultPrevented:false,preventDefault:()=>prevented=true,stopImmediatePropagation(){}});return {link,prevented};}
  return {click,elements,downloads,shared,alerts,fetches:()=>fetches,button:text=>elements.find(x=>x.tag==='button'&&x.textContent===text)};
}
test('download keeps current page and names PDF',async()=>{const f=fixture();const r=await f.click('download');assert.ok(r.prevented);assert.equal(f.downloads[0].download,'SA-TEST.pdf');assert.equal(r.link.dataset.webExportBusy,'false');assert.equal(r.link.textContent,'Original');});
test('share waits for a fresh user click and sends a file, not an internal URL',async()=>{const f=fixture();await f.click('share');assert.equal(f.shared.length,0);await f.button('PDF’yi Paylaş').click();assert.equal(f.shared[0].files[0].name,'SA-TEST.pdf');assert.equal(f.shared[0].files[0].type,'application/pdf');assert.equal(f.shared[0].url,undefined);});
test('WhatsApp prepares native file share',async()=>{const f=fixture();await f.click('whatsapp');await f.button('WhatsApp için Paylaş').click();assert.equal(f.shared.length,1);});
test('unsupported file sharing offers explicit download',async()=>{const f=fixture({supported:false});await f.click('whatsapp');assert.equal(f.shared.length,0);await f.button('PDF İndir').click();assert.equal(f.downloads.length,1);});
test('cancelling share permits retry without error',async()=>{const f=fixture({shareError:{name:'AbortError'}});await f.click('share');const b=f.button('PDF’yi Paylaş');await b.click();assert.equal(b.disabled,false);assert.equal(f.alerts.length,0);assert.ok(f.elements.find(x=>x.tag==='dialog').open);});
test('server errors and login HTML never download as PDF',async()=>{for(const response of [{ok:false,status:500},{redirected:true},{headers:{get:()=> 'text/html'}},{blob:async()=>new Blob([])}]){const f=fixture({response});await f.click('download');assert.equal(f.downloads.length,0);assert.equal(f.alerts.length,1);}});
test('desktop native actions remain untouched',async()=>{const f=fixture({native:true});for(const kind of ['share','whatsapp','download']){assert.equal((await f.click(kind)).prevented,false);}assert.equal(f.fetches(),0);});
