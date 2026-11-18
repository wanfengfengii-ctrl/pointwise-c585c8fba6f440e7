from dataclasses import dataclass
from collections.abc import Mapping


class TimingConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class TimingConfig:
    P2: int
    P2star: int
    N_Bs: int
    N_Cr: int

    def to_document(self):
        return {'P2': self.P2, 'P2star': self.P2star, 'N_Bs': self.N_Bs, 'N_Cr': self.N_Cr}


def validate_timing(timing):
    if isinstance(timing, TimingConfig):
        timing = timing.to_document()
    names = {'P2', 'P2star', 'N_Bs', 'N_Cr'}
    if not isinstance(timing, Mapping) or set(timing) != names:
        raise TimingConfigurationError('timing requires P2, P2star, N_Bs and N_Cr')
    for name, value in timing.items():
        if type(value) is not int or value <= 0:
            raise TimingConfigurationError(f'{name} must be a positive integer')
    return TimingConfig(**timing)


def deadline_after(t_ms, delay_ms):
    if type(t_ms) is not int or t_ms < 0 or type(delay_ms) is not int or delay_ms <= 0:
        raise TimingConfigurationError('invalid deadline operands')
    return t_ms + delay_ms


def expires_before(deadline_ms, t_ms):
    return deadline_ms < t_ms
