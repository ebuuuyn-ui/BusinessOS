"""Read-only account ledger workbook, including both transfer legs."""
from io import BytesIO
from decimal import Decimal
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


def export_account_xlsx(account, entries):
    book=Workbook(); sheet=book.active; sheet.title='Hesap Hareketleri'
    sheet.append([account['name']]);sheet.merge_cells('A1:H1')
    sheet.append(['Tüm hesap hareketleri · Tutarlar TL · Virmanlar dahildir'])
    sheet.merge_cells('A2:H2')
    sheet.append(['Tarih','İşlem / Şekil','Cari / Kaynak','Açıklama','Giriş','Çıkış','Bakiye','Hesap'])
    balance=Decimal(0); incoming=Decimal(0); outgoing=Decimal(0)
    for entry in entries:
        amount=entry['amount'];balance+=amount
        credit=max(amount,Decimal(0));debit=max(-amount,Decimal(0));incoming+=credit;outgoing+=debit
        sheet.append([entry['date'],entry['method'],entry['party'],entry['description'],credit,debit,balance,account['name']])
        for column in (2,3,4,8): sheet.cell(sheet.max_row,column).data_type='s'
        sheet.cell(sheet.max_row,1).number_format='dd.mm.yyyy'
        for column in (5,6,7): sheet.cell(sheet.max_row,column).number_format='#,##0.00;[Red]-#,##0.00'
    last=sheet.max_row
    sheet.append(['TOPLAM','','','',incoming,outgoing,balance])
    for cell in sheet[sheet.max_row]:cell.font=Font(bold=True)
    for column in (5,6,7):sheet.cell(sheet.max_row,column).number_format='#,##0.00;[Red]-#,##0.00'
    sheet['A1'].data_type='s';sheet['A1'].font=Font(size=16,bold=True,color='234466')
    for cell in sheet[3]:
        cell.fill=PatternFill('solid',fgColor='234466');cell.font=Font(bold=True,color='FFFFFF')
    for index,width in enumerate([14,20,38,65,18,18,18,26],1):sheet.column_dimensions[get_column_letter(index)].width=width
    for row in sheet.iter_rows(min_row=4,max_row=last):
        for cell in row:
            cell.alignment=Alignment(vertical='top',wrap_text=True)
            if cell.row%2==0:cell.fill=PatternFill('solid',fgColor='F0F4F8')
    sheet.freeze_panes='A4';sheet.auto_filter.ref=f'A3:H{last}'
    sheet.sheet_view.showGridLines=False
    sheet.print_title_rows='1:3';sheet.sheet_properties.pageSetUpPr.fitToPage=True
    sheet.page_setup.orientation='landscape';sheet.page_setup.paperSize=sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth=1;sheet.page_setup.fitToHeight=0
    output=BytesIO();book.save(output);output.seek(0);return output
