"""Pure authoring contracts for the separately installable Fused package."""

from dataclasses import replace
from pathlib import Path
import os
import unittest
from unittest.mock import patch

from harnest.mcp import MCPClient
from harnest_fused import FusedMCPClient, OpenAPISpec
from harnest_fused._serialize import client_source


class FusedClientTests(unittest.TestCase):
    def test_declaration_is_offline_and_runtime_is_standard_mcp(self):
        with patch("subprocess.run", side_effect=AssertionError("process")), patch("pathlib.Path.open", side_effect=AssertionError("file")), patch.dict(os.environ, {}, clear=True):
            client = FusedMCPClient.from_openapi("specs/crm.yaml", "specs/billing.yaml", name="business")
        self.assertIsInstance(client, MCPClient)
        self.assertEqual(client.url, "${HARNEST_FUSED_BUSINESS_URL}")
        self.assertEqual([spec.name for spec in client.specs], ["crm", "billing"])
        self.assertTrue(all(spec.operations is None for spec in client.specs))

    def test_mixed_selection_is_frozen_and_scoped_per_service(self):
        selected = ["listInvoices", "getInvoice"]
        billing = OpenAPISpec("billing.yaml", operations=selected)
        selected.append("deleteInvoice")
        self.assertEqual(billing.selection("v2"), {"version": "v2", "operations": ["listInvoices", "getInvoice"]})
        self.assertEqual(OpenAPISpec("crm.yaml").selection("v1"), {"version": "v1", "select_all": True})

    def test_standard_policies_survive_compiler_identity_assignment(self):
        client = FusedMCPClient.from_openapi(
            "crm.yaml", name="business", prefix="business", permission="crm.use",
            tool_permissions={"execute": "crm.execute"}, timeout_seconds=17,
            headers={"X-Fused-End-User-Ref": "${CUSTOMER_ID}"},
        )
        configured = replace(client, identity="business", capability_id="mcp__business")
        self.assertEqual(configured.specs, client.specs)
        self.assertEqual(configured.required_permissions_for("execute"), {"crm.use", "crm.execute"})
        with patch.dict(os.environ, HARNEST_FUSED_BUSINESS_URL="https://engine.test/mcp/pinned", HARNEST_FUSED_BUSINESS_TOKEN="private-token", CUSTOMER_ID="customer"):
            connection = configured.to_langgraph_connection()
        self.assertEqual(connection["url"], "https://engine.test/mcp/pinned")
        self.assertEqual(connection["headers"]["Authorization"], "Bearer private-token")
        self.assertEqual(connection["headers"]["X-Fused-End-User-Ref"], "customer")

    def test_names_and_environment_references_are_unambiguous(self):
        invalid = [({}, ()), ({}, ("a/crm.yaml", "b/crm.yaml")),
                   ({"url_env": "bad-name"}, ("crm.yaml",)),
                   ({"url_env": "SAME", "token_env": "SAME"}, ("crm.yaml",)),
                   ({"headers": {"authorization": "secret"}}, ("crm.yaml",))]
        for options, specs in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                FusedMCPClient.from_openapi(*specs, name="business", **options)
        with self.assertRaises(ValueError):
            FusedMCPClient.from_openapi("crm.yaml", name="../unsafe")

    def test_empty_or_ambiguous_selection_is_not_all_operations(self):
        for operations in ([], "listInvoices", [""], ["a", "a"], [None]):
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                OpenAPISpec("billing.yaml", operations=operations)

    def test_auth_selectors_are_frozen_and_credentials_are_not_accepted(self):
        auth = {"type": "oauth", "name": "oauth2", "ref": "${bucket.auth.crm.oauth2}"}
        spec = OpenAPISpec("crm.yaml", auth=auth, scopes=["read:customers"])
        auth["name"] = "changed"
        selection = spec.selection("v1")
        self.assertEqual(selection["auth"]["name"], "oauth2")
        self.assertEqual(selection["connect"], {"scopes": ["read:customers"]})
        with self.assertRaises(ValueError):
            OpenAPISpec("crm.yaml", auth={"token": "private"})

    def test_source_shorthand_and_explicit_names(self):
        self.assertEqual(OpenAPISpec(Path("my_api.yaml")).name, "my-api")
        self.assertEqual(OpenAPISpec("https://api.test/openapi.json", name="crm").name, "crm")
        with self.assertRaises(ValueError):
            OpenAPISpec("crm.yaml", version="")

    def test_generated_source_preserves_policy_and_rejects_custom_objects(self):
        client = MCPClient.streamable_http("${URL}", headers={"Authorization": "Bearer ${TOKEN}"}, permission="crm.use")
        namespace = {}
        exec(client_source(client), namespace)
        self.assertEqual(namespace["client"]().permission, "crm.use")
        with self.assertRaises(ValueError):
            client_source(replace(client, approval=object()))
