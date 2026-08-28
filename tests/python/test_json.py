import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

from harnest._json import JSONValueError, json_value


class Status(Enum):
    READY = "ready"


@dataclass
class SearchHit:
    title: str
    score: Decimal
    indexed_at: datetime


class DumpModel:
    def model_dump(self, *, mode, by_alias):
        if (mode, by_alias) != ("json", True):
            raise AssertionError("model_dump must request JSON aliases")
        return {"documentId": UUID("12345678-1234-5678-1234-567812345678")}


class JSONValueTests(unittest.TestCase):
    def test_normalizes_structured_models_and_common_scalars(self):
        hit = SearchHit(
            title="Guide",
            score=Decimal("0.95"),
            indexed_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )

        value = json_value(
            {
                "hits": [hit],
                "status": Status.READY,
                "model": DumpModel(),
                "source": Path("handbook.md"),
            }
        )

        self.assertEqual(
            value,
            {
                "hits": [
                    {
                        "title": "Guide",
                        "score": "0.95",
                        "indexed_at": "2026-08-28T12:00:00+00:00",
                    }
                ],
                "status": "ready",
                "model": {"documentId": "12345678-1234-5678-1234-567812345678"},
                "source": "handbook.md",
            },
        )

    def test_rejects_unsupported_nonfinite_and_recursive_values(self):
        with self.assertRaisesRegex(JSONValueError, "unsupported JSON value type set"):
            json_value({"tags": {"one", "two"}})
        with self.assertRaisesRegex(JSONValueError, "keys must be strings"):
            json_value({1: "one"})
        with self.assertRaisesRegex(JSONValueError, "finite"):
            json_value(float("nan"))
        recursive = []
        recursive.append(recursive)
        with self.assertRaisesRegex(JSONValueError, "recursive"):
            json_value(recursive)

    def test_best_effort_diagnostics_may_stringify_unsupported_values(self):
        marker = object()

        self.assertEqual(json_value(marker, unsupported="string"), str(marker))


if __name__ == "__main__":
    unittest.main()
