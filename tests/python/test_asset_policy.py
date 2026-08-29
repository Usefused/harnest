import unittest
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel

from harnest import Stored
from harnest.asset_policy import storage_policy
from harnest.content import Image


class AssetPolicyTests(unittest.TestCase):
    def test_stored_is_explicit_annotation_metadata(self):
        policy = Stored(
            store="media",
            path="screenshots",
            expires_in=30,
            retention=timedelta(days=7),
        )

        class Result(BaseModel):
            screenshot: Annotated[Image, policy]

        self.assertEqual(
            storage_policy(Result.model_fields["screenshot"].metadata), policy
        )
        self.assertEqual(storage_policy(Annotated[Image, policy]), policy)
        schema = Result.model_json_schema()["properties"]["screenshot"]
        self.assertEqual(schema["x-harnest-storage"]["store"], "media")
        self.assertEqual(schema["x-harnest-storage"]["expiresIn"], 30)
        self.assertEqual(policy.retention_seconds, 7 * 24 * 60 * 60)

    def test_stored_rejects_ambiguous_or_unsafe_configuration(self):
        for kwargs in (
            {"store": "not/valid"},
            {"path": "../private"},
            {"path": "/absolute"},
            {"expires_in": 0},
            {"retention": timedelta(0)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                Stored(**kwargs)


if __name__ == "__main__":
    unittest.main()
