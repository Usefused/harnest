"""Durable, retryable provisioning through Docker Compose and the Kubernetes API CLI."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from .logging import get_logger
from .provisioner_config import Deployment, ProvisionError, load_manifest
from .provisioner_plan import OWNER_LABEL, Plan
from .provisioner_history import RevisionStore
from .provisioner_images import pin_plan
from .provisioner_lock import exclusive_lock

_AUDIT = get_logger("provisioner.audit")


def run_process(argv: list[str], payload: str = "") -> str:
    """Use argv and stdin; never expose process output that may contain credentials."""

    try:
        result = subprocess.run(argv, input=payload, text=True, capture_output=True, timeout=660, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisionError(f"{argv[0]} could not complete; check installation, connectivity, and readiness") from None
    if result.returncode:
        raise ProvisionError(f"{argv[0]} failed; inspect the selected backend for image, credential, or health-check errors")
    return result.stdout


class Provisioner:
    """Journal resource ownership before applying so interrupted operations can be retried or removed."""

    def __init__(self, root: Path, environment: str = "local", *, runner: Callable = run_process):
        """Bind operations to a trusted project and a bounded environment identifier."""

        import re
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment):
            raise ProvisionError("Invalid deployment environment name")
        self.root = root.resolve()
        self.environment = environment
        self.runner = runner
        self.directory = self.root / ".harnest" / "provisioner" / environment

    def plan(self, revision: int | None = None) -> dict:
        """Preview current intent or a selected rollback without resolving credentials or images."""

        if revision is None:
            return self._plan().summary()
        with self._locked():
            plan = self._rollback_plan(revision)
            return {**plan.summary(), "rollback_of": revision}


    def _plan(self) -> Plan:
        """Use the same validation and project identity for Studio and CLI requests."""

        return Plan(load_manifest(self.root, self.environment), self.environment, str(self.root))

    @contextmanager
    def _locked(self):
        """Serialize CLI and Studio operations across processes and reject linked state paths."""

        parts = self.directory.relative_to(self.root).parts
        for index in range(1, len(parts) + 1):
            path = self.root.joinpath(*parts[:index])
            if path.is_symlink():
                raise ProvisionError("Provisioner state cannot use symbolic links")
            path.mkdir(exist_ok=True, mode=0o700)
        with exclusive_lock(self.directory / "lock"):
            RevisionStore(self.directory).recover()
            yield

    def _read(self) -> dict:
        """Read current ownership from the same database that owns revision outcomes."""

        return RevisionStore(self.directory).read()

    def _write(self, state: dict) -> None:
        """Commit state and any revision outcome in one database transaction."""

        RevisionStore(self.directory).write(state)

    def apply(self, secrets: dict[str, str] | None = None) -> dict:
        """Freeze image identities and record a new revision before reconciling infrastructure."""

        with self._locked():
            plan = self._plan()
            credentials = os.environ if secrets is None else secrets
            for name in plan.workloads:
                plan.environment_for(name, credentials)
            # Resolve tags only after credentials and target compatibility are validated.
            _next_state(plan, self._read())
            plan, images = pin_plan(plan, self.runner)
            return self._deploy(plan, credentials, images)

    def _deploy(self, plan: Plan, credentials: dict, images: dict, rollback_of: int | None = None) -> dict:
        """Use one journaled reconciliation path for normal applies and explicit rollbacks."""

        previous = self._read()
        state = _next_state(plan, previous)
        operation = "rollback" if rollback_of is not None else "apply"
        state = RevisionStore(self.directory).begin(plan, state, operation, images, rollback_of)
        plan.revision = state["attempt_revision"]
        backend = Backend(plan, self.runner)
        return self._mutate(operation, state, lambda: backend.apply(credentials, previous))

    def history(self, limit: int = 20, before: int | None = None) -> dict:
        """Return paginated metadata without sending stored configuration or credentials to Studio."""

        with self._locked():
            store = RevisionStore(self.directory)
            state = store.read()
            return {"active_revision": state.get("active_revision"), **store.history(limit, before)}

    def recorded(self) -> dict:
        """Expose last recorded deployment metadata without presenting it as current backend health."""
        with self._locked():
            state = self._read()
            return {"status": state.get("status", "not-deployed"), "deployment": state.get("summary"),
                    "active_revision": state.get("active_revision"), "attempt_revision": state.get("attempt_revision")}

    def rollback(self, revision: int, secrets: dict[str, str] | None = None) -> dict:
        """Redeploy a successful snapshot as a new revision, using current secret-reference values."""

        with self._locked():
            plan = self._rollback_plan(revision)
            credentials = os.environ if secrets is None else secrets
            for name in plan.workloads:
                plan.environment_for(name, credentials)
            images = {name: {"requested": node.image, "resolved": node.image} for name, node in plan.workloads.items()}
            return self._deploy(plan, credentials, images, rollback_of=revision)

    def _rollback_plan(self, revision: int) -> Plan:
        """Check target identity and persistent-service compatibility before any rollback writes."""

        if revision < 1:
            raise ProvisionError("Choose a positive deployment revision")
        store = RevisionStore(self.directory)
        previous = store.read()
        if not previous or previous.get("status") == "removed":
            raise ProvisionError("Rollback requires an existing deployment")
        deployment = store.snapshot(revision)
        plan = Plan(deployment, self.environment, str(self.root))
        _next_state(plan, previous)
        latest = previous.get("attempt_revision")
        if latest is None:
            raise ProvisionError("Apply once to record a versioned deployment before rolling back")
        check_persistent_rollback(store.snapshot(latest, successful=False), deployment)
        return plan

    def _mutate(self, operation: str, state: dict, execute: Callable) -> dict:
        """Audit only committed operations or failures, without payloads or secret-bearing errors."""

        try:
            execute()
            final = committed_state(operation, state)
            self._write(final)
        except Exception:
            try:
                self._write({**state, "status": "failed", "operation": operation})
            finally:
                _AUDIT.warning("provisioner.failed", operation=operation, trigger="user", outcome="failed",
                               deployment_id=state["summary"]["identity"], backend=state["summary"]["backend"],
                    revision=state.get("attempt_revision"))
            raise
        _AUDIT.info("provisioner.committed", operation=operation, trigger="user", outcome="committed",
                    deployment_id=state["summary"]["identity"], backend=state["summary"]["backend"],
                    revision=state.get("attempt_revision"))
        return {"status": final["status"], "deployment": final["summary"],
                "revision": final.get("attempt_revision"), "active_revision": final.get("active_revision")}

    def control(self, operation: str) -> dict:
        """Stop or remove only recorded workloads, retaining connected services and all volumes."""

        if operation not in {"stop", "remove"}:
            raise ProvisionError("Unsupported control operation")
        with self._locked():
            state = self._read()
            if not state or state["status"] == "removed":
                return {"status": "not-deployed"}
            backend = _recorded_backend(state, self.runner)
            self._write({**state, "status": operation + "-pending", "operation": operation})
            return self._mutate(operation, state, lambda: backend.control(operation, state["resources"]))

    def status(self) -> dict:
        """Read actual backend status, even after the manifest changes or disappears."""

        with self._locked():
            state = self._read()
            if not state or state["status"] == "removed":
                return {"status": "not-deployed", "components": []}
            backend = _recorded_backend(state, self.runner)
            components = backend.status(state["resources"])
            return {"status": runtime_status(components, state), "last_operation_status": state["status"],
                    "deployment": state["summary"], "components": components,
                    "active_revision": state.get("active_revision"), "attempt_revision": state.get("attempt_revision")}


def _next_state(plan: Plan, previous: dict) -> dict:
    """Keep prior and desired ownership until apply succeeds, so partial updates remain removable."""

    summary = plan.summary()
    if previous.get("resources"):
        fields = ("identity", "backend", "context", "namespace")
        if any(summary[key] != previous["summary"][key] for key in fields):
            raise ProvisionError("Remove the previous deployment before changing its name or target")
    desired = {name: node.image for name, node in plan.workloads.items()}
    return {"summary": summary, "resources": {**previous.get("resources", {}), **desired}, "desired": desired,
            "active_revision": previous.get("active_revision")}


def _recorded_backend(state: dict, runner: Callable):
    """Recreate only target identity from the journal; source edits cannot redirect cleanup."""

    summary = state["summary"]
    deployment = Deployment(name=summary["name"], backend=summary["backend"], context=summary["context"], namespace=summary["namespace"])
    plan = Plan(deployment, summary["environment"], "")
    plan.identity = summary["identity"]
    return Backend(plan, runner)


class Backend:
    """Translate a validated plan into bounded backend operations with ownership checks."""

    def __init__(self, plan: Plan, runner: Callable):
        """Inject process I/O so lifecycle tests exercise real serialization without a cluster."""

        self.plan = plan
        self.runner = runner

    def _kubectl(self, arguments: list[str], payload: dict | None = None) -> str:
        """Always select the reviewed context and namespace, regardless of kubectl defaults."""

        target = self.plan.deployment
        return self.runner(["kubectl", "--context", target.context, "--namespace", target.namespace,
                            "--request-timeout=30s", *arguments], json.dumps(payload) if payload else "")

    def _compose(self, arguments: list[str], document: dict) -> str:
        """Pass configuration on stdin so resolved credentials never enter generated files."""

        return self.runner(["docker", "compose", "--env-file", os.devnull, "--project-name", self.plan.identity, "--file", "-", *arguments], json.dumps(document))

    def apply(self, secrets: dict[str, str], previous: dict) -> None:
        """Wait for health before reporting readiness and prune only previously owned workloads."""

        if self.plan.deployment.backend == "local":
            self._check_local_ownership()
            if self.plan.workloads:
                self._compose(["up", "--detach", "--wait", "--wait-timeout", "600", "--remove-orphans"], self.plan.compose(secrets))
            elif previous.get("resources"):
                self.control("remove", previous["resources"])
            return
        for name in self.plan.deployment.order():
            if name in self.plan.workloads:
                self._apply_component(name, secrets)
        obsolete = previous.get("resources", {}).keys() - self.plan.workloads.keys()
        self.control("remove", {name: previous["resources"][name] for name in obsolete})

    def _apply_component(self, name: str, secrets: dict[str, str]) -> None:
        """Refuse resource adoption; use server-side apply to avoid secret annotations."""

        objects = self.plan.kubernetes(name, secrets)
        for item in objects:
            self._owned(item["kind"], item["metadata"]["name"])
        for item in objects:
            self._kubectl(["apply", "--server-side", "--field-manager=harnest-provisioner", "-f", "-"], item)
        self._kubectl(["rollout", "status", "deployment/" + self.plan.resource_name(name), "--timeout=600s"])
        if not self.plan.workloads[name].ports:
            resource = self.plan.resource_name(name)
            if self._owned("Service", resource):
                self._kubectl(["delete", "Service", resource, "--ignore-not-found"])


    def _owned(self, kind: str, name: str) -> bool:
        """Check exact resource ownership before modifying, scaling, or deleting it."""

        text = self._kubectl(["get", kind, name, "--ignore-not-found", "-o", "json"])
        if not text.strip():
            return False
        item = json.loads(text)
        if item.get("metadata", {}).get("labels", {}).get(OWNER_LABEL) != self.plan.identity:
            raise ProvisionError("Refusing to modify a resource not owned by this deployment")
        return True

    def control(self, operation: str, resources: dict) -> None:
        """Never delete PVCs, namespaces, or externally connected infrastructure."""

        if not resources:
            return
        if self.plan.deployment.backend == "local":
            self._check_local_ownership()
            args = ["stop"] if operation == "stop" else ["down", "--remove-orphans"]
            self._compose(args, self._compose_inventory(resources))
            return
        for name in resources:
            self._control_component(operation, name)

    def _control_component(self, operation: str, name: str) -> None:
        """Scale only Deployments; delete workload resources individually after ownership checks."""

        resource = self.plan.resource_name(name)
        kinds = ["Deployment"] if operation == "stop" else ["Deployment", "Service", "Secret"]
        for kind in kinds:
            if not self._owned(kind, resource):
                continue
            args = ["scale", "deployment", resource, "--replicas=0"] if operation == "stop" else ["delete", kind, resource, "--ignore-not-found", "--wait=true", "--timeout=60s"]
            self._kubectl(args)

    def _check_local_ownership(self) -> None:
        """Refuse to adopt unrelated containers or named volumes sharing the generated project name."""

        text = self.runner(["docker", "ps", "--all", "--filter", "label=com.docker.compose.project=" + self.plan.identity, "--format", "{{.ID}}"], "")
        identities = text.split()
        if identities:
            labels = self.runner(["docker", "inspect", "--format", "{{json .Config.Labels}}", *identities], "")
            for line in labels.splitlines():
                self._check_labels(json.loads(line))
        for name, node in self.plan.workloads.items():
            if node.persistence:
                self._check_local_volume(name)

    def _check_local_volume(self, name: str) -> None:
        """Never attach a preexisting unowned data volume to a newly provisioned container."""

        volume = self.plan.resource_name(name) + "-data"
        text = self.runner(["docker", "volume", "ls", "--filter", "name=" + volume, "--format", "{{.Name}}"], "")
        if volume in text.splitlines():
            labels = self.runner(["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume], "")
            self._check_labels(json.loads(labels))

    def _check_labels(self, labels: dict | None) -> None:
        """Container and volume ownership use the same identity as Kubernetes objects."""

        if not labels or labels.get(OWNER_LABEL) != self.plan.identity:
            raise ProvisionError("Refusing to modify a resource not owned by this deployment")

    def _compose_inventory(self, resources: dict) -> dict:
        """A credential-free document is sufficient for status and teardown of a known project."""

        return {"name": self.plan.identity, "services": {name: {"image": image} for name, image in resources.items()}}

    def status(self, resources: dict) -> list[dict]:
        """Project only names and readiness so backend command lines and credentials stay private."""

        if not resources:
            return []
        if self.plan.deployment.backend == "local":
            text = self._compose(["ps", "--all", "--format", "json"], self._compose_inventory(resources)).strip()
            items = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines()]
            return [{"name": item.get("Service"), "state": item.get("State"), "health": item.get("Health")} for item in items]
        text = self._kubectl(["get", "deployments", "-l", f"{OWNER_LABEL}={self.plan.identity}", "-o", "json"])
        return [{"name": item["metadata"]["name"], "desired": item.get("spec", {}).get("replicas", 0),
                 "ready": item.get("status", {}).get("readyReplicas", 0)} for item in json.loads(text).get("items", [])]


def runtime_status(components: list[dict], state: dict) -> str:
    """Distinguish live readiness from the outcome of the last journaled operation."""

    if not state["resources"]:
        return state["status"]
    if not components:
        return "missing"
    if not complete_inventory(components, state):
        return "degraded"
    if all(component_ready(item) for item in components):
        return "ready" if state["status"] == "ready" else "incomplete"
    if all(component_stopped(item) for item in components):
        return "stopped"
    return "degraded"


def component_ready(item: dict) -> bool:
    """Require actual health locally and every requested replica on Kubernetes."""

    if "desired" in item:
        return item["desired"] > 0 and item["ready"] == item["desired"]
    return item.get("state") == "running" and item.get("health") == "healthy"


def component_stopped(item: dict) -> bool:
    """Treat stopped replicas explicitly, without conflating an unhealthy pod with a stopped one."""

    if "desired" in item:
        return item["desired"] == 0
    return item.get("state") in {"exited", "stopped"}


def complete_inventory(components: list[dict], state: dict) -> bool:
    """Do not report readiness when a workload or one of its local replicas disappeared."""

    from collections import Counter
    actual = Counter(item["name"] for item in components)
    summary = state["summary"]
    if summary["backend"] == "kubernetes":
        return set(actual) == {summary["identity"] + "-" + name for name in state["resources"]}
    expected = {item["name"]: item.get("replicas", 1) for item in summary["components"] if item["mode"] == "provision"}
    return actual == expected


def committed_state(operation: str, state: dict) -> dict:
    """Advance the recovery inventory only after the backend operation succeeds."""

    final = {**state, "status": {"apply": "ready", "rollback": "ready", "stop": "stopped", "remove": "removed"}[operation], "operation": operation}
    if operation == "remove":
        final["resources"] = {}
        final["active_revision"] = None
    elif operation in {"apply", "rollback"}:
        final["resources"] = final.pop("desired")
        final["active_revision"] = final["attempt_revision"]
    return final


def check_persistent_rollback(current: Deployment, target: Deployment) -> None:
    """Do not silently downgrade databases, remount their data, or change their initialization settings."""

    names = {name for deployment in (current, target) for name, node in deployment.services.items() if getattr(node, "persistence", None)}
    for name in names:
        if current.services.get(name) != target.services.get(name):
            raise ProvisionError("Rollback changes a persistent service; reconcile database compatibility explicitly first")
