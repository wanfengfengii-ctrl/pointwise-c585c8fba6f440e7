from dataclasses import dataclass


class RequestFormatError(ValueError):
    pass


@dataclass(frozen=True)
class DiagnosticRequest:
    ecu: str
    did: int
    message: object


def parse_request(message):
    body = message.body
    if message.direction != 'request' or len(body) != 3 or body[0] != 0x22:
        raise RequestFormatError('request must be 22 followed by one two-byte DID')
    return DiagnosticRequest(message.ecu, int.from_bytes(body[1:3], 'big'), message)
