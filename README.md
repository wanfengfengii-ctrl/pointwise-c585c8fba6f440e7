# Diagnostic capture lab

Python 3.11 standard-library software for a vehicle environmental test laboratory. It passively processes finite classic CAN recordings; it neither connects to a vehicle nor sends commands.

A recorder registers an immutable DID catalog and imports a workday recording with its ECU channel table. The program validates the complete input, preserves frame positions, extracts supported single-frame ISO-TP messages, associates UDS operations, calculates exact DID values, and publishes a fixed SQLite report. Analysts read values and separate protocol, association, interpretation, negative-response and timeout conclusions. Reviewers replay saved evidence. An offline recipient opens an exported directory without the original database.

## Run

Run from this repository root. No installation or dependencies are needed. The packages use Python namespace subpackages.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m diagcapture demo
```

The demo uses a temporary directory, calculates the supplied hand-worked sample, verifies it, exports it, removes the original database, and verifies the offline result. It emits a `POINTWISE_SCENARIO` JSON line only after its actual assertions succeed. Tests and demo require a writable temporary directory.

`RUNNING.json` contains argument arrays for the complete daily workflow. Its initialize, register, capture, report, verify and export commands are intended to run in that order. Initialization and identical registration/import can be repeated. Export requires an absent destination. Existing reports and catalogs are never reset by initialization. The CLI returns 2 for input, storage and verification errors; legal recordings containing protocol errors can still publish successfully.

## API

```python
from diagcapture.archive.store import ArchiveStore
from diagcapture.did.catalog import CatalogRegistry
from diagcapture.source.ingest import CaptureService
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify
from diagcapture.archive.export import export

with ArchiveStore('workday.sqlite3') as store:
    registry = CatalogRegistry(store.connection())
    registry.register_catalog(catalog_document['id'], catalog_document['dids'])
    report_id = CaptureService(store, catalog_document['id']).capture(
        run_id, frames, channels, timing)
    fixed = report(store, report_id).to_document()
    checked = verify(store, report_id).to_document()
    export(store, report_id, 'previously-absent-directory')

with ArchiveStore('previously-absent-directory') as offline:
    same_report = report(offline, report_id).to_document()
    checked_again = verify(offline, report_id).to_document()
```

`ReportView` supports `to_document()` and keyed reads. Returned documents are copies. `report(..., ecu='A')` filters operations, messages and diagnostics while preserving report identity, relative order and the complete raw-frame evidence. Offline archives are read-only. Unknown reports raise `UnknownReportError`; damaged stored material raises `ArchiveIntegrityError`; failed replay raises `VerificationFailure`. Identity reuse with changed input raises `CaptureIdentityConflict`. Invalid capture structures raise `CaptureRejected`.

## Material formats and rules

`samples/workday.json` is the finite input example. Each frame has integer `t_ms`, integer `can_id`, and a byte array `data` of length 0–8. Boolean integers, nulls, extra or missing frame members, invalid byte values and decreasing times are rejected. CAN IDs range from 0 through 0x1FFFFFFF. Equal timestamps retain input order. Frames receive zero-based `position` values. Unknown CAN IDs remain evidence and produce no guessed messages.

Channels are an array of `{ecu, request_id, response_id}`; ECU names and every registered CAN ID must be unique. Channel ordering is canonicalized by ECU name; frame ordering is never canonicalized. Timing requires positive integer `P2`, `P2star`, `N_Bs` and `N_Cr` in milliseconds. All four participate in input identity. V1 uses P2 and P2star only.

V1 supports ordinary-addressing classic CAN ISO-TP single frames. PCI type must be zero, and declared length must be 1–7 and fit the available bytes. Only declared bytes become a message. Padding remains in raw evidence and participates in identity. Empty data, invalid length and unsupported PCI types create source-linked protocol diagnostics. Unsupported types are not assembled.

A query is exactly `22 DID-high DID-low`. A positive response is `62 DID-high DID-low data...`; a negative response is exactly `7F 22 NRC`. Each ECU has at most one unfinished query. A second query records an association error and cannot replace it. Before each raw frame, only deadlines strictly earlier than its timestamp expire. Thus a response exactly at its deadline is eligible; equal-time frame order remains significant. Complete NRC 78 responses append to the same operation and set the deadline to their own reception time plus P2star. A matching positive response succeeds, another NRC terminates negative, and an uncompleted query times out. Input end settles remaining operations at their logical deadlines without real time or sleeping. Late single frames remain unowned and do not revive an operation.

Catalogs contain `id` and `dids`. Each DID has integer `did`, expected data `length` and `fields`. Each field has `name`, byte `offset`, `width`, `byteorder` (`big` or `little`), boolean `signed`, rational `scale`, rational `bias`, and text `unit`. Rational parameters are `[numerator, denominator]`; zero denominators are rejected. Field gaps are permitted; overlap, duplicate names/DIDs and out-of-range spans are rejected. Catalog content is normalized and immutable under its identity. New calibration requires a new identity and explicit selection for new captures.

A field is computed as `Fraction(raw_integer) * scale + bias`, with the integer interpreted according to byte order and two's-complement signedness. Output contains raw bytes, raw integer, reduced numerator/denominator, unit and source ranges. `body_range` and `can_range` are zero-based half-open ranges; the CAN range includes the one-byte PCI offset. Unknown DIDs or wrong data lengths produce interpretation diagnostics with no partial fields. A correctly associated positive response remains success even if its data cannot be interpreted.

The workday sample has FF9C = -100, giving -10 degC. Little-endian 3930 = 12345, giving 2469/200 V. `samples/expected.json` records this independent hand calculation. Regression coverage also includes signed limits and the exact offset example 7/30.

## Fixed evidence and delivery

The run identity covers all raw bytes, frame order, channels, timing and the bound catalog identity/digest. Same run and same input return the same report identity; changed input conflicts. Operation IDs derive from run identity and request frame position. Empty input publishes a zero-operation report.

Publication uses one SQLite transaction for report identity, fixed bundle and raw sources. Failed writes roll back together, allowing retry. Supported initialization creates missing structures, checks compatibility and preserves user data. Unsupported schema versions and modified catalog identities are rejected rather than repaired.

A fixed bundle contains its catalog snapshot. Later registrations cannot change old reports. Reading checks bundle, headers, sources and cross references. Verification additionally recalculates transport, association, deadlines and exact fields from saved evidence and compares the entire semantic result. Digests detect corruption; they are not signatures against an attacker replacing every input and result coherently.

Export produces `bundle.json` and `manifest.json` in a self-contained directory. It uses a destination lock for cooperating exporters and prepares files in a dedicated temporary sibling directory before rename. Existing destinations are rejected, including empty directories and symbolic links. Preparation errors clean this invocation's temporary directory and lock. Export is an application-level publication operation; the program does not coordinate with unrelated processes manually renaming destination paths.

The four regression modules cover daily import, computation, fixed publication and offline delivery, including a real SQLite abort trigger for rollback, modified evidence with forged local digests, deleted sources, incompatible versions, catalog drift, failed CLI invocations and export cleanup. `tools/can_fixture.py` only encodes independent CAN bytes and virtual integer times; it does not implement message assembly or operation association.

## Long responses (finite ISO-TP)

`CaptureService.capture_isotp(run_id, frames, channels, timing)` is the second fixed profile.
It uses the same constructor, argument shapes, exception types and report identity namespace as
`capture`; `capture` and `decode_single_frame` keep their V1 behaviour. `CaptureService.capture`
cannot be used to re-import a run captured through `capture_isotp` (and the reverse) because the
capture profile is part of the input identity; such reuse raises `CaptureIdentityConflict`.

The new entry accepts the V1 single frames and finite classic ISO-TP long responses:

- A response first frame must hold exactly eight CAN bytes; the declared length is 12 bits, 8 to
  4095, and the following six bytes are the body prefix.
- A legal first frame always starts a transfer, replacing any in-flight transfer on the same ECU
  (`replaced`); the old transfer keeps its own owner.
- While waiting for flow control, a request-side frame `30|status block stmin` with zero STmin is
  accepted: CTS starts the consecutive-frame window with that block size (zero means unbounded),
  WAIT refreshes N_Bs without a limit, and OVERFLOW or a malformed frame fails the transfer.
- Consecutive frames use a 1-based mod-16 sequence; every non-final one must carry exactly seven
  payload bytes, the final one may be short and its surplus is padding. A block that runs out after
  a non-final frame waits for a new flow-control frame under N_Bs; a final frame never waits.
- Deadlines settle before a frame only when strictly earlier than its timestamp, and the end of
  input settles them at their logical time without sleeping. Timeouts are `N_Bs_timeout` /
  `N_Cr_timeout` with `end_frame` null.

Ownership is fixed when the first frame is processed: the first three body bytes must be
`62 DID-high DID-low` and a matching unfinished operation must exist. The reason `response_format`,
`no_active_operation` or `did_mismatch` is recorded when no binding happened. A first frame never
extends P2/P2star; a transfer that completes after its operation timed out stays in that
operation's `transfer_ids` without reviving it, and a late body never attaches to a newer query.
Complete three-byte `7F 22 78` single frames still extend the operation as in V1.

The result adds `transfers` (sorted by first-frame position) with identity, ECU, start/end frame
and time, declared length, status (`complete`/`failed`), reason, bound operation and binding
reason, consumed frame list, body and body provenance `segments`. A failed transfer has
`body: null`; consumed prefixes remain only as evidence. `operations[].transfer_ids` lists every
transfer ever bound to that operation. Complete long messages join `messages` with
`transfer_id`/`segments` and are ordered by completion position together with single frames. A
field that crosses frames reports `source = {body_range, segments}` with the per-frame provenance
clipped to the field; single-frame fields keep the V1 source shape. ECU filtering also filters
`transfers`.

The profile takes part in the fixed input identity and bundle, so `verify`, reopening, ECU
filtering and offline export re-derive transfers, ownership and fields from the saved raw frames
under the same profile. A V1 report is never reinterpreted by the long-response rules. Invalid
raw structure or configuration is rejected before publication and cannot disturb an existing
report.

`python3 -B -m tools.long_scenario --check-materials` checks only the sample material.
`python3 -B -m tools.long_scenario` performs the real capture, reopen, verify and offline delivery
for the supplied timely/late cases. `python3 -B -m tools.verify_all` runs all regressions and that
scenario, and is the manifest's verify entry.

### Transfer/operation state handoff

| Event | Transfer | Operation |
| --- | --- | --- |
| Matching first frame | created, waits FC (N_Bs) | unchanged; `transfer_ids` records it |
| CTS / WAIT | waits CF (N_Cr) / waits FC (N_Bs) | unchanged |
| Final consecutive frame | `complete`, joins `messages` | `success` at the completion time if still active |
| Timeout, malformed, sequence, overflow, replaced, interrupted | `failed` with a reason | unchanged, except complete 78 single frames which extend it |
| Operation times out or ends first | transfer continues to its own conclusion | terminal state fixed; later body stays evidence only |
