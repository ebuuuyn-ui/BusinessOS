"""Natural stock-code order and Turkish alphabetical product-name order."""
import re
import unicodedata

_alphabet = {letter: index for index, letter in enumerate('abcçdefgğhıijklmnoöprsştuüvyz')}


def stock_text_sort_key(value):
    text = unicodedata.normalize('NFC', str(value or '').strip()).replace('İ', 'i').replace('I', 'ı').lower()
    # Compare digit runs numerically: 01.2 precedes 01.10; preserve Turkish letters.
    return tuple((1, int(part)) if part.isdigit() else
                 (0, tuple(_alphabet.get(char, 100 + ord(char)) for char in part))
                 for part in re.split(r'(\d+)', text) if part)
