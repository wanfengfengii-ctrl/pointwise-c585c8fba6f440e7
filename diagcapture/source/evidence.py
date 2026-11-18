from dataclasses import dataclass
from diagcapture.source.frames import normalize_frames
from diagcapture.source.channels import validate_channels
from diagcapture.source.identity import canonical_capture_input, capture_fingerprint
from diagcapture.operations.timing import validate_timing


@dataclass(frozen=True)
class CaptureEvidence:
    run_id: str
    frames: tuple
    channels: object
    timing: object
    catalog_id: str
    catalog_digest: str
    input_digest: str

    def to_document(self):
        result = canonical_capture_input(self.run_id, self.frames, self.channels, self.timing,
                                         self.catalog_id, self.catalog_digest)
        result['input_digest'] = self.input_digest
        return result


def freeze_evidence(run_id, frames, channels, timing, catalog_id, catalog_digest):
    normalized_frames = normalize_frames(frames)
    normalized_channels = validate_channels(channels)
    normalized_timing = validate_timing(timing)
    document = canonical_capture_input(run_id, normalized_frames, normalized_channels,
                                       normalized_timing, catalog_id, catalog_digest)
    return CaptureEvidence(run_id, normalized_frames, normalized_channels, normalized_timing,
                           catalog_id, catalog_digest, capture_fingerprint(document))


def validate_evidence(document):
    if not isinstance(document, dict) or set(document) != {
        'run_id', 'frames', 'channels', 'timing', 'catalog_id', 'catalog_digest', 'input_digest'
    }:
        raise ValueError('incomplete evidence')
    rows = document['frames']
    if not isinstance(rows, list):
        raise ValueError('evidence frames must be an array')
    frames = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {'position', 't_ms', 'can_id', 'data'}:
            raise ValueError('invalid frame evidence')
        if type(row['position']) is not int or row['position'] != position:
            raise ValueError('missing or reordered frame position')
        frames.append({key: row[key] for key in ('t_ms', 'can_id', 'data')})
    evidence = freeze_evidence(document['run_id'], frames, document['channels'], document['timing'],
                               document['catalog_id'], document['catalog_digest'])
    if evidence.input_digest != document['input_digest']:
        raise ValueError('evidence input digest mismatch')
    if evidence.to_document() != document:
        raise ValueError('noncanonical evidence')
    return evidence
