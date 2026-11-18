from dataclasses import dataclass, field
from copy import deepcopy
from diagcapture.operations.requests import parse_request, RequestFormatError
from diagcapture.operations.responses import classify_response, ResponseFormatError
from diagcapture.operations.timing import deadline_after, expires_before, validate_timing


@dataclass
class DiagnosticOperation:
    operation_id: str
    request: object
    deadline_ms: int
    status: str = 'waiting'
    completed_at_ms: object = None
    responses: list = field(default_factory=list)
    deadline_history: list = field(default_factory=list)

    def to_document(self):
        return {
            'id': self.operation_id, 'ecu': self.request.ecu, 'did': self.request.did,
            'request': self.request.message.to_document(), 'deadline_ms': self.deadline_ms,
            'deadline_history': deepcopy(self.deadline_history), 'status': self.status,
            'completed_at_ms': self.completed_at_ms,
            'responses': [response.to_document() for response in self.responses],
            'interpretation': {'fields': [], 'diagnostics': []},
        }


@dataclass(frozen=True)
class AssociationDiagnostic:
    code: str
    detail: str
    ecu: str
    frame: int
    t_ms: int
    body: bytes
    operation_id: object = None

    def to_document(self):
        return {
            'code': self.code, 'detail': self.detail, 'ecu': self.ecu, 'frame': self.frame,
            't_ms': self.t_ms, 'body': list(self.body), 'operation_id': self.operation_id,
        }


class OperationSession:
    def __init__(self, run_id, timing):
        self.run_id = run_id
        self.timing = validate_timing(timing)
        self.active = {}
        self.operations = []
        self.diagnostics = []
        self.last_time = -1
        self.closed = False

    def _timeout(self, operation):
        operation.status = 'timeout'
        operation.completed_at_ms = operation.deadline_ms
        del self.active[operation.request.ecu]

    def advance_to(self, t_ms):
        if self.closed or type(t_ms) is not int or t_ms < 0 or t_ms < self.last_time:
            raise ValueError('session time must advance monotonically before finish')
        self.last_time = t_ms
        expired = [op for op in self.active.values() if expires_before(op.deadline_ms, t_ms)]
        for operation in sorted(expired, key=lambda op: (op.deadline_ms, op.request.message.frame)):
            self._timeout(operation)

    def _diagnose(self, message, code, detail, operation=None):
        self.diagnostics.append(AssociationDiagnostic(
            code, detail, message.ecu, message.frame, message.t_ms, message.body,
            operation.operation_id if operation else None))

    def accept(self, message):
        self.advance_to(message.t_ms)
        current = self.active.get(message.ecu)
        if message.direction == 'request':
            try:
                request = parse_request(message)
            except RequestFormatError as exc:
                self._diagnose(message, 'request_format', str(exc), current)
                return
            if current is not None:
                self._diagnose(message, 'active_request', 'ECU already has an unfinished operation', current)
                return
            deadline = deadline_after(message.t_ms, self.timing.P2)
            operation = DiagnosticOperation(f'{self.run_id}:request:{message.frame}', request, deadline)
            operation.deadline_history.append({'at_ms': message.t_ms, 'deadline_ms': deadline, 'frame': message.frame})
            self.operations.append(operation)
            self.active[message.ecu] = operation
            return
        try:
            response = classify_response(message)
        except ResponseFormatError as exc:
            self._diagnose(message, 'response_format', str(exc), current)
            return
        if current is None:
            self._diagnose(message, 'unowned_response', 'no unfinished operation for this ECU')
            return
        if not response.matches(current.request):
            self._diagnose(message, 'did_mismatch', 'response DID differs from active request', current)
            return
        current.responses.append(response)
        if response.kind == 'pending':
            current.status = 'waiting_extended'
            current.deadline_ms = deadline_after(message.t_ms, self.timing.P2star)
            current.deadline_history.append({
                'at_ms': message.t_ms, 'deadline_ms': current.deadline_ms, 'frame': message.frame})
        else:
            current.status = 'success' if response.kind == 'positive' else 'negative'
            current.completed_at_ms = message.t_ms
            del self.active[message.ecu]

    def finish(self):
        if self.closed:
            return
        remaining = sorted(self.active.values(), key=lambda op: (op.deadline_ms, op.request.message.frame))
        for operation in remaining:
            self._timeout(operation)
        self.closed = True

    def snapshot(self):
        return {
            'operations': [operation.to_document() for operation in self.operations],
            'association_diagnostics': [diagnostic.to_document() for diagnostic in self.diagnostics],
        }
