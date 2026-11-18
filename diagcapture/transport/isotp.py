"""Classic ISO-TP long-response assembly for the finite-recording profile.

The tracker owns at most one in-flight response transfer per ECU.  It never
reads a wall clock: every state change is driven by a raw frame and every
deadline is settled by the replay loop before the next frame, or at input end.
Evidence for a frame that ends a transfer stays on that transfer; a bare
protocol diagnostic is only emitted when no transfer is affected, so a stray
frame can never create or revive a transfer.
"""
from dataclasses import dataclass, field
from copy import deepcopy

from diagcapture.transport.diagnostics import ProtocolDiagnostic

TRANSFER_MAX_LENGTH = 4095
TRANSFER_MIN_LENGTH = 8


@dataclass(frozen=True)
class LongMessage:
    ecu: str
    direction: str
    frame: int
    t_ms: int
    body: bytes
    transfer_id: str
    segments: tuple

    def to_document(self):
        return {
            'ecu': self.ecu, 'direction': self.direction, 'body': list(self.body),
            'frame': self.frame, 't_ms': self.t_ms, 'transfer_id': self.transfer_id,
            'segments': deepcopy(list(self.segments)),
        }


@dataclass
class Transfer:
    transfer_id: str
    ecu: str
    start_frame: int
    start_ms: int
    declared_length: int
    frames: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)
    state: str = 'waiting_fc'
    deadline_ms: int = 0
    next_sequence: int = 1
    block_remaining: object = None
    operation: object = None
    operation_id: object = None
    binding_reason: object = None
    end_frame: object = None
    end_ms: object = None
    status: object = None
    reason: object = None

    def to_document(self):
        complete = self.status == 'complete'
        return {
            'id': self.transfer_id, 'ecu': self.ecu,
            'start_frame': self.start_frame, 'start_ms': self.start_ms,
            'declared_length': self.declared_length,
            'end_frame': self.end_frame, 'end_ms': self.end_ms,
            'status': self.status, 'reason': self.reason,
            'operation_id': self.operation_id, 'binding_reason': self.binding_reason,
            'frames': deepcopy(self.frames),
            'body': list(self.body) if complete else None,
            'segments': deepcopy(self.segments),
        }


class LongResponseTransport:
    def __init__(self, run_id, timing, session):
        self.run_id = run_id
        self.timing = timing
        self.session = session
        self.active = {}
        self.transfers = []
        self.completed = []
        self.diagnostics = []
        self.last_time = -1
        self.closed = False

    # -- time -----------------------------------------------------------
    def advance_to(self, t_ms):
        if self.closed or type(t_ms) is not int or t_ms < 0 or t_ms < self.last_time:
            raise ValueError('transport time must advance monotonically before finish')
        self.last_time = t_ms
        due = [transfer for transfer in self.active.values() if transfer.deadline_ms < t_ms]
        for transfer in sorted(due, key=lambda item: (item.deadline_ms, item.start_frame)):
            self._expire(transfer)

    def finish(self):
        if self.closed:
            return
        for transfer in sorted(self.active.values(),
                               key=lambda item: (item.deadline_ms, item.start_frame)):
            self._expire(transfer)
        self.closed = True

    def _expire(self, transfer):
        reason = 'N_Bs_timeout' if transfer.state == 'waiting_fc' else 'N_Cr_timeout'
        self._fail(transfer, reason, None, transfer.deadline_ms)

    # -- conclusions ----------------------------------------------------
    def _fail(self, transfer, reason, end_frame, end_ms, causing=None):
        if causing is not None and causing not in transfer.frames:
            transfer.frames.append(causing)
        transfer.status = 'failed'
        transfer.reason = reason
        transfer.end_frame = end_frame
        transfer.end_ms = end_ms
        if self.active.get(transfer.ecu) is transfer:
            del self.active[transfer.ecu]

    def _complete(self, transfer, ref):
        transfer.status = 'complete'
        transfer.reason = None
        transfer.end_frame = ref.position
        transfer.end_ms = ref.frame.t_ms
        message = LongMessage(transfer.ecu, 'response', transfer.start_frame, transfer.end_ms,
                              bytes(transfer.body), transfer.transfer_id,
                              tuple(deepcopy(transfer.segments)))
        del self.active[transfer.ecu]
        self.completed.append((ref.position, message.to_document()))
        if transfer.operation is not None:
            self.session.complete_long(transfer.operation, message)

    def _diagnose(self, ecu, direction, ref, code, detail):
        self.diagnostics.append(ProtocolDiagnostic(code, detail, ecu, direction,
                                                   ref.position, ref.frame.t_ms))

    def _end(self, ecu, reason, ref, code=None, detail=None):
        """Terminate the in-flight transfer, or record an orphan-frame diagnostic.

        A frame that ends a transfer is already recorded in that transfer's
        ``frames`` list together with the failure reason, so it does not also
        duplicate the evidence in ``protocol_diagnostics``.
        """
        transfer = self.active.get(ecu)
        if transfer is None:
            if code is not None:
                self._diagnose(ecu, 'response', ref, code, detail)
            return None
        self._fail(transfer, reason, ref.position, ref.frame.t_ms, causing=ref.position)
        return transfer

    # -- frame intake ---------------------------------------------------
    def response_single(self, ref, ecu):
        """A response single frame always interrupts an in-flight transfer first."""
        transfer = self.active.get(ecu)
        if transfer is not None:
            transfer.frames.append(ref.position)
            self._fail(transfer, 'interrupted', ref.position, ref.frame.t_ms)

    def response_ff(self, ref, ecu):
        data = ref.frame.data
        if len(data) != 8:
            self._end(ecu, 'malformed', ref, 'malformed_ff',
                      'first frame must contain eight data bytes')
            return
        declared = ((data[0] & 0x0F) << 8) | data[1]
        if not TRANSFER_MIN_LENGTH <= declared <= TRANSFER_MAX_LENGTH:
            self._end(ecu, 'malformed', ref, 'malformed_ff',
                      'declared length must be 8 through 4095')
            return
        previous = self.active.get(ecu)
        if previous is not None:
            self._fail(previous, 'replaced', ref.position, ref.frame.t_ms, causing=ref.position)
        transfer = Transfer(f'{self.run_id}:response:{ref.position}', ecu, ref.position,
                            ref.frame.t_ms, declared)
        transfer.frames.append(ref.position)
        transfer.body.extend(data[2:8])
        transfer.segments.append({'frame': ref.position, 'body_range': [0, 6], 'can_range': [2, 8]})
        transfer.deadline_ms = ref.frame.t_ms + self.timing.N_Bs
        self._bind(transfer, bytes(data[2:8]))
        self.active[ecu] = transfer
        self.transfers.append(transfer)

    def _bind(self, transfer, prefix):
        if prefix[0] != 0x62:
            transfer.binding_reason = 'response_format'
            return
        did = int.from_bytes(prefix[1:3], 'big')
        operation = self.session.active.get(transfer.ecu)
        if operation is None:
            transfer.binding_reason = 'no_active_operation'
            return
        if operation.request.did != did:
            transfer.binding_reason = 'did_mismatch'
            return
        # Fixed at the first frame; nothing later can rebind this transfer.
        transfer.operation = operation
        transfer.operation_id = operation.operation_id
        operation.transfer_ids.append(transfer.transfer_id)

    def response_cf(self, ref, ecu):
        transfer = self.active.get(ecu)
        if transfer is None:
            self._diagnose(ecu, 'response', ref, 'unexpected_cf',
                           'consecutive frame without an in-flight transfer')
            return
        if transfer.state != 'waiting_cf':
            self._fail(transfer, 'unexpected_cf', ref.position, ref.frame.t_ms,
                       causing=ref.position)
            return
        data = ref.frame.data
        if not data:
            self._fail(transfer, 'malformed', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        sequence = data[0] & 0x0F
        if sequence != transfer.next_sequence:
            self._fail(transfer, 'sequence', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        payload = data[1:]
        remaining = transfer.declared_length - len(transfer.body)
        if remaining <= len(payload):
            consumed, final = remaining, True
        elif len(data) == 8:
            consumed, final = 7, False
        else:
            self._fail(transfer, 'malformed', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        start = len(transfer.body)
        transfer.frames.append(ref.position)
        transfer.body.extend(payload[:consumed])
        transfer.segments.append({'frame': ref.position,
                                  'body_range': [start, start + consumed],
                                  'can_range': [1, 1 + consumed]})
        if final:
            self._complete(transfer, ref)
            return
        transfer.next_sequence = (transfer.next_sequence + 1) & 0x0F
        transfer.deadline_ms = ref.frame.t_ms + self.timing.N_Cr
        if transfer.block_remaining is not None:
            transfer.block_remaining -= 1
            if transfer.block_remaining <= 0:
                transfer.state = 'waiting_fc'
                transfer.block_remaining = None
                transfer.deadline_ms = ref.frame.t_ms + self.timing.N_Bs

    def response_fc(self, ref, ecu):
        self._end(ecu, 'unexpected_fc', ref, 'unexpected_fc',
                  'flow-control frame on the response identifier')

    def response_unknown(self, ref, ecu):
        if not ref.frame.data:
            self._end(ecu, 'malformed', ref, 'empty_data', 'empty response frame')
        else:
            self._end(ecu, 'malformed', ref, 'unsupported_pci',
                      'unsupported PCI type on the response identifier')

    def request_flow_control(self, ref, ecu):
        transfer = self.active.get(ecu)
        if transfer is None:
            self._diagnose(ecu, 'request', ref, 'unexpected_fc',
                           'flow-control frame without an in-flight transfer')
            return
        if transfer.state != 'waiting_fc':
            self._fail(transfer, 'unexpected_fc', ref.position, ref.frame.t_ms,
                       causing=ref.position)
            return
        data = ref.frame.data
        if len(data) < 3:
            self._fail(transfer, 'malformed', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        status = data[0] & 0x0F
        if status > 2 or data[2] != 0:
            self._fail(transfer, 'malformed', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        transfer.frames.append(ref.position)
        if status == 2:
            self._fail(transfer, 'overflow', ref.position, ref.frame.t_ms, causing=ref.position)
            return
        if status == 0:
            transfer.state = 'waiting_cf'
            transfer.block_remaining = data[1] if data[1] else None
            transfer.deadline_ms = ref.frame.t_ms + self.timing.N_Cr
        else:
            transfer.state = 'waiting_fc'
            transfer.block_remaining = None
            transfer.deadline_ms = ref.frame.t_ms + self.timing.N_Bs

    # -- results --------------------------------------------------------
    def drain_completed(self):
        completed, self.completed = self.completed, []
        return completed

    def drain_diagnostics(self):
        diagnostics, self.diagnostics = self.diagnostics, []
        return diagnostics

    def snapshot(self):
        return {
            'transfers': [transfer.to_document()
                          for transfer in sorted(self.transfers, key=lambda item: item.start_frame)],
        }
