from diagcapture.transport.diagnostics import SingleFrameProtocolError

SINGLE_FRAME_MAX_BODY = 7


def classify_pci(data):
    if not data:
        raise SingleFrameProtocolError('empty_data', 'registered frame has no PCI byte')
    return data[0] >> 4


def declared_single_length(data):
    kind = classify_pci(data)
    if kind != 0:
        raise SingleFrameProtocolError('unsupported_pci', f'PCI type {kind} is outside V1')
    length = data[0] & 15
    if not 1 <= length <= SINGLE_FRAME_MAX_BODY or length > len(data) - 1:
        raise SingleFrameProtocolError('bad_single_length', 'declared body does not fit a classic single frame')
    return length
