/* Accept Turkish grouped/decimal input and canonical decimal API values. */
function quoteNumber(value) {
  let text=String(value??'').trim();
  if(!text)return NaN;
  if(text.includes(',')){
    if(!/^-?(?:\d+|\d{1,3}(?:\.\d{3})+),\d{1,2}$/.test(text))return NaN;
    text=text.replaceAll('.','').replace(',','.');
  }else if(/^-?\d{1,3}(?:\.\d{3})+$/.test(text)){
    text=text.replaceAll('.','');
  }else if(!/^-?\d+(?:\.\d{1,2})?$/.test(text))return NaN;
  const valueNumber=Number(text);
  return Number.isFinite(valueNumber)?valueNumber:NaN;
}
if(typeof module!=='undefined')module.exports=quoteNumber;
