import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harnest.bundle import compile_application
from harnest.context import (
    activate_context,
    context,
    create_agent_context,
    derive_agent_context,
    revoke_context,
)
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.runtime_auth import AuthPrincipal, _activate_authenticated_principal
from harnest.skills import (
    FilesystemSkillSource,
    SkillDescriptor,
    SkillDocument,
    SkillNotFoundError,
    SkillPage,
    SkillRegistry,
    SkillResource,
    SkillScope,
    SkillSource,
    SkillSourceExecutionError,
    SkillValidationError,
    create_skill_tools,
    scoped_skill_sources,
)

from _session_store_fixture import write_session_store
from _skill_fixture import run_skill_tool


class _DynamicSource(SkillSource):
    """Provide mutable versions while retaining older pinned documents."""

    def __init__(self, *, allowed_agent: str = "root") -> None:
        self.allowed_agent = allowed_agent
        self.current = "v1"
        self.documents = {
            "v1": "Use the first generated workflow.",
            "v2": "Use the second generated workflow.",
        }
        self.seen = []

    def _descriptor(self, version: str) -> SkillDescriptor:
        return SkillDescriptor(
            "website-42",
            "website-42",
            "Operate the generated website workflow.",
            version,
        )

    async def list(self, context, *, query=None, cursor=None, limit=50):
        self.seen.append(("list", context.agent_name, context.user_id, limit))
        if context.agent_name != self.allowed_agent:
            return SkillPage(())
        return SkillPage((self._descriptor(self.current),))

    async def load(self, skill_id, context, *, version=None):
        self.seen.append(("load", context.agent_name, skill_id, version))
        if context.agent_name != self.allowed_agent or skill_id != "website-42":
            raise SkillNotFoundError("skill is unavailable")
        selected = version or self.current
        if selected not in self.documents:
            raise SkillNotFoundError("skill version is unavailable")
        return SkillDocument(self._descriptor(selected), self.documents[selected])

    async def load_resource(
        self, skill_id, path, context, *, version=None
    ):
        document = await self.load(skill_id, context, version=version)
        return SkillResource(
            skill_id,
            document.descriptor.version,
            path,
            f"resource:{path}:{document.descriptor.version}",
        )


class _FailingSource(SkillSource):
    async def list(self, context, *, query=None, cursor=None, limit=50):
        raise RuntimeError("secret-bearing-provider-message")

    async def load(self, skill_id, context, *, version=None):
        raise RuntimeError("secret-bearing-provider-message")


class SkillSourceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_skill(root: Path) -> Path:
        skill = root / "skills" / "local-guide"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: local-guide\ndescription: Use local guidance.\n---\n"
            "\nFollow the local workflow.\n",
            encoding="utf-8",
        )
        (skill / "references.md").write_text("Reference content.\n", encoding="utf-8")
        return skill

    @staticmethod
    def _context(registry: SkillRegistry, agent_name: str = "root"):
        return create_agent_context(
            framework="langgraph",
            agent_name=agent_name,
            invocation_id="invocation-1",
            user_id="user-1",
            session_id="session-1",
            metadata={"tenant": "tenant-1"},
            resources={},
            skill_registry=registry,
        )

    async def test_filesystem_source_uses_the_runtime_contract_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = self._write_skill(Path(directory))
            source = FilesystemSkillSource((skill,))
            scope = scoped_skill_sources({}, source)
            registry = SkillRegistry({"root": scope})
            active = self._context(registry)
            try:
                with activate_context(active):
                    page = await context.skills.list()
                    natural_match = await context.skills.list(
                        query="find guidance for local workflows"
                    )
                    document = await context.skills.load("local-guide")
                    resource = await context.skills.load_resource(
                        "local-guide", "references.md"
                    )
                descriptor = page.items[0].descriptor
                self.assertEqual(descriptor.name, "local-guide")
                self.assertEqual(
                    natural_match.items[0].descriptor.name, "local-guide"
                )
                self.assertTrue(descriptor.version.startswith("sha256:"))
                self.assertIn("Follow the local workflow.", document.instructions)
                self.assertEqual(resource.content, "Reference content.\n")

                (skill / "references.md").write_text("changed", encoding="utf-8")
                with activate_context(active):
                    with self.assertRaisesRegex(
                        SkillValidationError, "changed after compilation"
                    ):
                        await context.skills.load("local-guide")
            finally:
                revoke_context(active)

    async def test_filesystem_source_fuzzy_ranks_skill_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            paths = []
            for name, description in (
                ("contract-design", "Design executable workflow contracts."),
                ("thread-analysis", "Analyze live workflow threads and failures."),
            ):
                path = root / name
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {description}\n---\n",
                    encoding="utf-8",
                )
                paths.append(path)
            source = FilesystemSkillSource(tuple(paths))
            scope = scoped_skill_sources({}, source)
            active = self._context(SkillRegistry({"root": scope}))
            try:
                with activate_context(active):
                    page = await context.skills.list(query="thred analysys")
                self.assertEqual(
                    [item.descriptor.id for item in page.items],
                    ["thread-analysis"],
                )
            finally:
                revoke_context(active)

    async def test_dynamic_source_filters_by_agent_and_pins_loaded_version(self):
        source = _DynamicSource(allowed_agent="researcher")
        shared = SkillScope({"wex": source})
        registry = SkillRegistry({"root": shared, "researcher": shared})
        active = self._context(registry)
        try:
            with activate_context(active):
                root_page = await context.skills.list()
                child = derive_agent_context(active, agent_name="researcher")
                with activate_context(child):
                    child_page = await context.skills.list()
                    first = await context.skills.load(
                        "website-42", source="wex"
                    )
                    source.current = "v2"
                    pinned = await context.skills.load(
                        "website-42", source="wex"
                    )
                    with self.assertRaisesRegex(
                        SkillValidationError, "cannot change"
                    ):
                        await context.skills.load(
                            "website-42", source="wex", version="v2"
                        )
            self.assertEqual(root_page.items, ())
            self.assertEqual(child_page.items[0].source, "wex")
            self.assertEqual(first.descriptor.version, "v1")
            self.assertEqual(pinned.descriptor.version, "v1")
            self.assertIn(("list", "researcher", "user-1", 50), source.seen)
        finally:
            revoke_context(active)

    async def test_skill_context_exposes_verified_claims_without_credentials(self):
        class ClaimsSource(_DynamicSource):
            async def list(self, skill_context, **kwargs):
                self.claims = skill_context.claims
                return await super().list(skill_context, **kwargs)

        source = ClaimsSource()
        registry = SkillRegistry({"root": SkillScope({"wex": source})})
        active = self._context(registry)
        principal = AuthPrincipal(
            "user-1",
            claims={"tenant": "tenant-1", "role": "author"},
        )
        try:
            with _activate_authenticated_principal(principal), activate_context(active):
                await context.skills.list(source="wex")
            self.assertEqual(source.claims["tenant"], "tenant-1")
            self.assertNotIn("credentials", source.claims)
        finally:
            revoke_context(active)

    async def test_model_tools_and_context_access_share_the_same_scope(self):
        source = _DynamicSource()
        scope = SkillScope({"wex": source})
        registry = SkillRegistry({"root": scope})
        active = self._context(registry)
        tools = {value.__name__: value for value in create_skill_tools(scope)}
        try:
            with activate_context(active):
                payload = json.loads(await tools["list_skills"]())
                loaded = await tools["load_skill"](
                    "website-42", source="wex"
                )
                direct = await context.skills.load("website-42", source="wex")
            self.assertEqual(payload["skills"][0]["source"], "wex")
            self.assertEqual(payload["skills"][0]["id"], "website-42")
            self.assertEqual(loaded, direct.instructions)
        finally:
            revoke_context(active)

    async def test_model_skill_search_falls_back_from_literal_provider_queries(self):
        class LiteralSource(_DynamicSource):
            def __init__(self):
                super().__init__()
                self.queries = []

            async def list(self, skill_context, *, query=None, **kwargs):
                self.queries.append(query)
                page = await super().list(
                    skill_context, query=query, **kwargs
                )
                return SkillPage(()) if query else page

        source = LiteralSource()
        scope = SkillScope({"wex": source})
        active = self._context(SkillRegistry({"root": scope}))
        tools = {value.__name__: value for value in create_skill_tools(scope)}
        try:
            with activate_context(active):
                payload = json.loads(
                    await tools["list_skills"](
                        query="find the website automation skill"
                    )
                )
            self.assertEqual(
                source.queries,
                ["find the website automation skill", None],
            )
            self.assertTrue(payload["queryFallback"])
            self.assertEqual(payload["skills"][0]["id"], "website-42")
            self.assertIn("nextCursors", tools["list_skills"].__doc__)
        finally:
            revoke_context(active)

    async def test_source_failures_are_redacted_at_the_registry_boundary(self):
        active = self._context(
            SkillRegistry({"root": SkillScope({"remote": _FailingSource()})})
        )
        try:
            with activate_context(active):
                with self.assertRaises(SkillSourceExecutionError) as failure:
                    await context.skills.list()
            self.assertNotIn("secret-bearing-provider-message", str(failure.exception))
            self.assertIn("RuntimeError", str(failure.exception))
        finally:
            revoke_context(active)

    async def test_filesystem_source_name_is_reserved_for_compiler_content(self):
        with self.assertRaisesRegex(SkillValidationError, "reserved"):
            scoped_skill_sources({"filesystem": _DynamicSource()}, None)


class SkillCompilerIntegrationTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _backend() -> SimpleNamespace:
        return SimpleNamespace(
            lower_managed=lambda value, **_kwargs: value,
            wrap_managed=lambda target, native_extensions=(): None,
        )

    def _write_dynamic_agent(self, root: Path) -> None:
        write_session_store(root)
        self._write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            "root_agent = Agent(name='root', model='test/model')\n",
        )
        self._write(root / "instructions.md", "Use generated skills.\n")
        self._write(
            root / "lib" / "wex.py",
            "from harnest.skills import (SkillDescriptor, SkillDocument, "
            "SkillPage, SkillSource)\n"
            "class WexSource(SkillSource):\n"
            "  async def list(self, context, *, query=None, cursor=None, limit=50):\n"
            "    item = SkillDescriptor('site-1', 'site-1', "
            "f'Website skill for {context.agent_name}.', 'v1')\n"
            "    return SkillPage((item,))\n"
            "  async def load(self, skill_id, context, *, version=None):\n"
            "    item = SkillDescriptor(skill_id, skill_id, 'Website skill.', 'v1')\n"
            "    return SkillDocument(item, f'Operate {skill_id} for {context.user_id}.')\n",
        )
        self._write(
            root / "extensions" / "skills.py",
            "from harnest.lib.wex import WexSource\n"
            "from harnest.lifecycle import lifecycle\n"
            "@lifecycle.skills.source('wex')\n"
            "def wex(): return WexSource()\n",
        )

    def test_harnest_lib_source_compiles_for_both_framework_adapters(self):
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write_dynamic_agent(root)
                with patch("harnest.bundle.get_backend", return_value=self._backend()):
                    compiled = compile_application(
                        root, entrypoint="agent:root_agent", framework=framework
                    )
                tools = {tool.__name__: tool for tool in compiled.target.tools}
                catalog = json.loads(
                    run_skill_tool(compiled, "root", tools["list_skills"])
                )
                loaded = run_skill_tool(
                    compiled,
                    "root",
                    tools["load_skill"],
                    "site-1",
                    source="wex",
                )
                self.assertEqual(
                    catalog["skills"][0]["description"],
                    "Website skill for root.",
                )
                self.assertEqual(loaded, "Operate site-1 for skill-test-user.")

    def test_extension_discovery_validates_named_source_factories(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Path(temp)
            write_session_store(agent)
            root = agent / "extensions"
            self._write(
                root / "skills.py",
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.skills.source('wex')\n"
                "def wex(): return object()\n",
            )
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "must return SkillSource"
            ):
                discover_extensions(root, framework="langgraph")


if __name__ == "__main__":
    unittest.main()
