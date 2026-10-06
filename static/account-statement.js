// Keep the ledger compact; native disclosures retain keyboard and touch support.
(() => {
  const disclosures = [...document.querySelectorAll('.statement-description')];
  const syncOrder = disclosure => {
    const row = document.getElementById(disclosure.dataset.orderDetail);
    if (row) row.hidden = !disclosure.open;
  };
  disclosures.forEach(disclosure => {
    disclosure.addEventListener('toggle', () => syncOrder(disclosure));
    syncOrder(disclosure);
  });
  // Printing includes all existing details, then restores the working view.
  let printState;
  window.addEventListener('beforeprint', () => {
    if (printState) return;
    printState = disclosures.map(disclosure => disclosure.open);
    disclosures.forEach(disclosure => {disclosure.open = true; syncOrder(disclosure);});
  });
  window.addEventListener('afterprint', () => {
    if (!printState) return;
    disclosures.forEach((disclosure, index) => {
      disclosure.open = printState[index]; syncOrder(disclosure);
    });
    printState = undefined;
  });
})();
