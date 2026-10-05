const assert=require('node:assert/strict');
const parse=require('./static/quote-numbers.js');
for(const [input,expected] of [['12.290,50',12290.5],['12.290',12290],['12290.50',12290.5],['10,5',10.5],['0',0],['1.234.567,89',1234567.89],['4',4],['18.63',18.63]])assert.equal(parse(input),expected,input);
for(const input of ['', 'NaN','Infinity','12,3,4','1.2.3','abc','10x','1e3'])assert.ok(Number.isNaN(parse(input)),input);
assert.equal(Math.round(parse('12.290,00')*(1-parse('10,00')/100)*parse('4')*100)/100,44244);
console.log('Turkish quote number regression checks passed');
