from dataclasses import dataclass
import sqlite3
from diagcapture.source.evidence import validate_evidence
from diagcapture.did.catalog import catalog_from_document
from diagcapture.operations.replay import replay_capture
from diagcapture.archive.codec import canonical_json, bundle_fingerprint


class VerificationFailure(ValueError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    report_id: str
    input_digest: str
    bundle_digest: str
    operation_count: int

    def to_document(self):
        return {'report_id': self.report_id, 'input_digest': self.input_digest,
                'bundle_digest': self.bundle_digest, 'operation_count': self.operation_count,
                'verified': True}


def verify(store, report_id):
    try:
        bundle = store.load_bundle(report_id)
        document = bundle.to_document()
        evidence = validate_evidence(document['evidence'])
        catalog = catalog_from_document(document['catalog'])
        replayed = replay_capture(evidence, catalog).to_document()
        if canonical_json(replayed) != canonical_json(document['result']):
            raise VerificationFailure('raw replay differs from fixed report')
        if document['input_digest'] != evidence.input_digest:
            raise VerificationFailure('raw replay input identity differs')
        return VerificationResult(report_id, evidence.input_digest, bundle_fingerprint(bundle),
                                  len(replayed['operations']))
    except VerificationFailure:
        raise
    except (ValueError, LookupError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        raise VerificationFailure(f'cannot verify report: {exc}') from exc
