from dataclasses import dataclass
from fractions import Fraction


def read_integer(data, byteorder, signed):
    if not isinstance(data, bytes) or not data or byteorder not in ('big', 'little') or type(signed) is not bool:
        raise ValueError('invalid integer field')
    return int.from_bytes(data, byteorder=byteorder, signed=signed)


def apply_calibration(raw_integer, scale, bias):
    if type(raw_integer) is not int or not isinstance(scale, Fraction) or not isinstance(bias, Fraction):
        raise ValueError('calibration requires an integer and exact fractions')
    return Fraction(raw_integer) * scale + bias


def fraction_to_record(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


@dataclass(frozen=True)
class ExactFieldValue:
    name: str
    raw_bytes: bytes
    raw_integer: int
    value: Fraction
    unit: str
    frame: int
    body_start: int
    body_end: int
    segments: tuple = None

    def to_document(self):
        if self.segments is None:
            source = {'frame': self.frame, 'body_range': [self.body_start, self.body_end],
                      'can_range': [self.body_start + 1, self.body_end + 1]}
        else:
            # A field spanning consecutive frames has no single honest CAN
            # interval, so it carries the clipped per-frame provenance instead.
            source = {'body_range': [self.body_start, self.body_end],
                      'segments': [dict(segment) for segment in self.segments]}
        return {
            'name': self.name, 'raw_bytes': list(self.raw_bytes), 'raw_integer': self.raw_integer,
            'value': fraction_to_record(self.value), 'unit': self.unit, 'source': source,
        }
