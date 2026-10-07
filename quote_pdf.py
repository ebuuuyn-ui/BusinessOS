"""ABIKA quotation PDF, flowing rows and a separate editable-terms page."""
import base64,io
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT,TA_CENTER
from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Spacer,Image,PageBreak,KeepTogether,HRFlowable
from pdf_fonts import register_pdf_fonts

def money(value,currency='TRY'):return f'{Decimal(value):,.2f}'.replace(',','X').replace('.',',').replace('X','.')+(' USD' if currency=='USD' else ' TL')
def build_pdf(data,number):
    currency=data.get('currency','TRY')
    from document_language import label as document_label,document_type
    proforma=document_type(data)=='proforma'
    english=data.get('language')=='en'
    def t(text):return document_label(text,'en' if english else 'tr')
    def display_money(value):return f'{Decimal(value):,.2f}'+(' USD' if currency=='USD' else ' TRY') if english else money(value,currency)
    font,bold=register_pdf_fonts();buffer=io.BytesIO();navy=colors.HexColor('#19344D');accent=colors.HexColor('#3578A5');pale=colors.HexColor('#EDF4FA')
    normal=ParagraphStyle('Quote',fontName=font,fontSize=9,leading=13,textColor=navy)
    small=ParagraphStyle('QuoteSmall',parent=normal,fontSize=8,leading=11)
    heading=ParagraphStyle('QuoteHeading',parent=normal,fontName=bold,fontSize=12,leading=16,spaceAfter=12)
    right=ParagraphStyle('QuoteRight',parent=small,alignment=TA_RIGHT)
    centered=ParagraphStyle('QuoteCenter',parent=small,alignment=TA_CENTER)
    header_style=ParagraphStyle('QuoteHeader',parent=centered,fontName=bold,textColor=colors.white)
    total_label=ParagraphStyle('QuoteTotalLabel',parent=small,fontName=bold,textColor=colors.white)
    total_value=ParagraphStyle('QuoteTotalValue',parent=right,fontName=bold,fontSize=11,textColor=colors.white)
    def p(s,style=normal):return Paragraph(escape(str(s or '')).replace('\n','<br/>'),style)
    doc=SimpleDocTemplate(buffer,pagesize=(595.3,841.9),leftMargin=32,rightMargin=32,topMargin=120,bottomMargin=48)
    def chrome(c,d):
        c.saveState();c.setFillColor(navy);c.rect(0,835,595.3,7,fill=1,stroke=0);c.setFillColor(colors.HexColor('#243447'));c.rect(32,761,77,38,fill=1,stroke=0);c.setFillColor(colors.white);c.setFont(bold,20);c.drawCentredString(70.5,773,'ABİKA')
        c.setFillColor(navy);c.setFont(bold,18);c.drawString(121,785,'ABİKA MOBİLYA');c.setFillColor(accent);c.setFont(font,12);c.drawString(121,765,'PROFORMA INVOICE' if proforma else t('FİYAT TEKLİFİ'))
        c.setFillColor(navy);c.setFont(font,8);c.drawRightString(563,775,number);c.setStrokeColor(accent);c.setLineWidth(1);c.line(32,733,563,733)
        c.setFont(font,8);c.drawString(32,28,f'{"Page" if english else "Sayfa"} {d.page}');c.drawRightString(563,28,('Proforma No: ' if proforma else 'Quotation No: ' if english else 'Teklif No: ')+number);c.setStrokeColor(accent);c.setLineWidth(1.5);c.line(32,18,563,18);c.restoreState()
    customer=[('Müşteri',data['customer_name']),('Müşteri Kodu',data['customer_code']),('Adres',data['address']),('Yetkili',data['recipient'])]
    author=data['author'];details=[('Teklif Tarihi',date.fromisoformat(data['date']).strftime('%d.%m.%Y')),('Firma','ABİKA MOBİLYA'),('Hazırlayan',author['name']),('E-Posta',author['email'])]
    if data.get('phone') or author.get('phone'):customer.append(('Telefon',data.get('phone','')));details.append(('Telefon',author.get('phone','')))
    rows=[[p(t(a),small),p(b,small),p(t(c),small),p(d,small)] for (a,b),(c,d) in zip(customer,details)]
    meta=Table(rows,colWidths=[72,211,77,171],hAlign='LEFT');meta.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),pale),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story=[meta,Spacer(1,10)]
    if proforma:story.extend([p('Currency: '+currency,small),Spacer(1,10)])
    elif currency=='USD':story.extend([p(('Currency: USD · Exchange Rate: 1 USD = ' if english else 'Para Birimi: USD · Kullanılan Kur: 1 USD = ')+data['exchange_rate']+(' TRY' if english else ' TL'),small),Spacer(1,10)])
    else:story.append(Spacer(1,10))
    header=['Ürün Adı / Kodu','Ürün Görseli','Birim Fiyatı','Adet','Toplam'] if proforma else ['Ürün Adı / Kodu','Ürün Görseli','Birim Fiyatı','İskonto','İskontolu Birim Fiyatı','Adet','Toplam'];table_rows=[[p('Qty' if english and s=='Adet' else t(s),header_style) for s in header]]
    for row in data['lines']:
        name=[p(row['name'],ParagraphStyle('Product',parent=small,fontName=bold)),Spacer(1,6),p(row['code'],small),p(row['description'],small)]
        picture=p(t('Görsel Yok'),centered)
        if row.get('image'):
            picture=Image(io.BytesIO(base64.b64decode(row['image'])),width=70,height=85,kind='proportional')
        prices=[p(display_money(row['price']),right)] if proforma else [p(display_money(row['list_price']),right),p(row['discount']+'%' if english else '%'+row['discount'],right),p(display_money(row['price']),right)]
        table_rows.append([name,picture,*prices,p(str(row['quantity']),right),p(display_money(row['total']),right)])
    table=Table(table_rows,colWidths=([180,100,95,45,111] if proforma else [110,84,79,46,82,40,90] if english else [119,84,79,46,82,31,90]),repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#F8FAFB'),colors.white]),('LINEBEFORE',(0,1),(0,-1),1.5,accent),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,0),10),('BOTTOMPADDING',(0,0),(-1,0),10),('TOPPADDING',(0,1),(-1,-1),14),('BOTTOMPADDING',(0,1),(-1,-1),14),('LINEBELOW',(0,1),(-1,-1),.4,colors.HexColor('#dddddd')),('LINEBELOW',(0,-1),(-1,-1),.8,accent)]))
    story.append(table);story.append(Spacer(1,12));tot=data['totals']
    summary=Table([[p(t(k),total_label if v=='total' else small),p(display_money(tot[v]),total_value if v=='total' else right)] for k,v in ([('Ara Toplam (KDV Hariç)','net'),('KDV Tutarı','tax'),('Genel Toplam','total')] if proforma else [('Liste Toplamı','listed'),('Toplam İskonto','discount'),('Ara Toplam (KDV Hariç)','net'),('KDV Tutarı','tax'),('Genel Toplam','total')])],colWidths=[143,110],hAlign='RIGHT')
    summary.setStyle(TableStyle([('BOTTOMPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,-1),(-1,-1),12),('BOTTOMPADDING',(0,-1),(-1,-1),12),('BACKGROUND',(0,-1),(-1,-1),navy),('LINEBEFORE',(0,-1),(0,-1),3,accent)]));story.append(KeepTogether([summary,Spacer(1,12),p(t('Birim fiyatlar ve satır toplamları KDV hariçtir. Teslimat ve diğer teklif koşulları sonraki sayfadadır.'),small)]))
    story += [PageBreak(),p('TERMS AND CONDITIONS' if proforma and english else t('TEKLİF KOŞULLARI'),heading),Spacer(1,12)]
    for label,value in data['conditions'].items():
        story.append(KeepTogether([p(t(label)+':',heading),p(value or '—'),Spacer(1,18),HRFlowable(width='100%',thickness=.4,color=accent),Spacer(1,22)]))
    doc.build(story,onFirstPage=chrome,onLaterPages=chrome);buffer.seek(0);return buffer
