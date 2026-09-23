/* New invoices only. Existing invoices retain their saved dates. */
(() => {
  const form = document.querySelector('#invoice-form');
  if (!form) return;
  const invoiceDate = form.querySelector('[name="invoice_date"]');
  const dueDate = form.querySelector('[name="due_date"]');
  invoiceDate.addEventListener('change', () => {
    if (!invoiceDate.value || !invoiceDate.validity.valid) return;
    const [year, month, day] = invoiceDate.value.split('-').map(Number);
    const nextMonth = month === 12 ? 1 : month + 1;
    const nextYear = year + (month === 12 ? 1 : 0);
    const lastDay = new Date(Date.UTC(nextYear, nextMonth, 0)).getUTCDate();
    dueDate.value = `${nextYear}-${String(nextMonth).padStart(2, '0')}-${String(Math.min(day, lastDay)).padStart(2, '0')}`;
  });
})();
