from dataclasses import dataclass
from copy import deepcopy
from diagcapture.source.evidence import validate_evidence
from diagcapture.transport.single import decode_single_frame
from diagcapture.operations.session import OperationSession
from diagcapture.did.interpret import interpret_did


@dataclass(frozen=True)
class ReplayResult:
    document: dict

    def to_document(self):
        return deepcopy(self.document)


def replay_capture(evidence, catalog):
    evidence = validate_evidence(evidence.to_document())
    if catalog.catalog_id != evidence.catalog_id or catalog.digest != evidence.catalog_digest:
        raise ValueError('replay catalog differs from bound evidence')
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
