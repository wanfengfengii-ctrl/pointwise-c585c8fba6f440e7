from dataclasses import dataclass
from diagcapture.source.channels import resolve_channel
from diagcapture.transport.profile import declared_single_length
from diagcapture.transport.messages import SingleMessage
from diagcapture.transport.diagnostics import SingleFrameProtocolError, make_protocol_diagnostic


@dataclass(frozen=True)
class SingleFrameResult:
    message: object = None
    diagnostic: object = None


def decode_single_frame(ref, channels):
    resolved = resolve_channel(channels, ref.frame.can_id)
    if resolved is None:
        return SingleFrameResult()
    ecu, direction = resolved
    try:
        length = declared_single_length(ref.frame.data)
    except SingleFrameProtocolError as exc:
        return SingleFrameResult(diagnostic=make_protocol_diagnostic(ref, ecu, direction, exc))
    body = ref.frame.data[1:1 + length]
    return SingleFrameResult(message=SingleMessage(ecu, direction, ref.frame.t_ms, ref.position, body))
