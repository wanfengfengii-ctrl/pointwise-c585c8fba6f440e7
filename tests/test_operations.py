import unittest
import tests.test_capture as fixtures
from diagcapture.archive.verify import verify
from tools.can_fixture import sf

QUERY = [0x22, 0x12, 0x34]
POSITIVE = [0x62, 0x12, 0x34, 0, 10, 0, 0]
PENDING = [0x7F, 0x22, 0x78]


class TestOperations(fixtures.ArchiveCase):
    def test_pending_then_success(self):
        identity = self.capture('pending', [sf(0, 0x700, QUERY), sf(8, 0x708, PENDING), sf(50, 0x708, POSITIVE)])
        operation = self.view(identity)['operations'][0]
        self.assertEqual(operation['status'], 'success')
        self.assertEqual([row['kind'] for row in operation['responses']], ['pending', 'positive'])
        self.assertEqual([row['deadline_ms'] for row in operation['deadline_history']], [10, 58])
        self.assertEqual(operation['completed_at_ms'], 50)
        self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_pending_chain(self):
        identity = self.capture('chain', [sf(0, 0x700, QUERY), sf(8, 0x708, PENDING),
                                         sf(55, 0x708, PENDING), sf(100, 0x708, POSITIVE)])
        operation = self.view(identity)['operations'][0]
        self.assertEqual(operation['status'], 'success')
        self.assertEqual([row['deadline_ms'] for row in operation['deadline_history']], [10, 58, 105])
        self.assertEqual([row['message']['frame'] for row in operation['responses']], [1, 2, 3])
        self.assertEqual(len(self.view(identity)['operations']), 1)

    def test_timeout_and_other_ecu(self):
        channels = fixtures.SAMPLE['channels'] + [{'ecu': 'B', 'request_id': 0x710, 'response_id': 0x718}]
        frames = [sf(0, 0x700, QUERY), sf(5, 0x710, QUERY),
                  sf(11, 0x708, POSITIVE), sf(14, 0x718, POSITIVE)]
        document = self.view(self.capture('timeout-isolation', frames, channels))
        first, second = document['operations']
        self.assertEqual((first['ecu'], first['status'], first['completed_at_ms']), ('A', 'timeout', 10))
        self.assertEqual(first['responses'], [])
        self.assertEqual((second['ecu'], second['status']), ('B', 'success'))
        self.assertEqual(second['responses'][0]['message']['frame'], 3)
        self.assertEqual(second['interpretation']['fields'][0]['value'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(document['association_diagnostics'][0]['code'], 'unowned_response')
        self.assertEqual(document['association_diagnostics'][0]['frame'], 2)

    def test_deadline_boundary(self):
        for t_ms, status in [(9, 'success'), (10, 'success'), (11, 'timeout')]:
            with self.subTest(t_ms=t_ms):
                document = self.view(self.capture(f'boundary-{t_ms}', [sf(0, 0x700, QUERY), sf(t_ms, 0x708, POSITIVE)]))
                operation = document['operations'][0]
                self.assertEqual(operation['status'], status)
                self.assertEqual(len(operation['responses']), int(status == 'success'))
                self.assertEqual(len(document['association_diagnostics']), int(status == 'timeout'))
        identity = self.capture('pending-boundary', [sf(0, 0x700, QUERY), sf(10, 0x708, PENDING), sf(60, 0x708, POSITIVE)])
        self.assertEqual(self.view(identity)['operations'][0]['status'], 'success')
        identity = self.capture('pending-late', [sf(0, 0x700, QUERY), sf(11, 0x708, PENDING)])
        self.assertEqual(self.view(identity)['operations'][0]['status'], 'timeout')
        self.assertEqual(self.view(identity)['operations'][0]['deadline_ms'], 10)
        identity = self.capture('end', [sf(0, 0x700, QUERY)])
        self.assertEqual(self.view(identity)['operations'][0]['completed_at_ms'], 10)

    def test_same_time_input_order(self):
        forward = self.view(self.capture('forward', [sf(0, 0x700, QUERY), sf(0, 0x708, POSITIVE)]))
        reverse = self.view(self.capture('reverse', [sf(0, 0x708, POSITIVE), sf(0, 0x700, QUERY)]))
        self.assertEqual(forward['operations'][0]['status'], 'success')
        self.assertEqual(reverse['operations'][0]['status'], 'timeout')
        self.assertEqual(reverse['association_diagnostics'][0]['frame'], 0)
        document = self.view(self.capture('same-tick-new-query', [sf(0, 0x700, QUERY),
            sf(10, 0x700, QUERY), sf(10, 0x708, POSITIVE)]))
        self.assertEqual(len(document['operations']), 1)
        self.assertEqual(document['operations'][0]['status'], 'success')
        self.assertEqual(document['association_diagnostics'][0]['code'], 'active_request')

    def test_ecu_and_did_isolation(self):
        channels = fixtures.SAMPLE['channels'] + [{'ecu': 'B', 'request_id': 0x710, 'response_id': 0x718}]
        frames = [sf(0, 0x700, QUERY), sf(1, 0x710, QUERY), sf(2, 0x700, [0x22, 0x99, 0x99]),
                  sf(3, 0x708, [0x62, 0x99, 0x99, 1]),
                  sf(4, 0x718, [0x62, 0x12, 0x34, 0, 20, 0, 0]), sf(5, 0x708, POSITIVE)]
        document = self.view(self.capture('isolation', frames, channels))
        first, second = document['operations']
        self.assertEqual([first['status'], second['status']], ['success', 'success'])
        self.assertEqual([first['request']['frame'], second['request']['frame']], [0, 1])
        self.assertEqual([first['responses'][0]['message']['frame'], second['responses'][0]['message']['frame']], [5, 4])
        self.assertEqual([first['interpretation']['fields'][0]['raw_integer'], second['interpretation']['fields'][0]['raw_integer']], [10, 20])
        self.assertEqual([row['code'] for row in document['association_diagnostics']], ['active_request', 'did_mismatch'])

    def test_malformed_services_and_no_revival(self):
        frames = [sf(0, 0x700, [0x22]), sf(1, 0x700, QUERY), sf(2, 0x708, [0x7F, 0x22]),
                  sf(3, 0x708, [0x7F, 0x10, 0x31]), sf(4, 0x708, [0x50]),
                  sf(12, 0x708, POSITIVE), sf(13, 0x700, QUERY), sf(14, 0x708, POSITIVE),
                  sf(15, 0x708, POSITIVE)]
        document = self.view(self.capture('malformed', frames))
        self.assertEqual([row['status'] for row in document['operations']], ['timeout', 'success'])
        self.assertEqual(document['operations'][0]['responses'], [])
        self.assertEqual(document['operations'][1]['responses'][0]['message']['frame'], 7)
        self.assertEqual([row['code'] for row in document['association_diagnostics']],
                         ['request_format', 'response_format', 'response_format', 'response_format',
                          'unowned_response', 'unowned_response'])

    def test_unknown_frame_advances_deadline(self):
        identity = self.capture('unknown-tick', [sf(0, 0x700, QUERY), {'t_ms': 100, 'can_id': 1, 'data': []}])
        operation = self.view(identity)['operations'][0]
        self.assertEqual((operation['status'], operation['completed_at_ms']), ('timeout', 10))


if __name__ == '__main__':
    unittest.main()
