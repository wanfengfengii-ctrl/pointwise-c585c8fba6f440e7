from pathlib import Path
import json
import sqlite3
from diagcapture.archive.codec import decode_bundle, canonical_json, document_digest, bundle_fingerprint, BundleIntegrityError
from diagcapture.source.identity import CaptureIdentityConflict


class UnknownReportError(LookupError):
    pass


class ArchiveIntegrityError(ValueError):
    pass


class ArchiveStore:
    def __init__(self, path):
        self.path = Path(path)
        self._db = None
        self._package = self.path if self.path.is_dir() else None
        if self._package is not None:
            self._load_package()
        else:
            self._db = sqlite3.connect(str(self.path), isolation_level=None)
            self._db.execute('PRAGMA foreign_keys=ON')
            try:
                self.initialize()
            except BaseException:
                self.close()
                raise

    def connection(self):
        if self._db is None:
            raise ValueError('archive is offline or closed')
        return self._db

    def initialize(self):
        db = self.connection()
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ArchiveIntegrityError('unsupported archive schema version')
        if version == 0:
            existing = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if existing:
                raise ArchiveIntegrityError('unversioned nonempty database')
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS reports (report_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, input_digest TEXT NOT NULL, bundle_digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS bundles (report_id TEXT PRIMARY KEY REFERENCES reports(report_id), document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS sources (report_id TEXT NOT NULL REFERENCES reports(report_id), position INTEGER NOT NULL, document TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(report_id, position))')
            expected = {
                'reports': ['report_id', 'run_id', 'input_digest', 'bundle_digest'],
                'bundles': ['report_id', 'document'],
                'sources': ['report_id', 'position', 'document', 'digest'],
            }
            for table, columns in expected.items():
                if [row[1] for row in db.execute(f'PRAGMA table_info({table})')] != columns:
                    raise ArchiveIntegrityError('incompatible archive structure')
            db.execute('PRAGMA user_version=1')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def _load_package(self):
        try:
            manifest = json.loads((self._package / 'manifest.json').read_text(encoding='utf-8'))
            bundle = decode_bundle((self._package / 'bundle.json').read_text(encoding='utf-8'))
            expected = {'format_version': 1, 'report_id': bundle.document['report_id'],
                        'bundle_sha256': bundle_fingerprint(bundle)}
            if canonical_json(manifest) != canonical_json(expected):
                raise ArchiveIntegrityError('offline manifest mismatch')
            return bundle
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ArchiveIntegrityError('incomplete or damaged offline package') from exc

    def find_by_run(self, run_id):
        if self._package is not None:
            bundle = self._load_package()
            return bundle if bundle.document['run_id'] == run_id else None
        row = self.connection().execute('SELECT report_id FROM reports WHERE run_id=?', (run_id,)).fetchone()
        return self.load_bundle(row[0]) if row else None

    def publish_atomic(self, bundle):
        bundle = decode_bundle(bundle.to_document())
        document = bundle.document
        db = self.connection()
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT report_id, input_digest FROM reports WHERE run_id=?', (document['run_id'],)).fetchone()
            if row is not None:
                existing = self.load_bundle(row[0])
                if row[1] != document['input_digest'] or bundle_fingerprint(existing) != bundle_fingerprint(bundle):
                    raise CaptureIdentityConflict('run_id already binds different input')
                db.execute('COMMIT')
                return row[0]
            db.execute('INSERT INTO reports VALUES (?, ?, ?, ?)', (
                document['report_id'], document['run_id'], document['input_digest'], bundle_fingerprint(bundle)))
            db.execute('INSERT INTO bundles VALUES (?, ?)', (document['report_id'], canonical_json(document)))
            for frame in document['evidence']['frames']:
                db.execute('INSERT INTO sources VALUES (?, ?, ?, ?)', (
                    document['report_id'], frame['position'], canonical_json(frame), document_digest(frame)))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        return document['report_id']

    def load_bundle(self, report_id):
        if self._package is not None:
            bundle = self._load_package()
            if bundle.document['report_id'] != report_id:
                raise UnknownReportError(report_id)
            return bundle
        db = self.connection()
        header = db.execute('SELECT run_id, input_digest, bundle_digest FROM reports WHERE report_id=?', (report_id,)).fetchone()
        if header is None:
            raise UnknownReportError(report_id)
        row = db.execute('SELECT document FROM bundles WHERE report_id=?', (report_id,)).fetchone()
        if row is None:
            raise ArchiveIntegrityError('report bundle missing')
        try:
            bundle = decode_bundle(row[0])
            document = bundle.document
            if document['report_id'] != report_id or document['run_id'] != header[0] or document['input_digest'] != header[1] or bundle_fingerprint(bundle) != header[2]:
                raise ArchiveIntegrityError('report header mismatch')
            sources = db.execute('SELECT position, document, digest FROM sources WHERE report_id=? ORDER BY position', (report_id,)).fetchall()
            expected_frames = document['evidence']['frames']
            if len(sources) != len(expected_frames):
                raise ArchiveIntegrityError('raw source missing')
            for row_source, expected_frame in zip(sources, expected_frames):
                position, serialized, digest = row_source
                frame = json.loads(serialized)
                if position != expected_frame['position'] or canonical_json(frame) != canonical_json(expected_frame) or digest != document_digest(frame):
                    raise ArchiveIntegrityError('raw source mismatch')
            return bundle
        except (BundleIntegrityError, ValueError, TypeError, KeyError) as exc:
            raise ArchiveIntegrityError('stored report integrity failure') from exc

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
