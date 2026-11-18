import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from diagcapture.archive.store import ArchiveStore
from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.ingest import CaptureService
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify
from diagcapture.archive.export import export


def read_json_input(path):
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)


def build_parser():
    parser = argparse.ArgumentParser(prog='diagcapture')
    parser.add_argument('--db', default='workday.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    catalog = commands.add_parser('catalog')
    catalog.add_argument('--input', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('--input', required=True)
    capture.add_argument('--catalog', required=True)
    for name in ('report', 'verify', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--id', required=True)
        if name == 'report':
            command.add_argument('--ecu')
        if name == 'export':
            command.add_argument('--target', required=True)
    commands.add_parser('demo')
    return parser


def _sample_summary(document):
    operation = document['operations'][0]
    fields = operation['interpretation']['fields']
    return {
        'report_id': document['report_id'], 'operation_id': operation['id'],
        'status': operation['status'], 'temperature': fields[0]['value'],
        'voltage': fields[1]['value'], 'response_frame': operation['responses'][0]['message']['frame'],
        'field_can_ranges': [field['source']['can_range'] for field in fields],
    }


def _long_demo(folder):
    """Real capture, replay, export and offline check of the long-response samples."""
    sample = read_json_input(Path(__file__).resolve().parent.parent / 'samples' / 'long_response.json')
    database = Path(folder) / 'long.sqlite3'
    saved, summaries = {}, []
    with ArchiveStore(database) as store:
        catalog = sample['catalog']
        CatalogRegistry(store.connection()).register_catalog(catalog['id'], catalog['dids'])
        service = CaptureService(store, catalog['id'])
        for case in sample['cases']:
            identity = service.capture_isotp(case['run_id'], case['frames'],
                                             sample['channels'], sample['timing'])
            view = report(store, identity).to_document()
            transfer = view['transfers'][0]
            expected = case['expected']
            if (transfer['body'] != expected['body'] or transfer['segments'] != expected['segments']
                    or [row['status'] for row in view['operations']] != expected['statuses']
                    or [row['completed_at_ms'] for row in view['operations']] != expected['completed_at_ms']
                    or transfer['operation_id'] != expected['operation_id']):
                raise ValueError('long demo differs from hand-worked sample: ' + case['run_id'])
            if not verify(store, identity).to_document()['verified']:
                raise ValueError('long demo replay failed: ' + case['run_id'])
            export(store, identity, Path(folder) / case['run_id'])
            saved[identity] = view
    database.unlink()
    for case in sample['cases']:
        identity = 'report:' + case['run_id']
        with ArchiveStore(Path(folder) / case['run_id']) as offline:
            view = report(offline, identity).to_document()
            if view != saved[identity] or not verify(offline, identity).to_document()['verified']:
                raise ValueError('long demo offline delivery mismatch: ' + case['run_id'])
            summaries.append({'run_id': case['run_id'],
                              'statuses': [row['status'] for row in view['operations']],
                              'completed_at_ms': [row['completed_at_ms'] for row in view['operations']],
                              'transfers': view['transfers'],
                              'interpretation': [row['interpretation'] for row in view['operations']]})
    print('POINTWISE_LONG_RESPONSE ' + json.dumps(summaries, ensure_ascii=False, sort_keys=True))
    return summaries


def _demo():
    root = Path(__file__).resolve().parent.parent
    sample = read_json_input(root / 'samples' / 'workday.json')
    expected = read_json_input(root / 'samples' / 'expected.json')
    with tempfile.TemporaryDirectory(prefix='diagcapture-demo-') as folder:
        database = Path(folder) / 'workday.sqlite3'
        target = Path(folder) / 'offline'
        with ArchiveStore(database) as store:
            catalog = sample['catalog']
            CatalogRegistry(store.connection()).register_catalog(catalog['id'], catalog['dids'])
            report_id = CaptureService(store, catalog['id']).capture(
                sample['run_id'], sample['frames'], sample['channels'], sample['timing'])
            document = report(store, report_id).to_document()
            actual = _sample_summary(document)
            if actual != expected:
                raise ValueError('demo differs from independent hand calculation')
            checked = verify(store, report_id).to_document()
            export(store, report_id, target)
        database.unlink()
        with ArchiveStore(target) as offline:
            offline_document = report(offline, report_id).to_document()
            offline_checked = verify(offline, report_id).to_document()
        downstream_actual = {'verified': checked['verified'] and offline_checked['verified'],
                             'offline_equal': offline_document == document}
        downstream_expected = {'verified': True, 'offline_equal': True}
        if downstream_actual != downstream_expected:
            raise ValueError('demo offline delivery mismatch')
        _long_demo(folder)
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v1', 'input': sample['frames'], 'expected': expected, 'actual': actual,
            'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
            'code_paths': ['diagcapture/source/ingest.py', 'diagcapture/transport/single.py',
                           'diagcapture/operations/session.py', 'diagcapture/did/interpret.py',
                           'diagcapture/archive/verify.py', 'diagcapture/archive/export.py'],
            'oracle': 'FF9C is signed -100; -100/10=-10; little-endian 3930 is 12345; 12345/1000=2469/200',
        }, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'demo':
            return _demo()
        with ArchiveStore(args.db) as store:
            if args.command == 'init':
                CatalogRegistry(store.connection()).initialize()
                result = {'schema_version': 1}
            elif args.command == 'catalog':
                document = read_json_input(args.input)
                document = document.get('catalog', document)
                identity = CatalogRegistry(store.connection()).register_catalog(document['id'], document['dids'])
                result = {'catalog_id': identity}
            elif args.command == 'capture':
                document = read_json_input(args.input)
                identity = CaptureService(store, args.catalog).capture(
                    document['run_id'], document['frames'], document['channels'], document['timing'])
                result = {'report_id': identity}
            elif args.command == 'report':
                result = report(store, args.id, args.ecu).to_document()
            elif args.command == 'verify':
                result = verify(store, args.id).to_document()
            else:
                result = {'target': export(store, args.id, args.target)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, LookupError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
