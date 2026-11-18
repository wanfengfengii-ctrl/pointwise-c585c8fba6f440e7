from dataclasses import dataclass
from fractions import Fraction


class LayoutValidationError(ValueError):
    pass


def _int(value, name, lower, upper=None):
    if type(value) is not int or value < lower or (upper is not None and value > upper):
        raise LayoutValidationError(f'invalid {name}')
    return value


def _rational(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise LayoutValidationError(f'{name} must be [numerator, denominator]')
    if any(type(part) is not int for part in value) or value[1] == 0:
        raise LayoutValidationError(f'invalid {name}')
    return Fraction(value[0], value[1])


@dataclass(frozen=True)
class FieldLayout:
    name: str
    offset: int
    width: int
    byteorder: str
    signed: bool
    scale: Fraction
    bias: Fraction
    unit: str

    def to_document(self):
        return {
            'name': self.name, 'offset': self.offset, 'width': self.width,
            'byteorder': self.byteorder, 'signed': self.signed,
            'scale': [self.scale.numerator, self.scale.denominator],
            'bias': [self.bias.numerator, self.bias.denominator], 'unit': self.unit,
        }


@dataclass(frozen=True)
class DidLayout:
    did: int
    length: int
    fields: tuple

    def to_document(self):
        return {'did': self.did, 'length': self.length,
                'fields': [field.to_document() for field in self.fields]}


def validate_layout(document):
    if isinstance(document, DidLayout):
        document = document.to_document()
    if not isinstance(document, dict) or set(document) != {'did', 'length', 'fields'}:
        raise LayoutValidationError('layout requires did, length and fields')
    did = _int(document['did'], 'DID', 0, 65535)
    length = _int(document['length'], 'DID length', 0, 4092)
    if not isinstance(document['fields'], (list, tuple)):
        raise LayoutValidationError('fields must be a sequence')
    fields, names, occupied = [], set(), set()
    for row in document['fields']:
        if isinstance(row, FieldLayout):
            row = row.to_document()
        if not isinstance(row, dict) or set(row) != {
            'name', 'offset', 'width', 'byteorder', 'signed', 'scale', 'bias', 'unit'
        }:
            raise LayoutValidationError('invalid field members')
        name = row['name']
        if not isinstance(name, str) or not name.strip() or name in names:
            raise LayoutValidationError('field names must be nonempty and unique')
        names.add(name)
        offset = _int(row['offset'], 'field offset', 0)
        width = _int(row['width'], 'field width', 1)
        if offset + width > length:
            raise LayoutValidationError('field exceeds DID data')
        locations = set(range(offset, offset + width))
        if occupied & locations:
            raise LayoutValidationError('overlapping fields')
        occupied.update(locations)
        if row['byteorder'] not in ('big', 'little') or type(row['signed']) is not bool:
            raise LayoutValidationError('invalid byteorder or signed flag')
        if not isinstance(row['unit'], str):
            raise LayoutValidationError('unit must be text')
        fields.append(FieldLayout(name, offset, width, row['byteorder'], row['signed'],
                                  _rational(row['scale'], 'scale'), _rational(row['bias'], 'bias'), row['unit']))
    return DidLayout(did, length, tuple(sorted(fields, key=lambda field: (field.offset, field.name))))
