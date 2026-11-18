from dataclasses import dataclass
from copy import deepcopy
from diagcapture.source.evidence import validate_evidence
from diagcapture.transport.single import decode_single_frame
from diagcapture.transport.isotp import LongResponseTransport
from diagcapture.operations.session import OperationSession
from diagcapture.did.interpret import interpret_did
from diagcapture.source.channels import resolve_channel


@dataclass(frozen=True)
class ReplayResult:
    document: dict

    def to_document(self):
        return deepcopy(self.document)


def replay_capture(evidence, catalog):
    evidence = validate_evidence(evidence.to_document())
    if catalog.catalog_id != evidence.catalog_id or catalog.digest != evidence.catalog_digest:
        raise ValueError('replay catalog differs from bound evidence')
    if evidence.profile == 'isotp':
        return _replay_isotp(evidence, catalog)
    return _replay_single(evidence, catalog)


def _replay_single(evidence, catalog):
    session = OperationSession(evidence.run_id, evidence.timing)
    messages, diagnostics = [], []
    for ref in evidence.frames:
        session.advance_to(ref.frame.t_ms)
        result = decode_single_frame(ref, evidence.channels)
        if result.diagnostic is not None:
            diagnostics.append(result.diagnostic.to_document())
        if result.message is not None:
            messages.append(result.message.to_document())
            session.accept(result.message)
    session.finish()
    snapshot = session.snapshot()
    for operation in snapshot['operations']:
        if operation['status'] == 'success':
            response = operation['responses'][-1]
            interpretation = interpret_did(catalog, operation['did'], bytes(response['data']),
                                           response['message']['frame'])
            operation['interpretation'] = interpretation.to_document()
    return ReplayResult({
        'messages': messages, 'protocol_diagnostics': diagnostics,
        'operations': snapshot['operations'],
        'association_diagnostics': snapshot['association_diagnostics'],
    })


def _replay_isotp(evidence, catalog):
    session = OperationSession(evidence.run_id, evidence.timing, profile='isotp')
    transport = LongResponseTransport(evidence.run_id, evidence.timing, session)
    messages, diagnostics = [], []
    for ref in evidence.frames:
        # Only deadlines strictly earlier than this frame settle before it.
        session.advance_to(ref.frame.t_ms)
        transport.advance_to(ref.frame.t_ms)
        resolved = resolve_channel(evidence.channels, ref.frame.can_id)
        if resolved is not None:
            ecu, direction = resolved
            data = ref.frame.data
            kind = (data[0] >> 4) if data else None
            if direction == 'response' and kind == 1:
                transport.response_ff(ref, ecu)
            elif direction == 'response' and kind == 2:
                transport.response_cf(ref, ecu)
            elif direction == 'response' and kind == 3:
                transport.response_fc(ref, ecu)
            elif direction == 'response' and kind in (None, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
                transport.response_unknown(ref, ecu)
            elif direction == 'request' and kind == 3:
                transport.request_flow_control(ref, ecu)
            else:
                if direction == 'response':
                    transport.response_single(ref, ecu)
                decoded = decode_single_frame(ref, evidence.channels)
                if decoded.diagnostic is not None:
                    diagnostics.append(decoded.diagnostic.to_document())
                if decoded.message is not None:
                    messages.append((ref.position, decoded.message.to_document()))
                    session.accept(decoded.message)
        diagnostics.extend(row.to_document() for row in transport.drain_diagnostics())
        for position, document in transport.drain_completed():
            messages.append((position, document))
    transport.finish()
    session.finish()
    snapshot = session.snapshot()
    messages.sort(key=lambda item: item[0])
    for operation in snapshot['operations']:
        if operation['status'] != 'success':
            continue
        response = operation['responses'][-1]
        message = response['message']
        segments = message.get('segments') if 'transfer_id' in message else None
        interpretation = interpret_did(catalog, operation['did'], bytes(response['data']),
                                       message['frame'], segments)
        operation['interpretation'] = interpretation.to_document()
    return ReplayResult({
        'messages': [document for _, document in messages],
        'protocol_diagnostics': diagnostics,
        'operations': snapshot['operations'],
        'association_diagnostics': snapshot['association_diagnostics'],
        'transfers': transport.snapshot()['transfers'],
    })
