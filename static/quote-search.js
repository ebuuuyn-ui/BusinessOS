function normalizeQuoteSearch(value){
 return String(value??'').toLocaleLowerCase('tr-TR').replaceAll('ı','i').normalize('NFD').replace(/[\u0300-\u036f]/g,'').trim();
}
function matchQuoteCustomers(customers,query){
 const words=normalizeQuoteSearch(query).split(/\s+/).filter(Boolean);
 if(!words.length)return [];
 return customers.filter(c=>{const hay=normalizeQuoteSearch(c.name+' '+c.code);return words.every(word=>hay.includes(word));});
}
if(typeof module!=='undefined')module.exports={normalizeQuoteSearch,matchQuoteCustomers};
