"""Verified sofa-family set images shared by quotation and order forms."""
from pathlib import Path

SOFA_SETS = {
    'O043': ('LİVİA', range(1, 6)), 'O044': ('NESSA', range(1, 6)),
    'O045': ('ARVO', range(1, 6)), 'O046': ('SARONA', range(1, 6)),
    'O047': ('ATLANTİS', range(1, 6)), 'O048': ('MOZZA', range(1, 6)),
    'O049': ('EVENA', range(1, 6)), 'O050': ('MARİA', range(1, 6)),
    'O051': ('GESTO', range(1, 6)), 'O052': ('MERİDYEN', range(1, 6)),
    'O053': ('SOFADO', range(1, 4)), 'O054': ('VERSA', range(1, 6)),
    'O055': ('GALA', range(1, 6)), 'O056': ('TARO', range(1, 6)),
    'O017': ('SADE METAL', (1, 3, 4, 5, 6)), 'O057': ('OME', range(1, 4)),
}

def default_image_path(root, product):
    code = product.code or ''
    if not code or '/' in code or '\\' in code:
        return None
    family = SOFA_SETS.get(code[:4])
    if family and len(code) == 6 and code[-2:].isdigit() and int(code[-2:]) in family[1]:
        # Both code and model name must agree; never assign another model's photo.
        if (product.name or '').upper().startswith(family[0] + ' '):
            path = Path(root) / 'static' / 'quote-sofa-sets' / (code[:4] + '.jpg')
            if path.is_file():
                return path
    path = Path(root) / 'static' / 'quote-products' / (code + '.jpg')
    return path if path.is_file() else None
