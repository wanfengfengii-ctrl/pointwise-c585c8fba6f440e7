from dataclasses import dataclass


@dataclass(frozen=True)
class SingleMessage:
    ecu: str
    direction: str
    t_ms: int
    frame: int
    body: bytes

    def to_document(self):
        return {
            'ecu': self.ecu,
            'direction': self.direction,
            't_ms': self.t_ms,
            'frame': self.frame,
            'body': list(self.body),
            'body_range': [1, 1 + len(self.body)],
        }
