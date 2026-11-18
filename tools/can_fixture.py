from dataclasses import dataclass


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('fixture integer outside range')
    return value


def _bytes(values):
    values = list(values)
    for value in values:
        _integer(value, 0, 255)
    return values


def _frame(t_ms, can_id, data):
    _integer(t_ms, 0, 2 ** 63 - 1)
    _integer(can_id, 0, 0x1FFFFFFF)
    if len(data) > 8:
        raise ValueError('classic CAN fixture exceeds eight bytes')
    return {'t_ms': t_ms, 'can_id': can_id, 'data': data}


def sf(t_ms, can_id, body, padding=()):
    body, padding = _bytes(body), _bytes(padding)
    _integer(len(body), 1, 7)
    return _frame(t_ms, can_id, [len(body)] + body + padding)


def ff(t_ms, can_id, body_length, first_six):
    _integer(body_length, 8, 4095)
    first_six = _bytes(first_six)
    if len(first_six) != 6:
        raise ValueError('first frame fixture requires six initial body bytes')
    return _frame(t_ms, can_id, [0x10 | (body_length >> 8), body_length & 255] + first_six)


def cf(t_ms, can_id, sequence, chunk, pad=0):
    _integer(sequence, 0, 15)
    _integer(pad, 0, 255)
    chunk = _bytes(chunk)
    _integer(len(chunk), 1, 7)
    return _frame(t_ms, can_id, [0x20 | sequence] + chunk + [pad] * (7 - len(chunk)))


def fc(t_ms, can_id, status, block_size, stmin=0):
    _integer(status, 0, 2)
    _integer(block_size, 0, 255)
    _integer(stmin, 0, 255)
    return _frame(t_ms, can_id, [0x30 | status, block_size, stmin])


@dataclass
class VirtualClock:
    now: int = 0

    def __post_init__(self):
        _integer(self.now, 0, 2 ** 63 - 1)

    def step(self, milliseconds):
        _integer(milliseconds, 0, 2 ** 63 - 1)
        self.now += milliseconds
        return self.now
