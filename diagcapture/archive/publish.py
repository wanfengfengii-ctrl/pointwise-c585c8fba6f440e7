from diagcapture.source.evidence import validate_evidence
from diagcapture.operations.replay import replay_capture
from diagcapture.archive.codec import encode_bundle
from diagcapture.source.identity import CaptureIdentityConflict


def publish_report(store, evidence, catalog):
    evidence = validate_evidence(evidence.to_document())
    existing = store.find_by_run(evidence.run_id)
    if existing is not None:
        if existing.document['input_digest'] != evidence.input_digest:
            raise CaptureIdentityConflict('run_id already binds different input')
        return existing.document['report_id']
    result = replay_capture(evidence, catalog)
    bundle = encode_bundle(evidence, catalog, result)
    return store.publish_atomic(bundle)
