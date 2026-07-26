import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const source = "/Users/apple/Downloads/cari kart listesi-21.07.2026.xlsx";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));

const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 12,
  tableMaxCols: 30,
  tableMaxCellChars: 160,
});
console.log(overview.ndjson);

const sheets = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
console.log(sheets.ndjson);

const firstSheet = workbook.worksheets.getItemAt(0);
const used = firstSheet.getUsedRange(true);
console.log(JSON.stringify({ sheetName: firstSheet.name, usedAddress: used?.address ?? null }));
