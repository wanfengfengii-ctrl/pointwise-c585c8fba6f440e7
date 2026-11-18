import hashlib
import json


class CaptureIdentityConflict(ValueError):
    pass


def canonical_capture_input(run_id, frames, channels, timing, catalog_id, catalog_digest):
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('run_id must be a nonempty string')
    if not isinstance(catalog_id, str) or not catalog_id.strip():
        raise ValueError('catalog_id must be explicit')
    if not isinstance(catalog_digest, str) or len(catalog_digest) != 64:
        raise ValueError('invalid catalog digest')
    try:
        int(catalog_digest, 16)
    except ValueError as exc:
        raise ValueError('invalid catalog digest') from exc
    return {
        'run_id': run_id,
        'frames': [frame.to_document() for frame in frames],
        'channels': channels.to_document(),
        'timing': timing.to_document(),
        'catalog_id': catalog_id,
        'catalog_digest': catalog_digest,
    }


def capture_fingerprint(document):
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
