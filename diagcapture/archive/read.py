from dataclasses import dataclass
from copy import deepcopy


@dataclass(frozen=True)
class ReportView:
    document: dict

    @property
    def report_id(self):
        return self.document['report_id']

    def __getitem__(self, key):
        return deepcopy(self.document[key])

    def to_document(self):
        return deepcopy(self.document)


def report(store, report_id, ecu=None):
    if ecu is not None and (not isinstance(ecu, str) or not ecu.strip()):
        raise ValueError('ECU filter must be a nonempty string')
    bundle = store.load_bundle(report_id).to_document()
    result = bundle['result']
    document = {
        'report_id': bundle['report_id'], 'run_id': bundle['run_id'],
        'input_digest': bundle['input_digest'], 'catalog_id': bundle['catalog']['id'],
        'frames': bundle['evidence']['frames'], 'channels': bundle['evidence']['channels'],
        'timing': bundle['evidence']['timing'], **result,
    }
    if ecu is not None:
        for key in ('operations', 'messages', 'protocol_diagnostics', 'association_diagnostics',
                    'transfers'):
            if key in document:
                document[key] = [row for row in document[key] if row['ecu'] == ecu]
    return ReportView(document)
