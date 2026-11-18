from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from diagcapture.archive.store import ArchiveStore
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify, VerificationFailure
from diagcapture.archive.codec import document_digest
from diagcapture.archive.export import export
from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.ingest import CaptureService
from diagcapture.source.identity import CaptureIdentityConflict
from tools.can_fixture import sf, ff, cf, fc

ROOT = Path(__file__).resolve().parent.parent
LONG = json.loads((ROOT / 'samples/long_response.json').read_text(encoding='utf-8'))
CATALOG = LONG['catalog']
QUERY = [0x22, 0x12, 0x34]
BODY = [0x62, 0x12, 0x34, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]
REQ, RSP = 0x700, 0x708
TIMING = {'P2': 10, 'P2star': 50, 'N_Bs': 20, 'N_Cr': 20}


def long_frames(start_ms, body=BODY, ffs_at=None, fc_block=0, gap=1, start_time=None):
    """Build FF + CTS + consecutive frames for one well-formed transfer."""
    start_time = start_ms if start_time is None else start_time
    frames = [ff(start_time, RSP, len(body), body[:6])]
    now = start_time + gap
    frames.append(fc(now, REQ, 0, fc_block))
    now += gap
    position, sequence = 6, 1
    while position < len(body):
        chunk = body[position:position + 7]
        frames.append(cf(now, RSP, sequence, chunk))
        position += len(chunk)
        sequence = (sequence + 1) & 0x0F
        now += gap
    return frames


class IsotpCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='diagcapture-isotp-')
        self.db_path = Path(self.folder.name) / 'capture.sqlite3'
        self.store = ArchiveStore(self.db_path)
        self.registry = CatalogRegistry(self.store.connection())
        self.registry.register_catalog(CATALOG['id'], CATALOG['dids'])
        self.service = CaptureService(self.store, CATALOG['id'])

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def run_isotp(self, run_id, frames, channels=None, timing=None):
        return self.service.capture_isotp(
            run_id, deepcopy(frames), deepcopy(LONG['channels'] if channels is None else channels),
            deepcopy(TIMING if timing is None else timing))

    def view(self, identity, ecu=None):
        return report(self.store, identity, ecu).to_document()


class TestSamples(IsotpCase):
    def test_hand_worked_samples(self):
        for case in LONG['cases']:
            with self.subTest(run_id=case['run_id']):
                identity = self.run_isotp(case['run_id'], case['frames'])
                self.assertEqual(identity, 'report:' + case['run_id'])
                document = self.view(identity)
                expected = case['expected']
                transfer = document['transfers'][0]
                for key in ('start_frame', 'end_frame', 'start_ms', 'end_ms', 'frames', 'body', 'segments'):
                    self.assertEqual(transfer[key], expected[key], key)
                self.assertEqual(transfer['id'], expected['transfer_id'])
                self.assertEqual(transfer['operation_id'], expected['operation_id'])
                self.assertEqual((transfer['status'], transfer['reason'], transfer['binding_reason']),
                                 ('complete', None, None))
                self.assertEqual([row['status'] for row in document['operations']], expected['statuses'])
                self.assertEqual([row['completed_at_ms'] for row in document['operations']],
                                 expected['completed_at_ms'])
                self.assertEqual(document['operations'][0]['transfer_ids'], [transfer['id']])
                if len(document['operations']) > 1:
                    self.assertEqual(document['operations'][1]['transfer_ids'], [])
                messages = [row for row in document['messages'] if row.get('transfer_id') == transfer['id']]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]['body'], BODY)
                self.assertEqual(messages[0]['frame'], expected['start_frame'])
                self.assertEqual(messages[0]['t_ms'], expected['end_ms'])
                self.assertNotIn('body_range', messages[0])
                if case['run_id'] == 'timely':
                    fields = document['operations'][0]['interpretation']['fields']
                    self.assertEqual(fields[0]['raw_integer'], 515)
                    self.assertEqual(fields[0]['value'], {'numerator': 103, 'denominator': 2})
                    self.assertEqual(fields[0]['source'], expected['field']['source'])
                    self.assertEqual(document['operations'][0]['responses'][0]['data'], BODY[3:])
                else:
                    for operation in document['operations']:
                        self.assertEqual(operation['responses'], [])
                        self.assertEqual(operation['interpretation'], {'fields': [], 'diagnostics': []})
                self.assertTrue(verify(self.store, identity).to_document()['verified'])
                self.assertEqual(report(self.store, identity, 'absent')['transfers'], [])
                self.assertEqual(len(report(self.store, identity, 'A')['transfers']), 1)

    def test_timely_requests_frame_is_not_extended(self):
        identity = self.run_isotp('timely', LONG['cases'][0]['frames'])
        operation = self.view(identity)['operations'][0]
        self.assertEqual(operation['deadline_history'],
                         [{'at_ms': 0, 'deadline_ms': 10, 'frame': 0}])
        self.assertEqual(operation['completed_at_ms'], 10)


class TestFlowControl(IsotpCase):
    def test_block_size_forces_extra_flow_control(self):
        body = BODY + list(range(20))
        # Block size one: every consecutive frame exhausts the window, so a
        # further flow-control frame is required before the next one.
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(body), body[:6]),
                  fc(3, REQ, 0, 1), cf(4, RSP, 1, body[6:13]),
                  fc(5, REQ, 0, 1), cf(6, RSP, 2, body[13:20]),
                  fc(7, REQ, 0, 1), cf(8, RSP, 3, body[20:27]),
                  fc(9, REQ, 0, 1), cf(10, RSP, 4, body[27:])]
        document = self.view(self.run_isotp('blocks', frames))
        transfer = document['transfers'][0]
        self.assertEqual(transfer['status'], 'complete')
        self.assertEqual(transfer['body'], list(body))
        self.assertEqual(transfer['frames'], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(document['operations'][0]['status'], 'success')
        self.assertEqual(document['operations'][0]['responses'][0]['data'], list(body[3:]))

    def test_block_exhausted_missing_flow_control_times_out(self):
        body = BODY + list(range(20))
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(body), body[:6]),
                  fc(3, REQ, 0, 1), cf(4, RSP, 1, body[6:13])]
        document = self.view(self.run_isotp('block-timeout', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason']), ('failed', 'N_Bs_timeout'))
        self.assertEqual((transfer['end_frame'], transfer['end_ms']), (None, 24))
        self.assertEqual(transfer['body'], None)
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual(document['operations'][0]['completed_at_ms'], 10)

    def test_last_consecutive_frame_beats_block_exhaustion(self):
        body = BODY + list(range(1))
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(body), body[:6]),
                  fc(3, REQ, 0, 1), cf(4, RSP, 1, body[6:])]
        document = self.view(self.run_isotp('last-cf-block', frames))
        transfer = document['transfers'][0]
        self.assertEqual(transfer['status'], 'complete')
        self.assertEqual(transfer['frames'], [1, 2, 3])
        self.assertEqual(document['operations'][0]['status'], 'success')

    def test_wait_updates_n_bs_without_limit(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6])]
        for index, moment in enumerate((10, 20, 30)):
            frames.append(fc(moment, REQ, 1, 0))
        frames += [fc(50, REQ, 0, 0), cf(51, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('wait', frames))
        transfer = document['transfers'][0]
        self.assertEqual(transfer['status'], 'complete')
        self.assertEqual(transfer['frames'], [1, 2, 3, 4, 5, 6])
        self.assertEqual(transfer['end_ms'], 51)

    def test_overflow_and_malformed_flow_control(self):
        cases = [
            ('overflow', fc(3, REQ, 2, 0), 'overflow'),
            ('bad-stmin', fc(3, REQ, 0, 0, stmin=1), 'malformed'),
            ('bad-status', fc(3, REQ, 0, 0), 'malformed'),
            ('short', {'t_ms': 3, 'can_id': REQ, 'data': [0x30, 0]}, 'malformed'),
        ]
        for name, control, reason in cases:
            with self.subTest(name=name):
                if name == 'bad-status':
                    control = {'t_ms': 3, 'can_id': REQ, 'data': [0x33, 0, 0]}
                frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), control,
                          cf(4, RSP, 1, BODY[6:])]
                document = self.view(self.run_isotp(name, frames))
                transfer = document['transfers'][0]
                self.assertEqual((transfer['status'], transfer['reason']), ('failed', reason))
                self.assertEqual(transfer['end_frame'], 2)
                self.assertEqual(transfer['frames'], [1, 2])
                self.assertEqual(document['operations'][0]['status'], 'timeout')

    def test_flow_control_in_wrong_state_and_orphans(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  fc(3, REQ, 0, 0), fc(4, REQ, 0, 0),
                  cf(5, RSP, 1, BODY[6:]), fc(6, REQ, 0, 0)]
        document = self.view(self.run_isotp('fc-states', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason']), ('failed', 'unexpected_fc'))
        self.assertEqual(transfer['end_frame'], 3)
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unexpected_cf', 'unexpected_fc'])
        self.assertEqual([row['frame'] for row in document['protocol_diagnostics']], [4, 5])
        self.assertEqual(document['messages'], [document['messages'][0]])


class TestSequenceAndErrors(IsotpCase):
    def test_sequence_wrap_fifteen_to_zero(self):
        pieces = bytes(range(1, 112))
        body = list(bytes([0x62, 0x12, 0x34]) + pieces)
        frames = [sf(0, REQ, QUERY), ff(1, RSP, len(body), body[:6]),
                  fc(2, REQ, 0, 0)]
        moment, position, sequence = 3, 6, 1
        sequence_used = []
        while position < len(body):
            chunk = body[position:position + 7]
            frames.append(cf(moment, RSP, sequence, chunk))
            sequence_used.append(sequence)
            position += len(chunk)
            sequence = (sequence + 1) & 0x0F
            moment += 1
        self.assertEqual(sequence_used[-2:], [15, 0])
        document = self.view(self.run_isotp('wrap', frames))
        transfer = document['transfers'][0]
        self.assertEqual(transfer['status'], 'complete')
        self.assertEqual(transfer['body'], body)
        self.assertEqual(transfer['declared_length'], len(body))
        self.assertEqual(len(transfer['segments']), 1 + len(range(6, len(body), 7)))
        self.assertTrue(verify(self.store, 'report:wrap').to_document()['verified'])

    def test_sequence_error_keeps_evidence_only(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  fc(3, REQ, 0, 0), cf(4, RSP, 2, BODY[6:])]
        document = self.view(self.run_isotp('bad-sequence', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason']), ('failed', 'sequence'))
        self.assertEqual((transfer['end_frame'], transfer['end_ms']), (3, 4))
        self.assertEqual(transfer['frames'], [1, 2, 3])
        self.assertEqual(transfer['segments'], [{'frame': 1, 'body_range': [0, 6], 'can_range': [2, 8]}])
        self.assertIsNone(transfer['body'])
        self.assertEqual(document['messages'], [document['messages'][0]])
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual(document['operations'][0]['completed_at_ms'], 10)

    def test_short_non_final_consecutive_frame(self):
        body = BODY + list(range(8))
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(body), body[:6]),
                  fc(3, REQ, 0, 0), {'t_ms': 4, 'can_id': RSP, 'data': [0x21, 1, 2, 3, 4, 5]}]
        document = self.view(self.run_isotp('short-cf', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason']), ('failed', 'malformed'))
        self.assertEqual(transfer['end_frame'], 3)

    def test_timeouts_use_logical_deadline_and_no_frame(self):
        # N_Cr timeout while a consecutive frame is missing.
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  {'t_ms': 100, 'can_id': 0x123, 'data': []}]
        document = self.view(self.run_isotp('ncr-timeout', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['reason'], transfer['end_frame'], transfer['end_ms']),
                         ('N_Cr_timeout', None, 23))
        self.assertEqual(transfer['frames'], [1, 2])
        # N_Bs timeout while waiting for flow control.
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  {'t_ms': 100, 'can_id': 0x123, 'data': []}]
        document = self.view(self.run_isotp('nbs-timeout', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['reason'], transfer['end_frame'], transfer['end_ms']),
                         ('N_Bs_timeout', None, 22))
        self.assertEqual(document['operations'][0]['status'], 'timeout')

    def test_equal_deadline_frames_are_eligible(self):
        # N_Cr deadline is 23; a consecutive frame exactly at 23 still counts.
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  cf(23, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp(
            'equal-deadline', frames,
            timing={'P2': 50, 'P2star': 50, 'N_Bs': 20, 'N_Cr': 20}))
        self.assertEqual(document['transfers'][0]['status'], 'complete')
        self.assertEqual(document['operations'][0]['status'], 'success')

    def test_input_end_settles_transfer_at_deadline(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0)]
        document = self.view(self.run_isotp('end-settle', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_ms']),
                         ('failed', 'N_Cr_timeout', 23))


class TestOwnership(IsotpCase):
    def test_binding_reasons(self):
        frames = [
            # no active operation
            ff(0, RSP, len(BODY), BODY[:6]), fc(1, REQ, 0, 0), cf(2, RSP, 1, BODY[6:]),
            # active operation with a different DID
            sf(5, REQ, [0x22, 0x99, 0x99]),
            ff(6, RSP, len(BODY), BODY[:6]), fc(7, REQ, 0, 0), cf(8, RSP, 1, BODY[6:]),
            # active operation with matching DID but a non-0x62 prefix
            sf(12, REQ, QUERY),
            ff(13, RSP, len(BODY), [0x7F, 0x22, 0x31, 0, 0, 0]), fc(14, REQ, 0, 0),
            cf(15, RSP, 1, BODY[6:]),
        ]
        document = self.view(self.run_isotp('reasons', frames))
        reasons = [transfer['binding_reason'] for transfer in document['transfers']]
        self.assertEqual(reasons, ['no_active_operation', 'did_mismatch', 'response_format'])
        self.assertEqual([transfer['operation_id'] for transfer in document['transfers']],
                         [None, None, None])
        self.assertEqual([transfer['status'] for transfer in document['transfers']],
                         ['complete', 'complete', 'complete'])
        self.assertEqual(document['operations'][0]['transfer_ids'], [])
        self.assertEqual(document['operations'][0]['responses'], [])
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual(len([row for row in document['messages'] if 'transfer_id' in row]), 3)

    def test_replacement_keeps_old_ownership(self):
        short = BODY + list(range(10))
        second = BODY
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(short), short[:6]), fc(3, REQ, 0, 0),
                  cf(4, RSP, 1, short[6:13]),
                  ff(5, RSP, len(second), second[:6]), fc(6, REQ, 0, 0),
                  cf(7, RSP, 1, second[6:])]
        document = self.view(self.run_isotp('replace', frames))
        old, new = document['transfers']
        self.assertEqual((old['status'], old['reason']), ('failed', 'replaced'))
        self.assertEqual((old['end_frame'], old['end_ms']), (4, 5))
        self.assertEqual(old['operation_id'], 'replace:request:0')
        self.assertIsNone(old['body'])
        self.assertEqual((new['status'], new['end_frame']), ('complete', 6))
        self.assertEqual(document['operations'][0]['transfer_ids'], [old['id'], new['id']])
        self.assertEqual(document['operations'][0]['status'], 'success')
        self.assertEqual(document['operations'][0]['responses'][0]['message']['transfer_id'], new['id'])

    def test_interrupted_by_single_frame_and_78(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  sf(4, RSP, [0x7F, 0x22, 0x78])]
        document = self.view(self.run_isotp('interrupted', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_frame']),
                         ('failed', 'interrupted', 3))
        operation = document['operations'][0]
        self.assertEqual(operation['status'], 'timeout')
        self.assertEqual(operation['completed_at_ms'], 54)
        self.assertEqual([row['kind'] for row in operation['responses']], ['pending'])
        self.assertEqual(operation['transfer_ids'], [transfer['id']])

    def test_request_single_frame_does_not_interrupt(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  sf(3, REQ, [0x22, 0x12, 0x34]),
                  fc(4, REQ, 0, 0), cf(5, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('request-sf', frames))
        self.assertEqual(document['transfers'][0]['status'], 'complete')
        self.assertEqual(document['operations'][0]['status'], 'success')
        self.assertEqual([row['code'] for row in document['association_diagnostics']], ['active_request'])

    def test_request_ff_and_cf_are_only_diagnostics(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  ff(3, REQ, 12, BODY[:6]), cf(4, REQ, 1, BODY[6:]),
                  fc(5, REQ, 0, 0), cf(6, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('request-long', frames))
        self.assertEqual(document['transfers'][0]['status'], 'complete')
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unsupported_pci', 'unsupported_pci'])

    def test_unowned_completion_does_not_attach(self):
        frames = [ff(0, RSP, len(BODY), BODY[:6]), fc(1, REQ, 0, 0), cf(2, RSP, 1, BODY[6:]),
                  sf(3, REQ, QUERY)]
        document = self.view(self.run_isotp('no-rebind', frames))
        transfer = document['transfers'][0]
        self.assertEqual(transfer['binding_reason'], 'no_active_operation')
        self.assertEqual(transfer['status'], 'complete')
        self.assertEqual(document['operations'][0]['responses'], [])
        self.assertEqual(document['operations'][0]['status'], 'timeout')

    def test_failed_transfer_does_not_end_operation(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  {'t_ms': 4, 'can_id': RSP, 'data': [0x25, 0, 0]},
                  sf(9, RSP, [0x62, 0x12, 0x34, 1])]
        document = self.view(self.run_isotp('failed-evidence', frames))
        self.assertEqual(document['transfers'][0]['reason'], 'sequence')
        operation = document['operations'][0]
        self.assertEqual(operation['status'], 'success')
        self.assertEqual(operation['responses'][0]['message']['frame'], 4)
        self.assertEqual(operation['transfer_ids'], [document['transfers'][0]['id']])

    def test_complete_78_still_extends(self):
        frames = [sf(0, REQ, QUERY), sf(8, RSP, [0x7F, 0x22, 0x78]),
                  ff(10, RSP, len(BODY), BODY[:6]), fc(11, REQ, 0, 0),
                  cf(12, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('pending-long', frames))
        operation = document['operations'][0]
        self.assertEqual(operation['status'], 'success')
        self.assertEqual([row['kind'] for row in operation['responses']], ['pending', 'positive'])
        self.assertEqual([row['deadline_ms'] for row in operation['deadline_history']], [10, 58])
        self.assertEqual(operation['completed_at_ms'], 12)

    def test_two_ecus_interleave_and_unknown_tick(self):
        channels = LONG['channels'] + [{'ecu': 'B', 'request_id': 0x710, 'response_id': 0x718}]
        frames = [sf(0, 0x700, QUERY), sf(0, 0x710, QUERY),
                  ff(2, 0x708, len(BODY), BODY[:6]),
                  ff(3, 0x718, len(BODY), BODY[:6]),
                  fc(4, 0x700, 0, 0), fc(5, 0x710, 0, 0),
                  {'t_ms': 6, 'can_id': 0x123, 'data': [0xFF]},
                  cf(7, 0x718, 1, BODY[6:]), cf(8, 0x708, 1, BODY[6:])]
        document = self.view(self.run_isotp('dual-ecu', frames, channels))
        self.assertEqual([row['ecu'] for row in document['transfers']], ['A', 'B'])
        self.assertEqual([row['status'] for row in document['transfers']], ['complete', 'complete'])
        self.assertEqual([row['status'] for row in document['operations']], ['success', 'success'])
        self.assertEqual([row['end_frame'] for row in document['transfers']], [8, 7])
        selected = self.view('report:dual-ecu', 'B')
        self.assertEqual([row['ecu'] for row in selected['transfers']], ['B'])
        self.assertEqual([row['ecu'] for row in selected['operations']], ['B'])
        self.assertEqual(selected['frames'], document['frames'])

    def test_unknown_ids_and_untouched_ecu(self):
        channels = LONG['channels'] + [{'ecu': 'B', 'request_id': 0x710, 'response_id': 0x718}]
        frames = [sf(0, 0x700, QUERY), ff(2, 0x708, len(BODY), BODY[:6]),
                  {'t_ms': 5, 'can_id': 0x999, 'data': []},
                  ff(6, 0x718, len(BODY), BODY[:6]),
                  fc(7, 0x700, 0, 0), cf(8, 0x708, 1, BODY[6:])]
        document = self.view(self.run_isotp('untouched', frames, channels))
        self.assertEqual([row['status'] for row in document['transfers']], ['complete', 'failed'])
        self.assertEqual(document['transfers'][1]['reason'], 'N_Bs_timeout')
        self.assertEqual(document['transfers'][1]['end_frame'], None)


class TestFixedEvidence(IsotpCase):
    def test_profile_participates_in_identity(self):
        frames = LONG['cases'][0]['frames']
        identity = self.run_isotp('shared', frames)
        self.assertEqual(self.run_isotp('shared', frames), identity)
        with self.assertRaises(CaptureIdentityConflict):
            self.service.capture('shared', deepcopy(frames), deepcopy(LONG['channels']), TIMING)
        with self.assertRaises(CaptureIdentityConflict):
            self.run_isotp('shared', frames, timing={'P2': 11, 'P2star': 50, 'N_Bs': 20, 'N_Cr': 20})
        v1 = self.service.capture('v1-run', [sf(0, REQ, QUERY), sf(1, RSP, [0x62, 0x12, 0x34, 1])],
                                  deepcopy(LONG['channels']), deepcopy(TIMING))
        self.assertNotIn('transfers', self.view(v1))
        self.assertNotIn('profile', self.store.load_bundle(v1).document['evidence'])

    def test_reopen_export_and_tamper(self):
        identity = self.run_isotp('fixed', LONG['cases'][0]['frames'])
        before = self.view(identity)
        self.store.close()
        self.store = ArchiveStore(self.db_path)
        self.assertEqual(self.view(identity), before)
        target = Path(self.folder.name) / 'offline'
        export(self.store, identity, target)
        self.store.close()
        self.db_path.unlink()
        offline = ArchiveStore(target)
        self.assertEqual(report(offline, identity).to_document(), before)
        self.assertTrue(verify(offline, identity).to_document()['verified'])
        offline.close()
        self.store = ArchiveStore(self.db_path)

    def test_modified_transfer_or_source_fails_verify(self):
        identity = self.run_isotp('tamper', LONG['cases'][0]['frames'])
        bundle = self.store.load_bundle(identity).to_document()
        bundle['result']['transfers'][0]['operation_id'] = None
        bundle['manifest'] = {name + '_sha256': document_digest(bundle[name])
                              for name in ('evidence', 'catalog', 'result')}
        self.store.connection().execute('UPDATE bundles SET document=? WHERE report_id=?',
                                        (json.dumps(bundle, sort_keys=True, separators=(',', ':')), identity))
        with self.assertRaises(VerificationFailure):
            verify(self.store, identity)

    def test_bad_input_preserves_published_report(self):
        identity = self.run_isotp('preserved', LONG['cases'][0]['frames'])
        before = self.view(identity)
        broken = deepcopy(LONG['cases'][0]['frames'])
        broken[1]['data'] = broken[1]['data'][:7]
        with self.assertRaises(ValueError):
            self.run_isotp('preserved', broken)
        self.assertEqual(self.view(identity), before)
        self.assertEqual(len(self.store.connection().execute('SELECT * FROM reports').fetchall()), 1)


if __name__ == '__main__':
    unittest.main()


class TestLongInterpretation(IsotpCase):
    def test_unknown_did_and_length_errors(self):
        unknown = bytes([0x62, 0x99, 0x99]) + bytes(9)
        frames = [sf(0, REQ, [0x22, 0x99, 0x99]), ff(1, RSP, len(unknown), unknown[:6]),
                  fc(2, REQ, 0, 0), cf(3, RSP, 1, unknown[6:])]
        document = self.view(self.run_isotp('unknown-long', frames))
        operation = document['operations'][0]
        self.assertEqual(operation['status'], 'success')
        self.assertEqual(operation['interpretation']['fields'], [])
        self.assertEqual(operation['interpretation']['diagnostics'][0]['code'], 'unknown_did')
        self.assertEqual(operation['interpretation']['diagnostics'][0]['frame'], 1)
        # declared length 8: the catalog expects 9 bytes of data
        short_body = bytes([0x62, 0x12, 0x34]) + bytes(5)
        frames = [sf(0, REQ, QUERY), ff(1, RSP, len(short_body), short_body[:6]),
                  fc(2, REQ, 0, 0), cf(3, RSP, 1, short_body[6:])]
        document = self.view(self.run_isotp('short-long', frames))
        self.assertEqual(document['operations'][0]['interpretation']['diagnostics'][0]['code'], 'data_length')

    def test_v1_entry_still_rejects_multiframe(self):
        identity = self.service.capture('v1-multiframe', deepcopy(LONG['cases'][0]['frames']),
                                        deepcopy(LONG['channels']), deepcopy(TIMING))
        document = self.view(identity)
        self.assertNotIn('transfers', document)
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual(document['messages'][0]['body'], [0x22, 0x12, 0x34])
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unsupported_pci', 'unsupported_pci', 'unsupported_pci'])

    def test_empty_and_single_frame_runs(self):
        empty = self.view(self.run_isotp('empty-long', []))
        self.assertEqual((empty['transfers'], empty['operations']), ([], []))
        single = self.view(self.run_isotp('single-long',
                                          [sf(0, REQ, QUERY), sf(5, RSP, [0x62, 0x12, 0x34, 1])]))
        self.assertEqual(single['transfers'], [])
        self.assertEqual(single['operations'][0]['status'], 'success')
        self.assertEqual(single['operations'][0]['transfer_ids'], [])
        self.assertEqual(single['operations'][0]['interpretation']['diagnostics'][0]['code'], 'data_length')


def _long_transfer(body, start_ms=1, block=0):
    frames = [ff(start_ms, RSP, len(body), body[:6]), fc(start_ms + 1, REQ, 0, block)]
    position, sequence, moment = 6, 1, start_ms + 2
    while position < len(body):
        chunk = body[position:position + 7]
        frames.append(cf(moment, RSP, sequence, chunk))
        position += len(chunk)
        sequence = (sequence + 1) & 0x0F
        moment += 1
    return frames


class TestLengthBoundaries(IsotpCase):
    def test_minimum_and_maximum_declared_lengths(self):
        body8 = list(range(8))
        body8[:3] = [0x62, 0x12, 0x34]
        body_max = [0x62, 0x12, 0x34] + [index % 256 for index in range(4092)]
        timing = {'P2': 5000, 'P2star': 50, 'N_Bs': 20, 'N_Cr': 20}
        for name, body in (('length-8', body8), ('length-4095', body_max)):
            with self.subTest(name=name):
                frames = [sf(0, REQ, QUERY)] + _long_transfer(body)
                document = self.view(self.run_isotp(name, frames, timing=timing))
                transfer = document['transfers'][0]
                self.assertEqual((transfer['declared_length'], transfer['body']), (len(body), body))
                self.assertEqual(transfer['segments'][0], {'frame': 1, 'body_range': [0, 6], 'can_range': [2, 8]})
                positions = [segment['body_range'] for segment in transfer['segments']]
                self.assertEqual(positions[0][0], 0)
                self.assertEqual(positions[-1][1], len(body))
                for left, right in zip(positions, positions[1:]):
                    self.assertEqual(left[1], right[0])
                self.assertEqual(document['operations'][0]['status'], 'success')
                self.assertTrue(verify(self.store, 'report:' + name).to_document()['verified'])

    def test_malformed_first_frames_never_start(self):
        cases = {
            'seven-bytes': [0x10, 8, 1, 2, 3, 4, 5],
            'short-declared': [0x10, 7, 1, 2, 3, 4, 5, 6],
            'zero-declared': [0x10, 0, 1, 2, 3, 4, 5, 6],
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                document = self.view(self.run_isotp(
                    'ff-' + name, [sf(0, REQ, QUERY), {'t_ms': 1, 'can_id': RSP, 'data': data}]))
                self.assertEqual(document['transfers'], [])
                self.assertEqual(document['operations'][0]['status'], 'timeout')
                self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                                 ['malformed_ff'])
                self.assertEqual(document['protocol_diagnostics'][0]['frame'], 1)
                self.assertEqual(document['messages'], [document['messages'][0]])

    def test_first_frame_low_nibble_is_length_high_bits(self):
        # 0x12 is a legal first frame whose declared length is 0x0208.
        data = [0x12, 0x08, 0x62, 0x12, 0x34, 1, 2, 3]
        document = self.view(self.run_isotp(
            'ff-high-bits', [sf(0, REQ, QUERY), {'t_ms': 1, 'can_id': RSP, 'data': data}]))
        self.assertEqual(document['transfers'][0]['declared_length'], 0x208)
        self.assertEqual(document['protocol_diagnostics'], [])

    def test_bad_first_frame_terminates_transfer(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  {'t_ms': 4, 'can_id': RSP, 'data': [0x10, 8, 1, 2, 3, 4, 5]},
                  cf(5, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('bad-ff-active', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_frame']),
                         ('failed', 'malformed', 3))
        self.assertEqual(transfer['frames'], [1, 2, 3])
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        # the malformed first frame is the transfer's own evidence; the later
        # consecutive frame is now an orphan and gets the position diagnostic
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']], ['unexpected_cf'])
        self.assertEqual(document['protocol_diagnostics'][0]['frame'], 4)

    def test_declared_length_prefix_bits(self):
        # the low nibble of the first byte carries the high length bits
        body = [0x62, 0x12, 0x34] + [index % 256 for index in range(9)]
        frames = [sf(0, REQ, QUERY), ff(1, RSP, len(body), body[:6]), fc(2, REQ, 0, 0),
                  cf(3, RSP, 1, body[6:])]
        document = self.view(self.run_isotp('prefix-bits', frames))
        self.assertEqual(document['transfers'][0]['declared_length'], 12)
        self.assertEqual(document['transfers'][0]['body'], body)


class TestResponseSideStrays(IsotpCase):
    def test_response_side_flow_control(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  fc(3, RSP, 0, 0, stmin=0), cf(4, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('response-fc', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_frame']),
                         ('failed', 'unexpected_fc', 2))
        # the trailing consecutive frame is now orphaned evidence, not a message
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unexpected_cf'])
        self.assertEqual(document['protocol_diagnostics'][0]['frame'], 3)
        self.assertEqual(document['messages'], [document['messages'][0]])

    def test_orphan_consecutive_and_flow_control_are_evidence_only(self):
        frames = [sf(0, REQ, QUERY), cf(1, RSP, 1, BODY[6:]), fc(2, REQ, 0, 0),
                  sf(3, RSP, [0x62, 0x12, 0x34, 1])]
        document = self.view(self.run_isotp('orphans', frames))
        self.assertEqual(document['transfers'], [])
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unexpected_cf', 'unexpected_fc'])
        self.assertEqual([(row['frame'], row['direction']) for row in document['protocol_diagnostics']],
                         [(1, 'response'), (2, 'request')])
        self.assertEqual(document['operations'][0]['status'], 'success')

    def test_empty_and_unknown_pci_terminate_only_active_transfer(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  {'t_ms': 4, 'can_id': RSP, 'data': []},
                  {'t_ms': 5, 'can_id': RSP, 'data': [0x45, 0, 0]}]
        document = self.view(self.run_isotp('empty-active', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_frame']),
                         ('failed', 'malformed', 3))
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unsupported_pci'])
        self.assertEqual(document['protocol_diagnostics'][0]['frame'], 4)


class TestLateBodyAndOrdering(IsotpCase):
    def test_completion_after_operation_timeout_is_evidence_only(self):
        # Request at 0 (P2=10). FF at 2 binds; the body completes at 12, after
        # the operation's deadline.  No new query exists to steal the body.
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]), fc(3, REQ, 0, 0),
                  cf(12, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('late-no-query', frames))
        operation = document['operations'][0]
        self.assertEqual((operation['status'], operation['completed_at_ms']), ('timeout', 10))
        self.assertEqual(operation['responses'], [])
        self.assertEqual(operation['interpretation'], {'fields': [], 'diagnostics': []})
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['end_frame']), ('complete', 3))
        self.assertEqual(operation['transfer_ids'], [transfer['id']])
        self.assertEqual(len([row for row in document['messages'] if 'transfer_id' in row]), 1)

    def test_consecutive_frame_before_flow_control(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  cf(3, RSP, 1, BODY[6:]), fc(4, REQ, 0, 0)]
        document = self.view(self.run_isotp('cf-before-fc', frames))
        transfer = document['transfers'][0]
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_frame']),
                         ('failed', 'unexpected_cf', 2))
        self.assertEqual(transfer['frames'], [1, 2])
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['unexpected_fc'])
        self.assertEqual(document['protocol_diagnostics'][0]['frame'], 3)

    def test_equal_time_flow_control_and_consecutive_frame(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  fc(3, REQ, 0, 0), cf(3, RSP, 1, BODY[6:])]
        document = self.view(self.run_isotp('same-time-transfer', frames))
        self.assertEqual(document['transfers'][0]['status'], 'complete')
        # reversed order at the same millisecond is not eligible
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6]),
                  cf(3, RSP, 1, BODY[6:]), fc(3, REQ, 0, 0)]
        document = self.view(self.run_isotp('same-time-reversed', frames))
        self.assertEqual(document['transfers'][0]['reason'], 'unexpected_cf')

    def test_changed_raw_input_conflicts(self):
        identity = self.run_isotp('changed', LONG['cases'][0]['frames'])
        changed = deepcopy(LONG['cases'][0]['frames'])
        changed[3]['data'][2] = 0xAA
        with self.assertRaises(CaptureIdentityConflict):
            self.run_isotp('changed', changed)
        self.assertEqual(len(self.store.connection().execute('SELECT * FROM reports').fetchall()), 1)
        self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_end_of_input_settles_operation_and_transfer(self):
        frames = [sf(0, REQ, QUERY), ff(2, RSP, len(BODY), BODY[:6])]
        document = self.view(self.run_isotp('end-both', frames))
        operation, transfer = document['operations'][0], document['transfers'][0]
        self.assertEqual((operation['status'], operation['completed_at_ms']), ('timeout', 10))
        self.assertEqual((transfer['status'], transfer['reason'], transfer['end_ms']),
                         ('failed', 'N_Bs_timeout', 22))
        self.assertEqual(transfer['end_frame'], None)
