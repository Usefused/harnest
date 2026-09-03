import json
import unittest
from types import MappingProxyType

from harnest.sandbox_types import (
    SandboxContext,
    SandboxFile,
    SandboxRequest,
    SandboxResult,
    freeze_sandbox_metadata,
    sandbox_metadata_to_dict,
)


class SandboxMetadataTests(unittest.TestCase):
    def test_metadata_deeply_freezes_caller_containers(self):
        original = {"provider": {"tags": ["initial", {"attempt": 1}]}}
        request = SandboxRequest("print(1)", metadata=original)
        result = SandboxResult(metadata=original)
        original["provider"]["tags"][1]["attempt"] = 2
        original["provider"]["tags"].append("later")

        for contract in (request, result):
            with self.subTest(contract=type(contract).__name__):
                self.assertIsInstance(contract.metadata, MappingProxyType)
                tags = contract.metadata["provider"]["tags"]
                self.assertIsInstance(tags, tuple)
                self.assertEqual(len(tags), 2)
                self.assertEqual(tags[1]["attempt"], 1)
                with self.assertRaises(TypeError):
                    tags[1]["attempt"] = 3

    def test_metadata_roundtrip_preserves_json_types_and_returns_fresh_data(self):
        original = {
            "text": "provider-id",
            "flag": True,
            "integer": 2**60,
            "decimal": 1.25,
            "null": None,
            "array": [False, 0, {"nested": ["value"]}],
        }
        frozen = freeze_sandbox_metadata(original)
        native = sandbox_metadata_to_dict(frozen)
        self.assertEqual(native, original)
        self.assertEqual(json.loads(json.dumps(native)), original)
        for key in ("flag", "integer", "decimal", "null"):
            self.assertIs(type(native[key]), type(original[key]))
        native["array"][2]["nested"].append("changed")
        self.assertEqual(sandbox_metadata_to_dict(frozen), original)

    def test_private_metadata_does_not_appear_in_contract_repr(self):
        for contract in (
            SandboxRequest("print(1)", metadata={"private": "secret-provider-id"}),
            SandboxResult(metadata={"private": "secret-provider-id"}),
        ):
            self.assertNotIn("secret-provider-id", repr(contract))
            self.assertNotIn("metadata", repr(contract))

    def test_metadata_rejects_non_json_values_without_stringification(self):
        class ProviderObject:
            def __str__(self):
                raise AssertionError("provider objects must not be stringified")

        invalid = [object(), ProviderObject(), b"bytes", {"set"}, 1j]
        for value in invalid:
            for contract in (SandboxRequest, SandboxResult):
                with self.subTest(value=type(value).__name__, contract=contract):
                    with self.assertRaisesRegex(TypeError, "JSON-compatible"):
                        contract("output", metadata={"nested": [value]})

    def test_metadata_rejects_nonfinite_numbers_and_non_string_keys(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    freeze_sandbox_metadata({"value": value})
        for key in (1, False, None, ("tuple",)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(TypeError, "keys must be strings"):
                    freeze_sandbox_metadata({"nested": {key: "value"}})

    def test_metadata_rejects_cycles_but_accepts_shared_acyclic_values(self):
        cyclic_array = []
        cyclic_array.append(cyclic_array)
        cyclic_object = {}
        cyclic_object["self"] = cyclic_object
        for value in (cyclic_array, cyclic_object):
            with self.assertRaisesRegex(ValueError, "cycles"):
                freeze_sandbox_metadata({"nested": value})

        shared = {"array": [1, 2]}
        source = {"first": shared, "second": shared}
        self.assertEqual(sandbox_metadata_to_dict(source), source)

    def test_metadata_requires_an_object_and_defaults_to_empty(self):
        for value in (None, [], "metadata", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TypeError, "JSON object"):
                    freeze_sandbox_metadata(value)
        self.assertEqual(SandboxRequest("print(1)").metadata, {})
        self.assertEqual(SandboxResult().metadata, {})

    def test_metadata_is_appended_without_changing_existing_positional_fields(self):
        context = SandboxContext(agent_name="root")
        file = SandboxFile("output.txt", b"value")
        request = SandboxRequest("print(1)", 5, context, (file,), "execution")
        result = SandboxResult("stdout", "stderr", (file,))
        self.assertEqual(request.execution_id, "execution")
        self.assertEqual(request.input_files, (file,))
        self.assertEqual(result.output_files, (file,))
        self.assertEqual(result.metadata, {})


if __name__ == "__main__":
    unittest.main()
