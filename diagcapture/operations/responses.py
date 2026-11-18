from dataclasses import dataclass


class ResponseFormatError(ValueError):
    pass


@dataclass(frozen=True)
class DiagnosticResponse:
    kind: str
    did: object
    nrc: object
    data: bytes
    message: object

    def matches(self, request):
        return self.message.ecu == request.ecu and (self.did is None or self.did == request.did)

    def to_document(self):
        return {
            'kind': self.kind, 'did': self.did, 'nrc': self.nrc,
            'data': list(self.data), 'message': self.message.to_document(),
        }


def classify_response(message):
    body = message.body
    if message.direction != 'response':
        raise ResponseFormatError('response direction required')
    if len(body) >= 3 and body[0] == 0x62:
        return DiagnosticResponse('positive', int.from_bytes(body[1:3], 'big'), None, body[3:], message)
    if len(body) == 3 and body[0:2] == bytes([0x7F, 0x22]):
        return DiagnosticResponse('pending' if body[2] == 0x78 else 'negative', None,
                                  body[2], b'', message)
    raise ResponseFormatError('response must be 62 DID data or exactly 7F 22 NRC')
