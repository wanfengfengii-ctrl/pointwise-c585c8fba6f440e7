import unittest
import tests.test_capture as fixtures
from diagcapture.source.frames import normalize_frames
from diagcapture.source.channels import validate_channels
from diagcapture.transport.single import decode_single_frame
from diagcapture.archive.verify import verify
from tools.can_fixture import sf, ff, cf, fc, VirtualClock


class TestSingleTransport(fixtures.ArchiveCase):
    def test_bad_length_is_evidence(self):
        frames = [sf(0, 0x700, [0x22, 0x12, 0x34]),
                  {'t_ms': 1, 'can_id': 0x708, 'data': [7, 0x62, 0x12, 0x34, 1]}]
        identity = self.capture('bad-length', frames)
        document = self.view(identity)
        self.assertEqual(document['operations'][0]['status'], 'timeout')
        self.assertEqual(document['operations'][0]['responses'], [])
        self.assertEqual(document['protocol_diagnostics'][0]['code'], 'bad_single_length')
        self.assertEqual(document['protocol_diagnostics'][0]['frame'], 1)
        self.assertEqual(len(document['messages']), 1)
        self.assertEqual(document['frames'][1]['data'], frames[1]['data'])
        self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_padding_and_unsupported(self):
        body = [0x62, 0x12, 0x34, 0, 1]
        frames = [sf(0, 0x708, body, [0, 0]), sf(0, 0x708, body, [0xAA, 0xAA])]
        channels = validate_channels(fixtures.SAMPLE['channels'])
        refs = normalize_frames(frames)
        messages = [decode_single_frame(ref, channels).message.to_document() for ref in refs]
        self.assertEqual(messages[0]['body'], body)
        self.assertEqual(messages[1]['body'], body)
        self.assertEqual([message['body_range'] for message in messages], [[1, 6], [1, 6]])
        self.assertNotEqual(refs[0].frame.data, refs[1].frame.data)
        clock = VirtualClock(0)
        unsupported = [
            {'t_ms': clock.now, 'can_id': 0x708, 'data': []},
            {'t_ms': clock.step(1), 'can_id': 0x708, 'data': [0]},
            ff(clock.step(1), 0x708, 12, [0x62, 0x12, 0x34, 1, 2, 3]),
            cf(clock.step(1), 0x708, 1, [4, 5, 6]),
            fc(clock.step(1), 0x700, 0, 1),
        ]
        identity = self.capture('unsupported', unsupported)
        document = self.view(identity)
        self.assertEqual(document['messages'], [])
        self.assertEqual(document['operations'], [])
        self.assertEqual([row['frame'] for row in document['protocol_diagnostics']], list(range(5)))
        self.assertEqual([row['code'] for row in document['protocol_diagnostics']],
                         ['empty_data', 'bad_single_length', 'unsupported_pci', 'unsupported_pci', 'unsupported_pci'])
        self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_unregistered_frames_are_retained_only(self):
        frames = [{'t_ms': 0, 'can_id': 0x100, 'data': []},
                  {'t_ms': 0, 'can_id': 0x101, 'data': [0xFF]}]
        document = self.view(self.capture('unknown', frames))
        self.assertEqual(len(document['frames']), 2)
        self.assertEqual(document['messages'], [])
        self.assertEqual(document['protocol_diagnostics'], [])

    def test_zero_and_excess_single_lengths(self):
        channels = validate_channels(fixtures.SAMPLE['channels'])
        for data in ([0], [8, 1, 2, 3, 4, 5, 6, 7], [7, 1], [15, 1]):
            with self.subTest(data=data):
                ref = normalize_frames([{'t_ms': 0, 'can_id': 0x708, 'data': data}])[0]
                result = decode_single_frame(ref, channels)
                self.assertIsNone(result.message)
                self.assertEqual(result.diagnostic.code, 'bad_single_length')


if __name__ == '__main__':
    unittest.main()
