"""Portable PDF fonts: system fonts when present, packaged ReportLab otherwise."""
from pathlib import Path
from threading import RLock
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_lock = RLock()

def register_pdf_fonts(regular_name='BusinessPDF', bold_name='BusinessPDFBold'):
    bundled = Path(reportlab.__file__).parent / 'fonts'
    candidates = [
        (Path('/System/Library/Fonts/Supplemental/Arial.ttf'), Path('/System/Library/Fonts/Supplemental/Arial Bold.ttf')),
        (Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')),
        (bundled / 'Vera.ttf', bundled / 'VeraBd.ttf'),
    ]
    with _lock:
        for regular,bold in candidates:
            if regular.is_file() and bold.is_file():
                if regular_name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(regular_name,str(regular)))
                if bold_name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(bold_name,str(bold)))
                pdfmetrics.registerFontFamily(regular_name,normal=regular_name,bold=bold_name,italic=regular_name,boldItalic=bold_name)
                return regular_name,bold_name
    raise RuntimeError('PDF yazı tipleri uygulama paketinde bulunamadı.')
