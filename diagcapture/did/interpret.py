from dataclasses import dataclass
from diagcapture.did.values import ExactFieldValue, read_integer, apply_calibration


@dataclass(frozen=True)
class InterpretationDiagnostic:
    code: str
    did: int
    detail: str
    frame: int

    def to_document(self):
        return {'code': self.code, 'did': self.did, 'detail': self.detail, 'frame': self.frame}


@dataclass(frozen=True)
class DidInterpretation:
    fields: tuple
    diagnostics: tuple

    def to_document(self):
        return {'fields': [field.to_document() for field in self.fields],
                'diagnostics': [diagnostic.to_document() for diagnostic in self.diagnostics]}


def interpret_did(catalog, did, data, frame):
    layout = catalog.find(did)
    if layout is None:
        return DidInterpretation((), (InterpretationDiagnostic('unknown_did', did, 'DID is absent from bound catalog', frame),))
    if len(data) != layout.length:
        detail = f'expected {layout.length} data bytes, received {len(data)}'
        return DidInterpretation((), (InterpretationDiagnostic('data_length', did, detail, frame),))
    values = []
    try:
        for field in layout.fields:
            raw_bytes = data[field.offset:field.offset + field.width]
            raw_integer = read_integer(raw_bytes, field.byteorder, field.signed)
            value = apply_calibration(raw_integer, field.scale, field.bias)
            values.append(ExactFieldValue(field.name, raw_bytes, raw_integer, value, field.unit,
                                          frame, 3 + field.offset, 3 + field.offset + field.width))
    except (ValueError, TypeError, OverflowError) as exc:
        return DidInterpretation((), (InterpretationDiagnostic('field_interpretation', did, str(exc), frame),))
    return DidInterpretation(tuple(values), ())
