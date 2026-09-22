"""Decision-based skill selection preserves context, authorization and provider boundaries."""

import asyncio
from dataclasses import replace
import json
from types import MappingProxyType
import unittest

from harnest.agent import Agent
from harnest.context import activate_context, create_agent_context, derive_agent_context, revoke_context
from harnest.decisions import ChoiceResult, DecisionCapabilities, DecisionResponse, Decisions, QuestionKind
from harnest.skills import (
    DecisionSkillSelector, SkillContext, SkillDescriptor, SkillDocument, SkillPage,
    SkillRegistry, SkillScope, SkillSelectionError, SkillSelectionFallback, SkillSource,
)
from harnest.skill_selection import selected_instructions


class Source(SkillSource):
    """Expose metadata independently from instruction body reads and scope checks."""

    def __init__(self):
        """Keep an observable catalog that can change between invocations."""
        self.ids = ["triage", "authentication", "billing"]
        self.loads = []
        self.lists = []

    async def list(self, context, *, query=None, cursor=None, limit=50):
        """Return only the current agent's permitted descriptors."""
        self.lists.append((context.agent_name, limit))
        ids = self.ids if context.agent_name == "root" else self.ids[:1]
        return SkillPage(tuple(self.descriptor(name) for name in ids[:limit]))

    async def load(self, skill_id, context, *, version=None):
        """Record exact identities and make bodies distinguishable from descriptions."""
        self.loads.append((context.agent_name, skill_id, version))
        if skill_id not in self.ids or version != "v1":
            raise ValueError("denied")
        return SkillDocument(self.descriptor(skill_id), f"PRIVATE BODY {skill_id}")

    def descriptor(self, name):
        """Produce a stable source-owned identity, distinct from decision answer keys."""
        return SkillDescriptor(name, name, f"Guidance for {name}", "v1")


class Provider:
    """Select candidates through real Choice validation rather than a selector mock."""

    capabilities = DecisionCapabilities(frozenset({QuestionKind.CHOICE}), batching=True)
    version = "test-1"

    def __init__(self, choose="select"):
        """Record requests only in the fixture to verify the disclosure boundary."""
        self.requests = []
        self.choose = choose

    async def evaluate(self, request):
        """Support success, invalid output, timeout and private-error fixtures."""
        self.requests.append(request)
        if self.choose == "error":
            raise RuntimeError("PRIVATE PROVIDER DETAIL")
        if self.choose == "timeout":
            await asyncio.sleep(10)
        return DecisionResponse({q.name: ChoiceResult(self.choose) for q in request.definition.questions})


def active_context(source, provider, *, name="root", providers=None):
    """Supply the same registry and resources owned by compiled runtime invocations."""
    return create_agent_context(
        framework="adk", agent_name=name, invocation_id="inv", user_id="user", session_id="session",
        metadata={"private": "not automatically sent"},
        resources={"decisions": Decisions(providers=providers or {"test": provider}, bindings=())},
        skill_registry=SkillRegistry({key: SkillScope({"remote": source}) for key in ("root", "child")}),
    )


class SkillSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Give each invocation independent provider history and lifecycle ownership."""
        self.source, self.provider = Source(), Provider()
        self.active = active_context(self.source, self.provider)
        self.addCleanup(revoke_context, self.active)

    async def test_dynamic_candidates_multiple_selections_and_invocation_cache(self):
        """Concurrent model passes select once and never reveal full bodies to the decision engine."""
        self.active._decision_output.enabled = True
        selector = DecisionSkillSelector(max_skills=2)
        with activate_context(self.active):
            results = await asyncio.gather(*(selected_instructions(selector, "help", {"private": "secret"}) for _ in range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.provider.requests), 1)
        request = self.provider.requests[0]
        self.assertEqual(dict(request.state["input"]), {"task": "help"})
        self.assertEqual(len(request.definition.questions), 3)
        self.assertNotIn("PRIVATE BODY", json.dumps([q.instructions for q in request.definition.questions]))
        self.assertEqual(self.source.loads, [("root", "triage", "v1"), ("root", "authentication", "v1")])
        self.assertEqual(self.active._decision_output.drain(), [])

    async def test_input_uses_existing_context_and_only_explicit_fields(self):
        """Custom callbacks see immutable state but do not implicitly forward task or identity."""
        captured = []
        async def build(context):
            """Select a deliberately small provider payload from the existing context."""
            self.assertIsInstance(context, SkillContext)
            self.assertEqual(context.task, "original task")
            self.assertEqual(context.agent_name, "root")
            captured.append(context)
            with self.assertRaises(TypeError):
                context.state["nested"]["private"] = "changed"
            return {"product": context.state["product"], "task": "custom description"}
        with activate_context(self.active):
            await selected_instructions(DecisionSkillSelector(input=build), "original task", {"product": "api", "nested": {"private": "hidden"}})
            with self.assertRaisesRegex(RuntimeError, "only during"):
                _ = captured[0].task
        self.assertEqual(dict(self.provider.requests[0].state["input"]), {"product": "api", "task": "custom description"})

    async def test_none_and_explicit_requests(self):
        """Skip means no automatic loads; exact mentions take precedence over decisions."""
        self.provider.choose = "skip"
        with activate_context(self.active):
            self.assertEqual(await selected_instructions(DecisionSkillSelector(), "unrelated", {}), "")
            result = await selected_instructions(DecisionSkillSelector(max_skills=1), "Use $authentication", {})
        self.assertIn("PRIVATE BODY authentication", result)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_empty_catalog_needs_no_provider(self):
        """No candidates means no callbacks, model calls or body reads."""
        self.source.ids.clear()
        with activate_context(self.active):
            self.assertEqual(await selected_instructions(DecisionSkillSelector(), "help", {}), "")
        self.assertEqual(self.provider.requests, [])

    async def test_failures_are_bounded_and_sanitized(self):
        """Provider errors, invalid answers and deadlines use the declared enum policy."""
        for failure in ("error", "invented", "timeout"):
            self.provider.choose = failure
            with self.subTest(failure=failure), activate_context(self.active):
                selector = DecisionSkillSelector(timeout_seconds=.02)
                self.assertEqual(await selected_instructions(selector, failure, {}), "")
                with self.assertRaisesRegex(SkillSelectionError, "^automatic skill selection failed$"):
                    await selected_instructions(replace(selector, fallback=DecisionSkillSelector.ERROR), failure, {})
        self.assertEqual(self.source.loads, [])

    async def test_cancellation_is_not_discovery_fallback(self):
        """Caller cancellation propagates instead of silently continuing agent work."""
        self.provider.choose = "timeout"
        with activate_context(self.active):
            task = asyncio.create_task(selected_instructions(DecisionSkillSelector(), "help", {}))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_child_scope_and_new_invocation_do_not_reuse_parent_selection(self):
        """Recheck agent visibility and rediscover catalog changes on a new request."""
        selector = DecisionSkillSelector()
        with activate_context(self.active):
            await selected_instructions(selector, "help", {})
        child = derive_agent_context(self.active, agent_name="child")
        with activate_context(child):
            await selected_instructions(selector, "help", {})
        self.assertEqual(self.source.loads[-1], ("child", "triage", "v1"))
        self.source.ids = ["new-skill"]
        fresh = active_context(self.source, self.provider)
        with activate_context(fresh):
            result = await selected_instructions(selector, "help", {})
        revoke_context(fresh)
        self.assertIn("new-skill", result)
        self.assertEqual(len(self.provider.requests), 3)

    async def test_candidate_limit_and_custom_provider_selection(self):
        """An explicit provider resolves multi-provider registries without registering static choices."""
        unused = Provider()
        active = active_context(self.source, self.provider, providers={"chosen": self.provider, "unused": unused})
        with activate_context(active):
            await selected_instructions(DecisionSkillSelector(provider="chosen", max_candidates=2, max_skills=1), "help", {})
        revoke_context(active)
        self.assertEqual(len(self.provider.requests[0].definition.questions), 2)
        self.assertEqual(unused.requests, [])

    async def test_callback_cannot_override_candidates_and_invalid_input_falls_back(self):
        """Custom fields stay inside state.input and cannot replace decision identities."""
        with activate_context(self.active):
            await selected_instructions(DecisionSkillSelector(input=lambda c: {"candidates": ["forbidden"]}), "help", {})
            result = await selected_instructions(DecisionSkillSelector(input=lambda c: object()), "other", {})
        self.assertEqual(result, "")
        self.assertEqual(len(self.provider.requests[0].definition.questions), 3)
        self.assertNotIn("forbidden", json.dumps([q.instructions for q in self.provider.requests[0].definition.questions]))

    async def test_existing_pin_cannot_be_replaced_by_new_catalog_version(self):
        """Selection honors versions already loaded by the agent's ordinary skill tools."""
        self.active._skill_pins[("root", "remote", "triage")] = "older"
        with activate_context(self.active):
            result = await selected_instructions(DecisionSkillSelector(), "help", {})
        self.assertEqual(result, "")
        self.assertEqual(self.source.loads, [])

    async def test_revoked_context_cannot_return_a_pending_selection(self):
        """An awaited provider result cannot revive an invocation after it has ended."""
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.provider.evaluate
        async def delayed(request):
            """Let the test revoke invocation authority while the provider is in flight."""
            entered.set()
            await release.wait()
            return await original(request)
        self.provider.evaluate = delayed
        with activate_context(self.active):
            pending = asyncio.create_task(selected_instructions(DecisionSkillSelector(), "help", {}))
            await entered.wait()
            revoke_context(self.active)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "invocation has finished"):
                await pending
        self.assertEqual(self.source.loads, [])

    async def test_provider_replacement_and_registry_ambiguity_are_explicit(self):
        """Dynamic definitions never register mutable choices or silently pick a provider."""
        registry = Decisions(providers={"first": self.provider, "second": Provider()}, bindings=())
        from harnest.decisions import Choice, DecisionDefinition
        definition = DecisionDefinition("dynamic", "1", (Choice("pick", "Choose", {"select": "Yes", "skip": "No"}),))
        with self.assertRaisesRegex(ValueError, "choose a provider"):
            await registry.evaluate_definition(definition, {})
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            await registry.evaluate_definition(definition, {}, provider="absent")
        await registry.evaluate_definition(definition, {}, provider="first")
        with self.assertRaises(KeyError):
            await registry.evaluate("dynamic", {})

    def test_configuration_is_typed_and_opt_in(self):
        """Reject static string modes and invalid work bounds at agent construction."""
        self.assertIs(DecisionSkillSelector.DISCOVERY, SkillSelectionFallback.DISCOVERY)
        self.assertIsNone(Agent(name="root", model="test").skill_selection)
        for options in ({"fallback": "discovery"}, {"max_skills": 0}, {"max_candidates": True}, {"timeout_seconds": 0}, {"input": "prompt"}):
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                DecisionSkillSelector(**options)
        with self.assertRaises(TypeError):
            Agent(name="root", model="test", skill_selection=True)
        self.assertIsInstance(self.active.metadata, MappingProxyType)
