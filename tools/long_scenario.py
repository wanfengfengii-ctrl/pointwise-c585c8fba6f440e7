"""Public business checks; contains no transport or ownership implementation."""
import argparse
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import tempfile

from diagcapture.archive.store import ArchiveStore
from diagcapture.archive.read import report
from diagcapture.archive.verify import verify
from diagcapture.archive.export import export
from diagcapture.did.catalog import CatalogRegistry, catalog_from_document
from diagcapture.source.frames import normalize_frames
from diagcapture.source.channels import validate_channels
from diagcapture.source.identity import CaptureIdentityConflict
from diagcapture.source.ingest import CaptureService
from diagcapture.operations.timing import validate_timing

ROOT = Path(__file__).resolve().parent.parent


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def materials():
    document = json.loads((ROOT / "samples" / "long_response.json").read_text(encoding="utf-8"))
    catalog_from_document(document["catalog"])
    validate_channels(document["channels"])
    validate_timing(document["timing"])
    require([case["run_id"] for case in document["cases"]] == ["timely", "late"], "missing cases")
    for case in document["cases"]:
        normalize_frames(case["frames"])
        expected = case["expected"]
        body = expected["body"]
        pieces = []
        previous = 0
        for segment in expected["segments"]:
            a, b = segment["body_range"]
            c, d = segment["can_range"]
            require(a == previous and b - a == d - c, "sample range mismatch")
            piece = case["frames"][segment["frame"]]["data"][c:d]
            require(piece == body[a:b], "sample source differs")
            pieces.extend(piece)
            previous = b
        require(pieces == body and len(body) == 12, "sample body mismatch")
        require(expected["start_frame"] == 1 and body[:3] == [98, 18, 52], "sample prefix mismatch")
    expected_field = document["cases"][0]["expected"]["field"]
    raw = int.from_bytes(bytes(document["cases"][0]["expected"]["body"][5:7]), "big")
    require(raw == expected_field["raw_integer"] == 515, "hand integer mismatch")
    value = Fraction(raw, 10)
    require(expected_field["value"] == {"numerator": value.numerator, "denominator": value.denominator},
            "hand rational mismatch")
    return document


def check_view(view, case):
    expected = case["expected"]
    operations = view["operations"]
    require([row["status"] for row in operations] == expected["statuses"], "operation status mismatch")
    require([row["completed_at_ms"] for row in operations] == expected["completed_at_ms"],
            "operation completion mismatch")
    require(len(view["transfers"]) == 1, "missing or extra transfer")
    transfer = view["transfers"][0]
    for key in ("operation_id", "start_frame", "end_frame", "start_ms", "end_ms", "frames", "body", "segments"):
        require(transfer[key] == expected[key], "transfer mismatch: " + key)
    require(transfer["id"] == expected["transfer_id"], "transfer identity mismatch")
    require(transfer["ecu"] == "A" and transfer["declared_length"] == 12, "transfer metadata mismatch")
    require(transfer["status"] == "complete" and transfer["reason"] is None
            and transfer["binding_reason"] is None, "transfer conclusion mismatch")
    require(operations[0]["transfer_ids"] == [transfer["id"]], "original owner lost transfer")
    for other in operations[1:]:
        require(other["transfer_ids"] == [], "later query stole transfer")
    messages = [row for row in view["messages"] if row.get("transfer_id") == transfer["id"]]
    require(len(messages) == 1, "complete transfer must produce one message")
    message = messages[0]
    require(message["body"] == expected["body"] and message["frame"] == expected["start_frame"]
            and message["t_ms"] == expected["end_ms"] and message["segments"] == expected["segments"],
            "complete message metadata mismatch")
    if case["run_id"] == "late":
        require(all(row["responses"] == [] and row["interpretation"] == {"fields": [], "diagnostics": []}
                    for row in operations), "late body affected business result")
    else:
        require(len(operations[0]["responses"]) == 1, "eligible response missing")
        response = operations[0]["responses"][0]
        require(response["kind"] == "positive" and response["data"] == expected["body"][3:]
                and response["message"] == message, "response/message handoff mismatch")
        fields = operations[0]["interpretation"]["fields"]
        require(len(fields) == 1, "split field missing")
        for key, value in expected["field"].items():
            require(fields[0][key] == value, "split field mismatch: " + key)


def scenario(document):
    summaries = []
    with tempfile.TemporaryDirectory(prefix="diagcapture-long-") as folder:
        database = Path(folder) / "capture.sqlite3"
        saved = {}
        with ArchiveStore(database) as store:
            catalog = document["catalog"]
            CatalogRegistry(store.connection()).register_catalog(catalog["id"], catalog["dids"])
            service = CaptureService(store, catalog["id"])
            for case in document["cases"]:
                args = (case["run_id"], deepcopy(case["frames"]),
                        deepcopy(document["channels"]), deepcopy(document["timing"]))
                identity = service.capture_isotp(*args)
                require(identity == "report:" + case["run_id"], "report identity mismatch")
                require(service.capture_isotp(*args) == identity, "reimport not idempotent")
                try:
                    service.capture(*args)
                except CaptureIdentityConflict:
                    pass
                else:
                    raise ValueError("cross-profile run identity reuse accepted")
                view = report(store, identity).to_document()
                check_view(view, case)
                require(verify(store, identity).to_document()["verified"], "initial replay failed")
                saved[identity] = view
        with ArchiveStore(database) as store:
            for case in document["cases"]:
                identity = "report:" + case["run_id"]
                view = report(store, identity).to_document()
                require(view == saved[identity], "reopen changed fixed result")
                require(report(store, identity, "absent")["transfers"] == [], "ECU filter leaked transfer")
                require(verify(store, identity).to_document()["verified"], "reopen replay failed")
                export(store, identity, Path(folder) / case["run_id"])
        database.unlink()
        for case in document["cases"]:
            identity = "report:" + case["run_id"]
            with ArchiveStore(Path(folder) / case["run_id"]) as offline:
                view = report(offline, identity).to_document()
                require(view == saved[identity], "offline result changed")
                check_view(view, case)
                require(verify(offline, identity).to_document()["verified"], "offline replay failed")
                summaries.append({"run_id": case["run_id"],
                                  "statuses": [row["status"] for row in view["operations"]],
                                  "transfers": view["transfers"],
                                  "interpretation": [row["interpretation"] for row in view["operations"]]})
    print("POINTWISE_LONG_RESPONSE " + json.dumps(summaries, ensure_ascii=False, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-materials", action="store_true")
    args = parser.parse_args()
    document = materials()
    if args.check_materials:
        print("LONG_RESPONSE_MATERIALS_OK")
    else:
        scenario(document)


if __name__ == "__main__":
    main()
