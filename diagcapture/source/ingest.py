from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.evidence import freeze_evidence
from diagcapture.archive.publish import publish_report


class CaptureRejected(ValueError):
    pass


class CaptureService:
    def __init__(self, store, catalog_id):
        if not isinstance(catalog_id, str) or not catalog_id.strip():
            raise CaptureRejected('an explicit catalog identity is required')
        self.store = store
        self.catalog_id = catalog_id

    def _capture(self, profile, run_id, frames, channels, timing):
        catalog = CatalogRegistry(self.store.connection()).load_catalog(self.catalog_id)
        try:
            evidence = freeze_evidence(run_id, frames, channels, timing,
                                       catalog.catalog_id, catalog.digest, profile)
        except (TypeError, ValueError) as exc:
            raise CaptureRejected(str(exc)) from exc
        return publish_report(self.store, evidence, catalog)

    def capture(self, run_id, frames, channels, timing):
        """V1 entry point: single-frame ISO-TP only."""
        return self._capture('v1', run_id, frames, channels, timing)

    def capture_isotp(self, run_id, frames, channels, timing):
        """Finite classic ISO-TP entry point: single frames and long responses."""
        return self._capture('isotp', run_id, frames, channels, timing)
