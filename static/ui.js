// Shared disclosure behavior; native details/summary provides keyboard activation.
document.addEventListener('click',event=>{
  document.querySelectorAll('.action-menu[open]').forEach(menu=>{
    if(!menu.contains(event.target)||event.target.closest('a'))menu.open=false;
  });
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')document.querySelectorAll('.action-menu[open]').forEach(menu=>{
    const focused=menu.contains(document.activeElement);menu.open=false;
    if(focused)menu.querySelector('summary').focus();
  });
});

// Keep a partly completed customer form when it is collapsed and reopened.
window.addEventListener('click',event=>{
  const trigger=event.target.closest('[data-open-customer]');
  const form=document.querySelector('#new-customer');
  if(!trigger||!form||event.defaultPrevented||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
  event.preventDefault();form.open=true;
  form.querySelector('input[name="name"]')?.focus();
},true);
