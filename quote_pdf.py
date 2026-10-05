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

def money(value):return f'{Decimal(value):,.2f}'.replace(',','X').replace('.',',').replace('X','.')+' TL'
def build_pdf(data,number):
    font,bold=register_pdf_fonts();buffer=io.BytesIO();navy=colors.HexColor('#243447');grey=colors.HexColor('#c4c4c4')
    normal=ParagraphStyle('Quote',fontName=font,fontSize=9,leading=13)
    small=ParagraphStyle('QuoteSmall',parent=normal,fontSize=8,leading=11)
    heading=ParagraphStyle('QuoteHeading',parent=normal,fontName=bold,fontSize=12,leading=16,spaceAfter=12)
    right=ParagraphStyle('QuoteRight',parent=small,alignment=TA_RIGHT)
    centered=ParagraphStyle('QuoteCenter',parent=small,alignment=TA_CENTER)
    def p(s,style=normal):return Paragraph(escape(str(s or '')).replace('\n','<br/>'),style)
    doc=SimpleDocTemplate(buffer,pagesize=(595.3,841.9),leftMargin=32,rightMargin=32,topMargin=120,bottomMargin=48)
    def chrome(c,d):
        c.saveState();c.setFillColor(navy);c.rect(32,761,77,38,fill=1,stroke=0);c.setFillColor(colors.white);c.setFont(bold,20);c.drawCentredString(70.5,773,'ABİKA')
        c.setFillColor(colors.black);c.setFont(bold,18);c.drawString(121,785,'ABİKA MOBİLYA');c.setFont(font,12);c.drawString(121,765,'FİYAT TEKLİFİ')
        c.setFont(font,8);c.drawRightString(563,775,number);c.setLineWidth(.8);c.line(32,733,563,733)
        c.setFont(font,8);c.drawString(32,28,f'Sayfa {d.page}');c.drawRightString(563,28,'Teklif No: '+number);c.restoreState()
    customer=[('Müşteri',data['customer_name']),('Müşteri Kodu',data['customer_code']),('Adres',data['address']),('Yetkili',data['recipient'])]
    author=data['author'];details=[('Teklif Tarihi',date.fromisoformat(data['date']).strftime('%d.%m.%Y')),('Firma','ABİKA MOBİLYA'),('Hazırlayan',author['name']),('E-Posta',author['email'])]
    if data.get('phone') or author.get('phone'):customer.append(('Telefon',data.get('phone','')));details.append(('Telefon',author.get('phone','')))
    rows=[[p(a,small),p(b,small),p(c,small),p(d,small)] for (a,b),(c,d) in zip(customer,details)]
    meta=Table(rows,colWidths=[72,211,77,171],hAlign='LEFT');meta.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story=[meta,Spacer(1,12)]
    header=['Ürün Adı / Kodu','Ürün Görseli','Birim Fiyatı','İskonto','İskontolu Birim Fiyatı','Adet','Toplam'];table_rows=[[p(s,centered) for s in header]]
    for row in data['lines']:
        name=[p(row['name'],ParagraphStyle('Product',parent=small,fontName=bold)),Spacer(1,6),p(row['code'],small),p(row['description'],small)]
        picture=p('Görsel Yok',centered)
        if row.get('image'):
            picture=Image(io.BytesIO(base64.b64decode(row['image'])),width=70,height=85,kind='proportional')
        table_rows.append([name,picture,p(money(row['list_price']),right),p('%'+row['discount'],right),p(money(row['price']),right),p(str(row['quantity']),right),p(money(row['total']),right)])
    table=Table(table_rows,colWidths=[119,84,79,46,82,31,90],repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),grey),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,0),10),('BOTTOMPADDING',(0,0),(-1,0),10),('TOPPADDING',(0,1),(-1,-1),14),('BOTTOMPADDING',(0,1),(-1,-1),14),('LINEBELOW',(0,1),(-1,-1),.4,colors.HexColor('#dddddd')),('LINEBELOW',(0,-1),(-1,-1),.8,colors.black)]))
    story.append(table);story.append(Spacer(1,12));tot=data['totals']
    summary=Table([[p(k,small),p(money(tot[v]),right)] for k,v in [('Liste Toplamı','listed'),('Toplam İskonto','discount'),('Ara Toplam (KDV Hariç)','net'),('KDV Tutarı','tax'),('Genel Toplam','total')]],colWidths=[125,100],hAlign='RIGHT')
    summary.setStyle(TableStyle([('BOTTOMPADDING',(0,0),(-1,-1),8)]));story.append(KeepTogether([summary,Spacer(1,12),p('Birim fiyatlar ve satır toplamları KDV hariçtir. Teslimat ve diğer teklif koşulları sonraki sayfadadır.',small)]))
    story += [PageBreak(),p('TEKLİF KOŞULLARI',heading),Spacer(1,12)]
    for label,value in data['conditions'].items():
        story.append(KeepTogether([p(label+':',heading),p(value or '—'),Spacer(1,18),HRFlowable(width='100%',thickness=.4,color=colors.HexColor('#dddddd')),Spacer(1,22)]))
    doc.build(story,onFirstPage=chrome,onLaterPages=chrome);buffer.seek(0);return buffer
