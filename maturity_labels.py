"""Purchase labels for shared maturity reports; never transform record contents."""

def tracking_text(value, context):
    if not context.get('is_purchase', False):
        return value
    for old, new in [('Tahsil Edilen', 'Ödenen'), ('Tahsil edilenler', 'Ödenenler'),
                     ('Tahsilatlar', 'Ödemeler'), ('tahsilatlar', 'ödemeler'),
                     ('Tahsilat', 'Ödeme'), ('tahsilat', 'ödeme'),
                     ('teslim edilmiş satış', 'teslim alınmış satın alma'),
                     ('Teslim edilmiş satış', 'Teslim alınmış satın alma')]:
        value = value.replace(old, new)
    return value
