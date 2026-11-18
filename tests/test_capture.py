from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from diagcapture.archive.store import ArchiveStore
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify
from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.ingest import CaptureService
from tools.can_fixture import sf

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = json.loads((ROOT / 'samples/workday.json').read_text(encoding='utf-8'))


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='diagcapture-test-')
        self.db_path = Path(self.folder.name) / 'capture.sqlite3'
        self.store = ArchiveStore(self.db_path)
        self.registry = CatalogRegistry(self.store.connection())
        self.registry.register_catalog(SAMPLE['catalog']['id'], SAMPLE['catalog']['dids'])

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def capture(self, run_id='workday', frames=None, channels=None, timing=None, catalog_id='lab-v1'):
        return CaptureService(self.store, catalog_id).capture(
            run_id, deepcopy(SAMPLE['frames'] if frames is None else frames),
            deepcopy(SAMPLE['channels'] if channels is None else channels),
            deepcopy(SAMPLE['timing'] if timing is None else timing))

    def view(self, identity):
        return report(self.store, identity).to_document()


class TestSingleCapture(ArchiveCase):
    def test_scaled_did(self):
        identity = self.capture()
        document = self.view(identity)
        operation = document['operations'][0]
        fields = operation['interpretation']['fields']
        expected = json.loads((ROOT / 'samples/expected.json').read_text(encoding='utf-8'))
        actual = {
            'report_id': identity, 'operation_id': operation['id'], 'status': operation['status'],
            'temperature': fields[0]['value'], 'voltage': fields[1]['value'],
            'response_frame': operation['responses'][0]['message']['frame'],
            'field_can_ranges': [field['source']['can_range'] for field in fields],
        }
        self.assertEqual(actual, expected)
        self.assertEqual([field['raw_integer'] for field in fields], [-100, 12345])
        self.assertEqual([field['unit'] for field in fields], ['degC', 'V'])
        self.assertEqual(operation['request']['frame'], 0)
        self.assertEqual(document['frames'][1]['data'], [7, 98, 18, 52, 255, 156, 57, 48])
        downstream = verify(self.store, identity).to_document()
        self.assertTrue(downstream['verified'])
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v1', 'input': SAMPLE['frames'], 'expected': expected, 'actual': actual,
            'downstream_expected': {'verified': True, 'operation_count': 1},
            'downstream_actual': {key: downstream[key] for key in ('verified', 'operation_count')},
            'code_paths': ['diagcapture/source/ingest.py', 'diagcapture/transport/single.py',
                           'diagcapture/operations/replay.py', 'diagcapture/did/values.py',
                           'diagcapture/archive/verify.py'],
            'oracle': 'Independent hexadecimal, two-complement and rational hand calculation in samples/expected.json',
        }, sort_keys=True))

    def test_exact_fields(self):
        layout = {'did': 4660, 'length': 2, 'fields': [{
            'name': 'calibrated', 'offset': 0, 'width': 2, 'byteorder': 'big', 'signed': True,
            'scale': [1, 10], 'bias': [1, 3], 'unit': 'u',
        }]}
        self.registry.register_catalog('offset', [layout])
        cases = [([255, 255], -1, [7, 30]), ([128, 0], -32768, [-49147, 15]),
                 ([0, 0], 0, [1, 3]), ([127, 255], 32767, [98311, 30])]
        for index, (raw, integer, exact) in enumerate(cases):
            with self.subTest(raw=raw):
                identity = self.capture(f'exact-{index}', [sf(0, 0x700, [0x22, 0x12, 0x34]),
                    sf(1, 0x708, [0x62, 0x12, 0x34] + raw)], catalog_id='offset')
                operation = self.view(identity)['operations'][0]
                value = operation['interpretation']['fields'][0]
                self.assertEqual(operation['status'], 'success')
                self.assertEqual(value['raw_integer'], integer)
                self.assertEqual(value['value'], {'numerator': exact[0], 'denominator': exact[1]})
                self.assertEqual(value['source'], {'frame': 1, 'body_range': [3, 5], 'can_range': [4, 6]})
                self.assertEqual(value['raw_bytes'], raw)

    def test_decode_failure(self):
        for index, (did, data, code) in enumerate([(0x9999, [1], 'unknown_did'), (0x1234, [1], 'data_length')]):
            with self.subTest(code=code):
                query = [0x22, did >> 8, did & 255]
                response = [0x62, did >> 8, did & 255] + data
                identity = self.capture(f'decode-{index}', [sf(0, 0x700, query), sf(1, 0x708, response)])
                operation = self.view(identity)['operations'][0]
                self.assertEqual(operation['status'], 'success')
                self.assertEqual(operation['interpretation']['fields'], [])
                self.assertEqual(operation['interpretation']['diagnostics'][0]['code'], code)
                self.assertEqual(operation['responses'][0]['data'], data)
                self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_negative_response(self):
        identity = self.capture('negative', [sf(0, 0x700, [0x22, 0x12, 0x34]), sf(1, 0x708, [0x7F, 0x22, 0x31])])
        operation = self.view(identity)['operations'][0]
        self.assertEqual(operation['status'], 'negative')
        self.assertEqual(operation['responses'][0]['nrc'], 0x31)
        self.assertEqual(operation['interpretation'], {'fields': [], 'diagnostics': []})

    def test_report_filter(self):
        channels = SAMPLE['channels'] + [{'ecu': 'B', 'request_id': 0x710, 'response_id': 0x718}]
        frames = [sf(0, 0x700, [0x22, 0x12, 0x34]), sf(0, 0x710, [0x22, 0x12, 0x34]),
                  sf(2, 0x718, [0x62, 0x12, 0x34, 0, 10, 0, 0]),
                  sf(3, 0x708, [0x62, 0x12, 0x34, 0, 20, 0, 0])]
        identity = self.capture('filter', frames, channels)
        full = self.view(identity)
        selected = report(self.store, identity, 'B').to_document()
        self.assertEqual(selected['operations'], [full['operations'][1]])
        self.assertEqual(selected['messages'], [full['messages'][1], full['messages'][2]])
        self.assertEqual(selected['report_id'], identity)
        self.assertEqual(selected['frames'], full['frames'])
        self.assertEqual(report(self.store, identity, 'absent')['operations'], [])
        selected['operations'].clear()
        self.assertEqual(len(self.view(identity)['operations']), 2)


class TestCaptureValidation(ArchiveCase):
    def test_time_regression_preserves_report(self):
        identity = self.capture()
        before = self.view(identity)
        frames = deepcopy(SAMPLE['frames'])
        frames[1]['t_ms'] = 0
        frames[0]['t_ms'] = 1
        with self.assertRaises(ValueError):
            self.capture('workday', frames)
        self.assertEqual(self.view(identity), before)
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM reports').fetchone()[0], 1)

    def test_structure_and_config(self):
        identity = self.capture()
        before = self.view(identity)
        bad_frames = [None, {}, [None], [{'t_ms': 0, 'can_id': 1}],
                      [{'t_ms': True, 'can_id': 1, 'data': []}],
                      [{'t_ms': 0, 'can_id': True, 'data': []}],
                      [{'t_ms': -1, 'can_id': 1, 'data': []}],
                      [{'t_ms': 0, 'can_id': 0x20000000, 'data': []}],
                      [{'t_ms': 0, 'can_id': 1, 'data': [0] * 9}],
                      [{'t_ms': 0, 'can_id': 1, 'data': [None]}],
                      [{'t_ms': 0, 'can_id': 1, 'data': [True]}],
                      [{'t_ms': 0, 'can_id': 1, 'data': [256]}],
                      [{'t_ms': 0, 'can_id': 1, 'data': '00'}],
                      [{'t_ms': 1.5, 'can_id': 1, 'data': []}]]
        service = CaptureService(self.store, 'lab-v1')
        for index, frames in enumerate(bad_frames):
            with self.subTest(index=index), self.assertRaises(ValueError):
                service.capture(f'bad-{index}', frames, SAMPLE['channels'], SAMPLE['timing'])
        for key in SAMPLE['timing']:
            for bad in (0, -1, True, None, 1.5):
                timing = deepcopy(SAMPLE['timing'])
                timing[key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    self.capture('bad-timing', timing=timing)
        with self.assertRaises(ValueError):
            self.capture('bad-timing', timing={'P2': 10})
        with self.assertRaises(ValueError):
            self.capture('')
        empty = self.capture('empty', [])
        self.assertEqual(self.view(empty)['operations'], [])
        self.assertTrue(verify(self.store, empty).to_document()['verified'])
        self.assertEqual(self.view(identity), before)

    def test_channel_overlap(self):
        invalid = [
            [{'ecu': 'A', 'request_id': 1, 'response_id': 1}],
            [{'ecu': 'A', 'request_id': 1, 'response_id': 2}, {'ecu': 'B', 'request_id': 2, 'response_id': 3}],
            [{'ecu': 'A', 'request_id': 1, 'response_id': 2}, {'ecu': 'A', 'request_id': 3, 'response_id': 4}],
            [{'ecu': 'A', 'request_id': True, 'response_id': 2}],
        ]
        for channels in invalid:
            with self.subTest(channels=channels), self.assertRaises(ValueError):
                self.capture('overlap', channels=channels)
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM reports').fetchone()[0], 0)

    def test_catalog_rejection(self):
        original = self.registry.load_catalog('lab-v1').digest
        mutations = []
        for field_key, value in [('offset', 3), ('width', 0), ('byteorder', 'native'),
                                 ('signed', 0), ('scale', [1, 0]), ('bias', [1, 0])]:
            rows = deepcopy(SAMPLE['catalog']['dids'])
            rows[0]['fields'][0][field_key] = value
            mutations.append(rows)
        rows = deepcopy(SAMPLE['catalog']['dids'])
        rows[0]['fields'][1]['offset'] = 1
        mutations.append(rows)
        rows = deepcopy(SAMPLE['catalog']['dids'])
        rows[0]['fields'][1]['name'] = rows[0]['fields'][0]['name']
        mutations.append(rows)
        mutations.append(deepcopy(SAMPLE['catalog']['dids']) * 2)
        for index, rows in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.registry.register_catalog(f'invalid-{index}', rows)
        changed = deepcopy(SAMPLE['catalog']['dids'])
        changed[0]['fields'][0]['scale'] = [1, 100]
        with self.assertRaises(ValueError):
            self.registry.register_catalog('lab-v1', changed)
        self.registry.register_catalog('lab-v1', SAMPLE['catalog']['dids'])
        self.assertEqual(self.registry.load_catalog('lab-v1').digest, original)

    def test_equal_time_order(self):
        frames = [sf(0, 0x700, [0x22, 0x12, 0x34]), sf(0, 0x708, [0x7F, 0x22, 0x31]),
                  {'t_ms': 0, 'can_id': 1, 'data': [255]}]
        identity = self.capture('same-time', frames)
        document = self.view(identity)
        self.assertEqual(document['frames'], [dict(position=index, **frame) for index, frame in enumerate(frames)])
        self.assertEqual([message['frame'] for message in document['messages']], [0, 1])
        self.assertEqual(document['operations'][0]['status'], 'negative')
        self.assertEqual(document['protocol_diagnostics'], [])

    def test_cli_failure_and_repeat_initialization(self):
        identity = self.capture()
        bad = Path(self.folder.name) / 'invalid.json'
        bad.write_text(json.dumps({'run_id': 'bad', 'frames': None,
                                  'channels': SAMPLE['channels'], 'timing': SAMPLE['timing']}), encoding='utf-8')
        prefix = [sys.executable, '-B', '-m', 'diagcapture', '--db', str(self.db_path)]
        for _ in range(2):
            completed = subprocess.run(prefix + ['init'], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        completed = subprocess.run(prefix + ['capture', '--catalog', 'lab-v1', '--input', str(bad)],
                                   cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn('error', json.loads(completed.stderr))
        completed = subprocess.run(prefix + ['report', '--id', 'report:missing'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.view(identity)['operations'][0]['status'], 'success')
