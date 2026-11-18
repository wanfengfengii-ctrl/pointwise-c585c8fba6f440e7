from dataclasses import dataclass
from collections.abc import Mapping


class FrameValidationError(ValueError):
    pass


def _integer(value, name, lower, upper=None):
    if type(value) is not int or value < lower or (upper is not None and value > upper):
        raise FrameValidationError(f'{name}: invalid integer')
    return value


@dataclass(frozen=True)
class Frame:
    t_ms: int
    can_id: int
    data: bytes

    def to_document(self):
        return {'t_ms': self.t_ms, 'can_id': self.can_id, 'data': list(self.data)}


@dataclass(frozen=True)
class FrameRef:
    position: int
    frame: Frame

    def to_document(self):
        return {'position': self.position, **self.frame.to_document()}


def normalize_frames(frames):
    if frames is None or isinstance(frames, (str, bytes, bytearray, Mapping)):
        raise FrameValidationError('frames must be a finite sequence of frames')
    try:
        iterator = iter(frames)
    except TypeError as exc:
        raise FrameValidationError('frames is not iterable') from exc
    result = []
    previous = -1
    for position, item in enumerate(iterator):
        if isinstance(item, Frame):
            item = item.to_document()
        if not isinstance(item, Mapping) or set(item) != {'t_ms', 'can_id', 'data'}:
            raise FrameValidationError(f'frame {position}: expected t_ms, can_id, data')
        t_ms = _integer(item['t_ms'], 't_ms', 0)
        can_id = _integer(item['can_id'], 'can_id', 0, 0x1FFFFFFF)
        data = item['data']
        if not isinstance(data, (list, tuple, bytes, bytearray)) or len(data) > 8:
            raise FrameValidationError(f'frame {position}: data must contain at most eight bytes')
        for value in data:
            _integer(value, 'data byte', 0, 255)
        if t_ms < previous:
            raise FrameValidationError(f'frame {position}: time regression')
        previous = t_ms
        result.append(FrameRef(position, Frame(t_ms, can_id, bytes(data))))
    return tuple(result)
