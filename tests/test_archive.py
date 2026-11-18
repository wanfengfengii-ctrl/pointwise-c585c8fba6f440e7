from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import tests.test_capture as fixtures
from diagcapture.archive.store import ArchiveStore, ArchiveIntegrityError, UnknownReportError
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify, VerificationFailure
from diagcapture.archive.export import export, ExportConflict
from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.ingest import CaptureService
from diagcapture.source.identity import CaptureIdentityConflict


def serialized(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def checksum(document):
    return hashlib.sha256(serialized(document).encode('utf-8')).hexdigest()


class TestArchive(fixtures.ArchiveCase):
    def reopen(self):
        self.store.close()
        self.store = ArchiveStore(self.db_path)
        self.registry = CatalogRegistry(self.store.connection())

    def test_idempotence_and_catalog_binding(self):
        identity = self.capture()
        before = self.view(identity)
        self.assertEqual(self.capture(), identity)
        revised = deepcopy(fixtures.SAMPLE['catalog']['dids'])
        revised[0]['fields'][0]['scale'] = [1, 100]
        self.registry.register_catalog('lab-v2', revised)
        self.reopen()
        self.store.initialize()
        self.registry.initialize()
        self.assertEqual(self.view(identity), before)
        self.assertTrue(verify(self.store, identity).to_document()['verified'])
        with self.assertRaises(CaptureIdentityConflict):
            self.capture(catalog_id='lab-v2')
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM reports').fetchone()[0], 1)
        self.assertEqual(self.registry.load_catalog('lab-v2').catalog_id, 'lab-v2')

    def test_input_identity(self):
        identity = self.capture()
        before = self.view(identity)
        variants = []
        frames = deepcopy(fixtures.SAMPLE['frames'])
        frames[1]['data'][4] = 0
        variants.append({'frames': frames})
        frames = deepcopy(fixtures.SAMPLE['frames'])
        frames[0]['data'].append(0xAA)
        variants.append({'frames': frames})
        frames = deepcopy(fixtures.SAMPLE['frames'])
        frames[0]['t_ms'] = frames[1]['t_ms'] = 0
        variants.append({'frames': list(reversed(frames))})
        channels = deepcopy(fixtures.SAMPLE['channels'])
        channels[0]['response_id'] += 1
        variants.append({'channels': channels})
        for key in fixtures.SAMPLE['timing']:
            timing = deepcopy(fixtures.SAMPLE['timing'])
            timing[key] += 1
            variants.append({'timing': timing})
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(CaptureIdentityConflict):
                self.capture(**variant)
        self.assertEqual(self.view(identity), before)
        self.assertEqual(self.capture(), identity)
        self.assertEqual(self.view(identity)['timing']['P2'], 10)
        timing = deepcopy(fixtures.SAMPLE['timing'])
        timing['P2'] = 5
        with self.assertRaises(CaptureIdentityConflict):
            self.capture(timing=timing)

    def test_atomic_publication(self):
        old = self.capture()
        old_document = self.view(old)
        db = self.store.connection()
        db.execute("CREATE TRIGGER abort_source BEFORE INSERT ON sources WHEN NEW.report_id='report:fail' BEGIN SELECT RAISE(ABORT, 'controlled write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.capture('fail')
        self.reopen()
        self.assertIsNone(self.store.find_by_run('fail'))
        with self.assertRaises(UnknownReportError):
            report(self.store, 'report:fail')
        self.assertEqual(self.view(old), old_document)
        self.assertTrue(verify(self.store, old).to_document()['verified'])
        self.store.connection().execute('DROP TRIGGER abort_source')
        retried = self.capture('fail')
        self.assertEqual(retried, 'report:fail')
        self.assertEqual(self.capture('fail'), retried)
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM reports').fetchone()[0], 2)

    def test_modified_raw_frame_fails_verify(self):
        identity = self.capture()
        db = self.store.connection()
        row = db.execute('SELECT document FROM sources WHERE report_id=? AND position=1', (identity,)).fetchone()
        frame = json.loads(row[0])
        frame['data'][4] = 0
        db.execute('UPDATE sources SET document=?, digest=? WHERE report_id=? AND position=1',
                   (serialized(frame), checksum(frame), identity))
        with self.assertRaises(VerificationFailure):
            verify(self.store, identity)
        with self.assertRaises(ArchiveIntegrityError):
            report(self.store, identity)

    def _replace_document(self, document, refresh=True):
        if refresh:
            evidence = document['evidence']
            evidence['input_digest'] = checksum({key: value for key, value in evidence.items() if key != 'input_digest'})
            document['input_digest'] = evidence['input_digest']
            document['manifest'] = {name + '_sha256': checksum(document[name]) for name in ('evidence', 'catalog', 'result')}
        identity = document['report_id']
        db = self.store.connection()
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('UPDATE reports SET input_digest=?, bundle_digest=? WHERE report_id=?',
                       (document['input_digest'], checksum(document), identity))
            db.execute('UPDATE bundles SET document=? WHERE report_id=?', (serialized(document), identity))
            db.execute('DELETE FROM sources WHERE report_id=?', (identity,))
            for frame in document['evidence']['frames']:
                db.execute('INSERT INTO sources VALUES (?, ?, ?, ?)',
                           (identity, frame['position'], serialized(frame), checksum(frame)))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def test_forged_digests_still_require_raw_replay(self):
        identity = self.capture()
        document = self.store.load_bundle(identity).to_document()
        document['evidence']['frames'][1]['data'][4] = 0
        document['result']['messages'][1]['body'][3] = 0
        response = document['result']['operations'][0]['responses'][0]
        response['message']['body'][3] = 0
        response['data'][0] = 0
        field = document['result']['operations'][0]['interpretation']['fields'][0]
        field['raw_bytes'][0] = 0
        self._replace_document(document)
        self.assertEqual(self.view(identity)['operations'][0]['interpretation']['fields'][0]['raw_integer'], -100)
        with self.assertRaisesRegex(VerificationFailure, 'raw replay differs'):
            verify(self.store, identity)

    def test_result_and_source_integrity(self):
        for index, mode in enumerate(('configuration', 'snapshot', 'result', 'missing_source', 'missing_bundle')):
            with self.subTest(mode=mode):
                identity = self.capture(f'damage-{index}')
                document = self.store.load_bundle(identity).to_document()
                db = self.store.connection()
                if mode == 'configuration':
                    document['evidence']['timing']['P2'] = 5
                    self._replace_document(document, refresh=False)
                elif mode == 'snapshot':
                    document['catalog']['dids'][0]['fields'][0]['scale'] = [1, 100]
                    self._replace_document(document, refresh=False)
                elif mode == 'result':
                    document['result']['operations'][0]['interpretation']['fields'][0]['value']['numerator'] = 99
                    self._replace_document(document, refresh=False)
                elif mode == 'missing_source':
                    db.execute('DELETE FROM sources WHERE report_id=? AND position=1', (identity,))
                else:
                    db.execute('DELETE FROM bundles WHERE report_id=?', (identity,))
                with self.assertRaises(ArchiveIntegrityError):
                    report(self.store, identity)
                with self.assertRaises(VerificationFailure):
                    verify(self.store, identity)

    def test_registry_modification_is_not_silently_repaired(self):
        identity = self.capture()
        before = self.view(identity)
        changed = deepcopy(fixtures.SAMPLE['catalog'])
        changed['dids'][0]['fields'][0]['scale'] = [1, 100]
        self.store.connection().execute('UPDATE catalogs SET document=? WHERE id=?', (serialized(changed), 'lab-v1'))
        self.reopen()
        with self.assertRaises(ValueError):
            self.registry.load_catalog('lab-v1')
        with self.assertRaises(ValueError):
            self.registry.register_catalog('lab-v1', fixtures.SAMPLE['catalog']['dids'])
        self.assertEqual(self.view(identity), before)
        self.assertTrue(verify(self.store, identity).to_document()['verified'])

    def test_unsupported_schema_refused(self):
        other = Path(self.folder.name) / 'future.sqlite3'
        with sqlite3.connect(other) as db:
            db.execute('PRAGMA user_version=9')
            db.execute('CREATE TABLE preserved(value TEXT)')
            db.execute("INSERT INTO preserved VALUES ('user-data')")
        with self.assertRaises(ArchiveIntegrityError):
            ArchiveStore(other)
        with sqlite3.connect(other) as db:
            self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0], 'user-data')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)

    def test_offline_export_and_existing_target(self):
        identity = self.capture()
        before = self.view(identity)
        changed = deepcopy(fixtures.SAMPLE['catalog']['dids'])
        changed[0]['fields'][0]['scale'] = [1, 100]
        self.registry.register_catalog('lab-v2', changed)
        target = Path(self.folder.name) / 'offline'
        export(self.store, identity, target)
        saved = {path.name: path.read_bytes() for path in target.iterdir()}
        with self.assertRaises(ExportConflict):
            export(self.store, identity, target)
        self.assertEqual(saved, {path.name: path.read_bytes() for path in target.iterdir()})
        self.store.close()
        self.db_path.unlink()
        self.store = ArchiveStore(target)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.view(identity), before)
        self.assertEqual(report(self.store, identity, 'A')['operations'], before['operations'])
        self.assertTrue(verify(self.store, identity).to_document()['verified'])
        self.assertEqual(self.store.load_bundle(identity).document['catalog']['id'], 'lab-v1')
        with self.assertRaises(ValueError):
            self.store.connection()

    def test_export_cleanup(self):
        identity = self.capture()
        parent = Path(self.folder.name)
        existing_file = parent / 'existing-file'
        existing_file.write_bytes(b'keep')
        existing_directory = parent / 'existing-directory'
        existing_directory.mkdir()
        for target in (existing_file, existing_directory):
            with self.assertRaises(ExportConflict):
                export(self.store, identity, target)
        self.assertEqual(existing_file.read_bytes(), b'keep')
        self.assertEqual(list(existing_directory.iterdir()), [])
        before = sorted(path.name for path in parent.iterdir())
        with self.assertRaises(OSError):
            export(self.store, identity, parent / ('x' * 300))
        self.assertEqual(sorted(path.name for path in parent.iterdir()), before)
        self.assertFalse(any(path.name.startswith('.diagcapture-') for path in parent.iterdir()))

    def test_cli_verify_failure(self):
        identity = self.capture()
        self.store.connection().execute('DELETE FROM sources WHERE report_id=? AND position=1', (identity,))
        completed = subprocess.run([sys.executable, '-B', '-m', 'diagcapture', '--db', str(self.db_path),
                                    'verify', '--id', identity], cwd=fixtures.ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn('error', json.loads(completed.stderr))


if __name__ == '__main__':
    unittest.main()
