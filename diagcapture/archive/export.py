from pathlib import Path
import hashlib
import os
import shutil
import tempfile
from diagcapture.archive.codec import canonical_json, bundle_fingerprint
from diagcapture.archive.verify import verify, VerificationFailure


class ExportConflict(FileExistsError):
    pass


def export(store, report_id, target):
    target = Path(target)
    if not target.name or not target.parent.is_dir():
        raise ValueError('export parent must exist')
    if os.path.lexists(target):
        raise ExportConflict(str(target))
    lock_name = '.diagcapture-lock-' + hashlib.sha256(target.name.encode('utf-8')).hexdigest()
    lock = target.parent / lock_name
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ExportConflict('another export owns this destination') from exc
    temporary = None
    try:
        if os.path.lexists(target):
            raise ExportConflict(str(target))
        verification = verify(store, report_id)
        bundle = store.load_bundle(report_id)
        digest = bundle_fingerprint(bundle)
        if digest != verification.bundle_digest:
            raise VerificationFailure('report changed while preparing export')
        temporary = Path(tempfile.mkdtemp(prefix='.diagcapture-', dir=target.parent))
        manifest = {'format_version': 1, 'report_id': report_id, 'bundle_sha256': digest}
        for name, document in (('bundle.json', bundle.to_document()), ('manifest.json', manifest)):
            with (temporary / name).open('x', encoding='utf-8') as handle:
                handle.write(canonical_json(document) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        if os.path.lexists(target):
            raise ExportConflict(str(target))
        os.rename(temporary, target)
        temporary = None
        return str(target)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        lock.rmdir()
