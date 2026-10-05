const assert=require('node:assert/strict');
const {matchQuoteCustomers:search}=require('./static/quote-search.js');
const customers=[{name:'ABİKA MOBİLYA İNŞ. PAZ.',code:'320.01.A001'},{name:'YANILMAZ MOBİLYA',code:'120.Y001'},{name:'Başka Firma',code:'B1'}];
for(const query of ['abika','ABİKA','ABIKA','bika','mobilya abika','320.01.a001'])assert.deepEqual(search(customers,query),[customers[0]],query);
for(const query of ['yanılmaz','yanilmaz','YANILMAZ'])assert.deepEqual(search(customers,query),[customers[1]],query);
assert.equal(search(customers,'mobilya').length,2);assert.equal(search(customers,'yok').length,0);assert.equal(search(customers,' ').length,0);console.log('Turkish customer search checks passed');
