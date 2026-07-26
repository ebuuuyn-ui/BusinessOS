import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const source = "/Users/apple/Downloads/ürün listesi-22.07.2026.xlsx";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));
const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 14000,
  tableMaxRows: 15,
  tableMaxCols: 30,
  tableMaxCellChars: 180,
});
console.log(overview.ndjson);
const sheet = workbook.worksheets.getItemAt(0);
console.log(JSON.stringify({ sheetName: sheet.name, usedAddress: sheet.getUsedRange(true)?.address ?? null }));
