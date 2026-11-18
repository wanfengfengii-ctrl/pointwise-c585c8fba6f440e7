from dataclasses import dataclass
import hashlib
import json
from diagcapture.did.layout import validate_layout, LayoutValidationError


def _canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class Catalog:
    catalog_id: str
    layouts: tuple

    @property
    def digest(self):
        return catalog_fingerprint(self)

    def find(self, did):
        return next((layout for layout in self.layouts if layout.did == did), None)


def catalog_to_document(catalog):
    return {'id': catalog.catalog_id, 'dids': [layout.to_document() for layout in catalog.layouts]}


def catalog_from_document(document):
    if not isinstance(document, dict) or set(document) != {'id', 'dids'}:
        raise LayoutValidationError('catalog requires id and dids')
    identity = document['id']
    if not isinstance(identity, str) or not identity.strip() or not isinstance(document['dids'], list):
        raise LayoutValidationError('invalid catalog identity or DID array')
    layouts = [validate_layout(row) for row in document['dids']]
    if len({layout.did for layout in layouts}) != len(layouts):
        raise LayoutValidationError('duplicate DID')
    return Catalog(identity, tuple(sorted(layouts, key=lambda layout: layout.did)))


def catalog_fingerprint(catalog):
    document = catalog_to_document(catalog) if isinstance(catalog, Catalog) else catalog
    return hashlib.sha256(_canonical(document).encode('utf-8')).hexdigest()


class CatalogRegistry:
    def __init__(self, connection):
        self.db = connection
        self.initialize()

    def initialize(self):
        if self.db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('unsupported catalog database version')
        self.db.execute('CREATE TABLE IF NOT EXISTS catalogs (id TEXT PRIMARY KEY, document TEXT NOT NULL, digest TEXT NOT NULL)')
        columns = [row[1] for row in self.db.execute('PRAGMA table_info(catalogs)')]
        if columns != ['id', 'document', 'digest']:
            raise ValueError('incompatible catalog structure')

    def register_catalog(self, catalog_id, layouts):
        catalog = catalog_from_document({'id': catalog_id, 'dids': layouts})
        document = _canonical(catalog_to_document(catalog))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT document, digest FROM catalogs WHERE id=?', (catalog_id,)).fetchone()
            if row is not None:
                saved = catalog_from_document(json.loads(row[0]))
                if _canonical(catalog_to_document(saved)) != row[0] or saved.digest != row[1]:
                    raise ValueError('stored catalog integrity failure')
                if row[0] != document or row[1] != catalog.digest:
                    raise ValueError('catalog identity conflict')
            else:
                self.db.execute('INSERT INTO catalogs VALUES (?, ?, ?)', (catalog_id, document, catalog.digest))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return catalog.catalog_id

    def load_catalog(self, catalog_id):
        row = self.db.execute('SELECT document, digest FROM catalogs WHERE id=?', (catalog_id,)).fetchone()
        if row is None:
            raise ValueError(f'unknown catalog: {catalog_id}')
        try:
            catalog = catalog_from_document(json.loads(row[0]))
            if catalog.catalog_id != catalog_id or catalog.digest != row[1] or _canonical(catalog_to_document(catalog)) != row[0]:
                raise ValueError('stored catalog integrity failure')
            return catalog
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('stored catalog integrity failure') from exc
