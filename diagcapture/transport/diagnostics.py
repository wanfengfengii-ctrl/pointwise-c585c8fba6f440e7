from dataclasses import dataclass


class SingleFrameProtocolError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ProtocolDiagnostic:
    code: str
    detail: str
    ecu: str
    direction: str
    frame: int
    t_ms: int

    def to_document(self):
        return {
            'code': self.code, 'detail': self.detail, 'ecu': self.ecu,
            'direction': self.direction, 'frame': self.frame, 't_ms': self.t_ms,
        }


def make_protocol_diagnostic(ref, ecu, direction, error):
    return ProtocolDiagnostic(error.code, error.detail, ecu, direction,
                              ref.position, ref.frame.t_ms)
