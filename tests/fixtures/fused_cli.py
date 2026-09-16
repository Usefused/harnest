"""A subprocess protocol fixture for explicit Fused provisioning tests."""

import json
import os
from pathlib import Path
import sys


def option(arguments, flag):
    """Read an exact flag value as the real CLI's argument parser would."""

    return arguments[arguments.index(flag) + 1]


def respond(arguments):
    """Exercise the real command/receipt order without any external service."""

    command = arguments[:2]
    if command == ["whoami", "--json"]:
        return {"subject_id": "test-operator"}
    if command == ["import", "plan"]:
        name = option(arguments, "--slug")
        receipt = {"plan_id": f"import-{name}", "review_hash": "review", "slug": name, "target_version": "2026-09"}
        Path(option(arguments, "--receipt-out")).write_text(json.dumps(receipt))
        return receipt
    if command == ["import", "apply"]:
        receipt = json.loads(Path(option(arguments, "--receipt")).read_text())
        return {"status": "applied", "phase": "complete", "commit_state": "committed",
                "operation_id": receipt["plan_id"], "slug": receipt["slug"], "version": receipt["target_version"],
                "service_id": "service-id", "service_version_id": "version-id"}
    if command == ["mcp", "plan"]:
        config = json.loads(Path(option(arguments, "--file")).read_text())
        selections = config["services"].values()
        if any("unknown" in item.get("operations", []) for item in selections):
            sys.exit(4)
        receipt = {"plan_id": "mcp-plan"}
        Path(option(arguments, "--receipt-out")).write_text(json.dumps(receipt))
        return [receipt]
    if command == ["mcp", "apply"]:
        assert "--json" not in arguments
        assert Path(option(arguments, "--receipt")).exists()
        return None
    if command == ["mcp", "versions"]:
        return version_page(arguments)
    raise AssertionError("unexpected command")


def version_page(arguments):
    """Keep the desired version off the first page to exercise pagination."""

    if option(arguments, "--offset") == "0":
        return {"items": [{"name": "business", "version": "old"}], "total": 2}
    return {"items": [{"name": "business", "version": "1.0.0", "status": "active",
                       "transport_urls": {"streamable_http": "https://engine.test/mcp/family",
                                          "versioned_streamable_http": "https://engine.test/mcp/pinned"}}], "total": 2}


def main():
    """Record only arguments and simulate terminal, malformed, and partial failures."""

    arguments = sys.argv[1:]
    assert arguments.pop(0) == "--no-input"
    with Path(os.environ["FUSED_TEST_LOG"]).open("a") as output:
        output.write(json.dumps(arguments) + "\n")
    if " ".join(arguments[:2]) == os.environ.get("FUSED_TEST_FAIL"):
        print("sensitive provider output", file=sys.stderr)
        sys.exit(3)
    value = respond(arguments)
    if " ".join(arguments[:2]) == os.environ.get("FUSED_TEST_BAD_JSON"):
        print("sensitive invalid json")
    elif value is not None:
        print(json.dumps(value))


if __name__ == "__main__":
    main()
