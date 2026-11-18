from dataclasses import dataclass
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from diagcapture.source.evidence import validate_evidence
from diagcapture.did.catalog import catalog_from_document, catalog_to_document
from diagcapture.transport.single import decode_single_frame
from diagcapture.operations.requests import parse_request
from diagcapture.operations.responses import classify_response
from diagcapture.transport.messages import SingleMessage


class BundleIntegrityError(ValueError):
    pass


def canonical_json(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def document_digest(document):
    return hashlib.sha256(canonical_json(document).encode('utf-8')).hexdigest()


def _require(condition, detail):
    if not condition:
        raise BundleIntegrityError(detail)


def _same(left, right):
    return canonical_json(left) == canonical_json(right)


@dataclass(frozen=True)
class FrozenBundle:
    document: dict

    def to_document(self):
        return deepcopy(self.document)


def bundle_fingerprint(bundle):
    return document_digest(bundle.document if isinstance(bundle, FrozenBundle) else bundle)


def encode_bundle(evidence, catalog, result):
    document = {
        'format_version': 1, 'report_id': 'report:' + evidence.run_id,
        'run_id': evidence.run_id, 'input_digest': evidence.input_digest,
        'evidence': evidence.to_document(), 'catalog': catalog_to_document(catalog),
        'result': result.to_document(),
    }
    document['manifest'] = {name + '_sha256': document_digest(document[name])
                            for name in ('evidence', 'catalog', 'result')}
    return validate_bundle(document)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleIntegrityError('duplicate JSON member')
        result[key] = value
    return result


def decode_bundle(document):
    if isinstance(document, (str, bytes, bytearray)):
        try:
            document = json.loads(document, object_pairs_hook=_unique_object,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as exc:
            raise BundleIntegrityError('invalid bundle JSON') from exc
    return validate_bundle(document)


def _as_message(document):
    return SingleMessage(document['ecu'], document['direction'], document['t_ms'],
                         document['frame'], bytes(document['body']))


def _validate_document(document):
    _require(isinstance(document, dict) and set(document) == {
        'format_version', 'report_id', 'run_id', 'input_digest', 'evidence', 'catalog', 'result', 'manifest'
    }, 'incomplete bundle')
    _require(type(document['format_version']) is int and document['format_version'] == 1, 'unsupported bundle version')
    evidence = validate_evidence(document['evidence'])
    catalog = catalog_from_document(document['catalog'])
    _require(_same(catalog_to_document(catalog), document['catalog']), 'noncanonical catalog snapshot')
    _require(catalog.catalog_id == evidence.catalog_id and catalog.digest == evidence.catalog_digest, 'catalog binding mismatch')
    _require(document['run_id'] == evidence.run_id and document['report_id'] == 'report:' + evidence.run_id,
             'report identity mismatch')
    _require(document['input_digest'] == evidence.input_digest, 'input identity mismatch')
    manifest = {name + '_sha256': document_digest(document[name]) for name in ('evidence', 'catalog', 'result')}
    _require(_same(manifest, document['manifest']), 'bundle member digest mismatch')
    result = document['result']
    _require(isinstance(result, dict) and set(result) == {
        'messages', 'protocol_diagnostics', 'operations', 'association_diagnostics'
    }, 'incomplete replay result')
    _require(all(isinstance(value, list) for value in result.values()), 'result members must be arrays')
    expected_messages, expected_diagnostics = [], []
    for ref in evidence.frames:
        decoded = decode_single_frame(ref, evidence.channels)
        if decoded.message is not None:
            expected_messages.append(decoded.message.to_document())
        if decoded.diagnostic is not None:
            expected_diagnostics.append(decoded.diagnostic.to_document())
    _require(_same(result['messages'], expected_messages), 'message sources disagree with raw frames')
    _require(_same(result['protocol_diagnostics'], expected_diagnostics), 'protocol evidence mismatch')
    messages = {message['frame']: message for message in expected_messages}
    used_requests, used_responses, operation_ids = set(), set(), set()
    last_request_frame = -1
    for operation in result['operations']:
        _require(isinstance(operation, dict) and set(operation) == {
            'id', 'ecu', 'did', 'request', 'deadline_ms', 'deadline_history', 'status',
            'completed_at_ms', 'responses', 'interpretation'
        }, 'invalid operation members')
        request_document = operation['request']
        request_frame = request_document['frame']
        _require(type(request_frame) is int and request_frame in messages and
                 _same(request_document, messages[request_frame]), 'request source missing')
        request = parse_request(_as_message(request_document))
        _require(request_frame > last_request_frame and request_frame not in used_requests, 'operation request order mismatch')
        last_request_frame = request_frame
        used_requests.add(request_frame)
        expected_id = f'{evidence.run_id}:request:{request_frame}'
        _require(operation['id'] == expected_id and expected_id not in operation_ids, 'operation identity mismatch')
        operation_ids.add(expected_id)
        _require(operation['ecu'] == request.ecu and type(operation['did']) is int and operation['did'] == request.did,
                 'operation request metadata mismatch')
        _require(operation['status'] in ('success', 'negative', 'timeout'), 'unsettled operation')
        _require(isinstance(operation['responses'], list), 'invalid response array')
        deadline = request_document['t_ms'] + evidence.timing.P2
        history = [{'at_ms': request_document['t_ms'], 'deadline_ms': deadline, 'frame': request_frame}]
        previous_frame, terminal = request_frame, None
        for response_document in operation['responses']:
            message_document = response_document['message']
            frame = message_document['frame']
            _require(type(frame) is int and frame in messages and _same(message_document, messages[frame]), 'response source missing')
            response = classify_response(_as_message(message_document))
            _require(_same(response_document, response.to_document()), 'response classification mismatch')
            _require(response.matches(request) and frame > previous_frame and frame not in used_responses,
                     'response ownership or order mismatch')
            _require(terminal is None and message_document['t_ms'] <= deadline, 'response is ineligible')
            previous_frame = frame
            used_responses.add(frame)
            if response.kind == 'pending':
                deadline = message_document['t_ms'] + evidence.timing.P2star
                history.append({'at_ms': message_document['t_ms'], 'deadline_ms': deadline, 'frame': frame})
            else:
                terminal = response
        _require(_same(operation['deadline_ms'], deadline) and _same(operation['deadline_history'], history), 'deadline history mismatch')
        if terminal is None:
            _require(operation['status'] == 'timeout' and _same(operation['completed_at_ms'], deadline), 'timeout conclusion mismatch')
        else:
            status = 'success' if terminal.kind == 'positive' else 'negative'
            _require(operation['status'] == status and _same(operation['completed_at_ms'], terminal.message.t_ms), 'terminal conclusion mismatch')
        interpretation = operation['interpretation']
        _require(isinstance(interpretation, dict) and set(interpretation) == {'fields', 'diagnostics'} and
                 isinstance(interpretation['fields'], list) and isinstance(interpretation['diagnostics'], list), 'invalid interpretation')
        if operation['status'] != 'success':
            _require(interpretation == {'fields': [], 'diagnostics': []}, 'fields on unsuccessful operation')
            continue
        _require(not (interpretation['fields'] and interpretation['diagnostics']), 'partial interpretation')
        names = set()
        for value in interpretation['fields']:
            _require(isinstance(value, dict) and set(value) == {'name', 'raw_bytes', 'raw_integer', 'value', 'unit', 'source'}, 'invalid field value')
            _require(isinstance(value['name'], str) and value['name'] and value['name'] not in names, 'duplicate field name')
            names.add(value['name'])
            _require(type(value['raw_integer']) is int and isinstance(value['unit'], str), 'invalid field scalar')
            exact = value['value']
            _require(isinstance(exact, dict) and set(exact) == {'numerator', 'denominator'} and
                     type(exact['numerator']) is int and type(exact['denominator']) is int and exact['denominator'] > 0, 'invalid exact value')
            fraction = Fraction(exact['numerator'], exact['denominator'])
            _require((fraction.numerator, fraction.denominator) == (exact['numerator'], exact['denominator']), 'unreduced exact value')
            source = value['source']
            _require(isinstance(source, dict) and set(source) == {'frame', 'body_range', 'can_range'}, 'invalid field source')
            span = source['body_range']
            _require(isinstance(span, list) and len(span) == 2 and all(type(part) is int for part in span), 'invalid field range')
            start, end = span
            body = terminal.message.body
            _require(3 <= start < end <= len(body) and _same(source['frame'], terminal.message.frame), 'field range outside response')
            _require(_same(source['can_range'], [start + 1, end + 1]) and _same(value['raw_bytes'], list(body[start:end])), 'field bytes disagree with source')
        for diagnostic in interpretation['diagnostics']:
            _require(isinstance(diagnostic, dict) and set(diagnostic) == {'code', 'did', 'detail', 'frame'}, 'invalid interpretation diagnostic')
            _require(diagnostic['code'] in ('unknown_did', 'data_length', 'field_interpretation') and
                     _same(diagnostic['did'], request.did) and _same(diagnostic['frame'], terminal.message.frame) and
                     isinstance(diagnostic['detail'], str), 'interpretation diagnostic source mismatch')
    for diagnostic in result['association_diagnostics']:
        _require(isinstance(diagnostic, dict) and set(diagnostic) == {
            'code', 'detail', 'ecu', 'frame', 't_ms', 'body', 'operation_id'
        }, 'invalid association diagnostic')
        frame = diagnostic['frame']
        _require(type(frame) is int and frame in messages, 'association source missing')
        message = messages[frame]
        _require(all(_same(diagnostic[key], message[key]) for key in ('ecu', 't_ms', 'body')), 'association evidence mismatch')
        _require(diagnostic['code'] in ('request_format', 'active_request', 'response_format', 'unowned_response', 'did_mismatch') and
                 isinstance(diagnostic['detail'], str), 'invalid association code')
        _require(diagnostic['operation_id'] is None or diagnostic['operation_id'] in operation_ids, 'association operation missing')


def validate_bundle(document):
    if isinstance(document, FrozenBundle):
        document = document.document
    try:
        _validate_document(document)
    except BundleIntegrityError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        raise BundleIntegrityError('invalid bundle structure or source') from exc
    return FrozenBundle(deepcopy(document))
