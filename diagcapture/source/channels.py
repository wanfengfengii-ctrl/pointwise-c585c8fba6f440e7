from dataclasses import dataclass
from collections.abc import Mapping


class ChannelConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Channel:
    ecu: str
    request_id: int
    response_id: int

    def to_document(self):
        return {'ecu': self.ecu, 'request_id': self.request_id, 'response_id': self.response_id}


@dataclass(frozen=True)
class ChannelTable:
    channels: tuple

    def to_document(self):
        return [channel.to_document() for channel in self.channels]

    def resolve(self, can_id):
        for channel in self.channels:
            if can_id == channel.request_id:
                return channel.ecu, 'request'
            if can_id == channel.response_id:
                return channel.ecu, 'response'
        return None


def validate_channels(channels):
    if isinstance(channels, ChannelTable):
        channels = channels.to_document()
    if not isinstance(channels, (list, tuple)):
        raise ChannelConfigurationError('channels must be a sequence')
    names, identifiers, result = set(), set(), []
    for row in channels:
        if isinstance(row, Channel):
            row = row.to_document()
        if not isinstance(row, Mapping) or set(row) != {'ecu', 'request_id', 'response_id'}:
            raise ChannelConfigurationError('invalid channel members')
        ecu = row['ecu']
        if not isinstance(ecu, str) or not ecu.strip() or ecu in names:
            raise ChannelConfigurationError('ECU names must be nonempty and unique')
        names.add(ecu)
        for key in ('request_id', 'response_id'):
            value = row[key]
            if type(value) is not int or not 0 <= value <= 0x1FFFFFFF:
                raise ChannelConfigurationError(f'invalid {key}')
            if value in identifiers:
                raise ChannelConfigurationError('overlapping CAN identifiers')
            identifiers.add(value)
        result.append(Channel(ecu, row['request_id'], row['response_id']))
    return ChannelTable(tuple(sorted(result, key=lambda channel: channel.ecu)))


def resolve_channel(table, can_id):
    return table.resolve(can_id)
