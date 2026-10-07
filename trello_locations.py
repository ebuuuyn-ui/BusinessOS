"""Approximate Turkish province locations; no external geocoding or addresses."""
import unicodedata
from functools import lru_cache
from trello_province_data import PROVINCES


def normalize(value):
    value=str(value or '').strip().replace('ı','i').replace('İ','I').casefold()
    return ''.join(c for c in unicodedata.normalize('NFKD',value) if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def provinces():
    return {normalize(row['name']):row for row in PROVINCES}


def location(city):
    row=provinces().get(normalize(city))
    if not row:
        return {}
    return {'locationName':row['name'], 'address':row['name']+', Türkiye',
            'coordinates[latitude]':row['latitude'], 'coordinates[longitude]':row['longitude']}
