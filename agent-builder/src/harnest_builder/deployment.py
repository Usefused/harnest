"""Discover deployment inputs without executing source and propose native manifests."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
import yaml

from harnest.provisioner_config import Deployment, ManifestLoader, ProvisionError, parse_manifest
from harnest.provisioner_plan import Plan
from .files import inventory, read, source_path, validate

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUES = {"OPENAI_MODEL", "OLLAMA_MODEL", "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"}


class Setup(BaseModel):
    """Require an explicit deployment target and image before proposing any files."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    image: str = Field(min_length=1, max_length=300)
    backend: Literal["local", "kubernetes"] = "local"
    context: str = Field(default="", max_length=200)
    namespace: str = Field(default="", max_length=40)
    port: int = Field(default=1907, ge=1024, le=65535)
    variables: list[str] = Field(default_factory=list, max_length=64)
    memory: str = Field(default="512Mi", pattern=r"^[1-9][0-9]*(Mi|Gi)$")
    cpus: float = Field(default=1.0, gt=0, le=128)
    services_yaml: str | None = Field(default=None, max_length=20000)
    network_yaml: str = Field(default="{}", max_length=4096)


def _literal(node) -> str:
    """Read only string literals, never evaluate authored expressions or imports."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _environment_key(node) -> str:
    """Recognize common literal getenv/environ forms, including imported aliases."""
    if isinstance(node, ast.Call) and node.args:
        target = ast.unparse(node.func)
        if target.endswith(("getenv", "environ.get")):
            return _literal(node.args[0])
    if isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
        return _literal(node.slice)
    return ""


def _python_inputs(text: str) -> tuple[set[str], set[str]]:
    """Collect explicit environment reads and stdio programs from parseable source."""
    variables = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
    programs = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return variables, programs
    for node in ast.walk(tree):
        key = _environment_key(node)
        if ENV_NAME.fullmatch(key):
            variables.add(key)
        if isinstance(node, ast.keyword) and node.arg == "command" and _literal(node.value):
            programs.add(_literal(node.value))
    return variables, programs


def inspect_project(root: Path) -> dict:
    """Describe statically visible needs; hide environment values and report inference limits."""
    config = _configuration(root)
    configured = config.get("spec", {}).get("environment", {})
    variables, programs, sources = set(configured), set(), []
    contents = []
    total = 0
    for path in inventory(root):
        if _runtime_python(path):
            text = _discovery_source(root, path, config)
            total += len(text.encode())
            _check_scan_limit(total)
            keys, commands = _python_inputs(text)
            variables.update(keys)
            programs.update(commands)
            contents.append(text)
            if keys or commands:
                sources.append(path)
    combined = "\n".join(contents)
    if "from_openai_environment" in combined:
        variables.update(("OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"))
    variables.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", yaml.safe_dump(config)))
    services = _services(variables, combined, configured)
    warnings = ["Static discovery cannot resolve dynamic imports or computed environment names. Review the variable list and image requirements."]
    if programs:
        warnings.append("The agent image must include these stdio MCP executables and their dependencies: " + ", ".join(sorted(programs)) + ".")
    if any("127.0.0.1" in str(value) or "localhost" in str(value) for value in configured.values()):
        warnings.append("Localhost endpoints refer to the container itself. Supply service URLs reachable from the deployment.")
    return {"name": config.get("metadata", {}).get("name", root.name), "variables": sorted(variables),
            "services": services, "programs": sorted(programs), "sources": sources, "warnings": warnings,
            "services_yaml": yaml.safe_dump(_connections(services), sort_keys=False),
            "resources": {},
            "existing": bool(read(root, "harnest-deployment.yaml")["revision"])}


def _discovery_source(root: Path, path: str, config: dict) -> str:
    """Keep ignored hints out of inferred environment variables and service requirements."""
    return yaml.safe_dump(config) if path == "config.yaml" else read(root, path)["text"]


def _check_scan_limit(total: int) -> None:
    """Bound analysis across root, subagents and extension code without truncating silently."""
    if total > 2_000_000:
        raise HTTPException(413, "Deployment discovery is limited to 2 MiB of Python source.")


def _runtime_python(path: str) -> bool:
    """Include declarative extension/plugin references but exclude test fixtures."""
    return path.endswith((".py", ".yaml", ".yml", ".toml")) and not path.startswith("tests/")


def _configuration(root: Path) -> dict:
    """Keep malformed project configuration actionable instead of raising an internal error."""
    try:
        config = yaml.safe_load(read(root, "config.yaml")["text"])
        if not isinstance(config, dict) or not isinstance(config.get("spec", {}), dict):
            raise ValueError("mapping required")
        # Retired hints must not seed limits or environment-variable discovery.
        config.get("spec", {}).pop("resources", None)
        config.get("spec", {}).pop("scaling", None)
        for section in (config.get("metadata", {}), config.get("spec", {}).get("environment", {})):
            if not isinstance(section, dict):
                raise ValueError("mapping required")
        return config
    except (yaml.YAMLError, ValueError):
        raise HTTPException(422, "Fix config.yaml before discovering deployment inputs.") from None


def _connections(services: list[dict]) -> dict:
    """Seed readable external bindings without copying endpoints or credentials."""
    return {service["name"]: {"mode": "connect", "type": service["type"],
            "provides": {key: {"secret": key} for key in service["variables"]}} for service in services}


def _services(variables: set[str], source: str, configured: dict) -> list[dict]:
    """Report service hints without assuming optional backends must be provisioned."""
    result = []
    if "DATABASE_URL" in variables:
        result.append({"name": "database", "type": "postgres" if "PostgresStore" in source else "database", "variables": ["DATABASE_URL"]})
    if "REDIS_URL" in variables:
        result.append({"name": "redis", "type": "redis", "variables": ["REDIS_URL"]})
    if "11434" in str(configured) or "ollama" in source.lower():
        keys = sorted(variables & {"OPENAI_BASE_URL", "OPENAI_API_KEY", "OLLAMA_API_BASE"})
        if keys:
            result.append({"name": "ollama", "type": "ollama", "variables": keys})
    return result


def _container(root: Path, body: Setup, inspection: dict) -> tuple[dict, dict]:
    """Wire known external services and explicit runtime variables without copying credentials."""
    configured = _configuration(root).get("spec", {}).get("environment", {})
    variables = set(body.variables)
    if any(not ENV_NAME.fullmatch(key) for key in variables):
        raise HTTPException(422, "Use environment variable names, not values.")
    environment = {key: _binding(key, configured) for key in sorted(variables)}
    services = _mapping(body.services_yaml) if body.services_yaml is not None else _connections(inspection["services"])
    for service in services.values():
        if not isinstance(service, dict):
            raise HTTPException(422, "Each service must be a mapping with mode: connect or provision.")
        keys = _service_variables(service)
        for key in keys:
            environment.pop(key, None)
    agent = {"image": body.image, "ports": {"http": body.port}, "environment": environment,
             "depends_on": list(services), "resources": {"cpus": body.cpus, "memory": body.memory},
             "network": _mapping(body.network_yaml),
             "healthcheck": {"command": ["python", "-c", f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{body.port}/health', timeout=2)"]}}
    # The image owns its entrypoint and startup command. Guessing these can break
    # otherwise valid images; surface the bind/port contract explicitly in review.
    if body.backend == "local":
        agent["publish"] = {"http": body.port}
    return agent, services


def _service_variables(service: dict) -> set[str]:
    """Reject malformed bindings before using service keys to deduplicate the agent environment."""
    provides, variable = service.get("provides", {}), service.get("variable", "")
    if not isinstance(provides, dict) or not isinstance(variable, str):
        raise HTTPException(422, "Service provides must be a mapping and variable must be an environment name.")
    return set(provides) | {variable}


def _mapping(text: str) -> dict:
    """Validate the small user-editable YAML sections before composing the native manifest."""
    try:
        result = yaml.load(text, Loader=ManifestLoader)
        if not isinstance(result, dict):
            raise ValueError("mapping required")
        return result
    except (yaml.YAMLError, ValueError, TypeError):
        raise HTTPException(422, "Services and network settings must each contain one YAML mapping with unique keys.") from None


def _binding(key: str, configured: dict):
    """Keep a small allowlist of non-secret settings; reference all other values at apply time."""
    value = configured.get(key)
    if key in SAFE_VALUES and isinstance(value, str) and not value.startswith("${"):
        return value
    return {"secret": key}


def generate(root: Path, body: Setup) -> dict:
    """Use the native schema and renderer, returning changes for normal diff/revision review."""
    inspection = inspect_project(root)
    agent, services = _container(root, body, inspection)
    data = {"version": 1, "name": body.name, "backend": body.backend, "services": services, "agents": {body.name: agent}}
    if body.backend == "kubernetes":
        data.update(context=body.context, namespace=body.namespace)
    text = yaml.safe_dump(data, sort_keys=False)
    try:
        deployment = parse_manifest(text)
        plan = Plan(deployment, "local", str(root))
    except ProvisionError as error:
        raise HTTPException(422, str(error)) from error
    changes = _preserved_manifest(root)
    changes.append(_change(root, "harnest-deployment.yaml", MANIFEST_HELP + text))
    if body.backend == "kubernetes":
        changes.append(_change(root, "deploy/kubernetes.yaml", kubernetes_preview(plan)))
    required = sorted({value.secret for value in _all_bindings(deployment) if hasattr(value, "secret")})
    notes = [f"Set these environment variables in the Studio/CLI process before applying: {', '.join(required) or 'none'}.",
             f"The agent image must contain the compiled agent and dependencies, start its server on 0.0.0.0:{body.port}, and include Python for the health probe.",
             "Services with mode: connect remain externally owned; mode: provision runs the declared image.",
             *inspection["warnings"]]
    return {"summary": "Deployment configuration ready for review. " + " ".join(notes), "files": changes,
            "plan": plan.summary(), "required_variables": required, "inspection": inspection}


def _all_bindings(deployment: Deployment) -> list:
    """Share required-variable discovery across agent and external-service bindings."""
    result = []
    for node in deployment.workloads().values():
        result.extend(node.environment.values())
    for node in deployment.services.values():
        result.extend((node.bindings() if hasattr(node, "bindings") else node.provides).values())
    return result


def _preserved_manifest(root: Path) -> list[dict]:
    """Preserve mistaken Kubernetes input and refuse replacing hand-authored Harnest settings."""
    current = read(root, "harnest-deployment.yaml")
    if not current["revision"]:
        return []
    try:
        documents = list(yaml.safe_load_all(current["text"]))
    except yaml.YAMLError:
        raise HTTPException(422, "Fix the YAML syntax or move the existing deployment file before generating a configuration.") from None
    if not documents or not all(isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc for doc in documents):
        raise HTTPException(409, "A deployment configuration already exists. Edit it or use Build with AI to update it without replacing your settings.")
    destination = "deploy/original-kubernetes.yaml"
    if read(root, destination)["revision"]:
        raise HTTPException(409, "The original Kubernetes backup already exists; preserve it before regenerating.")
    return [_change(root, destination, current["text"])]


def _change(root: Path, path: str, text: str) -> dict:
    """Keep generated edits on the same bounded, revision-checked path as model proposals."""
    before = read(root, path)
    validate(source_path(root, path), text)
    return {"path": path, "text": text, "revision": before["revision"], "before": before["text"]}


def kubernetes_preview(plan: Plan) -> str:
    """Export workloads without fabricating secret values or resolving the host environment."""
    objects, comments = [], ["# Generated Kubernetes workload preview. Prefer harnest provision plan/apply for managed deployment.",
                            "# Secrets are intentionally omitted. Provisioning creates them from runtime environment bindings.",
                            "# If using kubectl directly, create the named Secrets with ALL listed keys first."]
    for name in plan.workloads:
        environment = plan.environment_for(name, None)
        comments.append(f"# Secret {plan.resource_name(name)}: {', '.join(sorted(environment)) or '(no keys)'}")
        objects.extend(obj for obj in plan.kubernetes(name, None) if obj["kind"] != "Secret")
    return "\n".join(comments) + "\n" + yaml.safe_dump_all(objects, sort_keys=False)


def install_routes(app, workspace) -> None:
    """Expose read-only inspection and proposal generation under Studio's auth boundary."""
    @app.get("/api/deployment/inspect")
    def inspect(project: str):
        """Return inferred inputs without importing the agent or invoking cluster commands."""
        with workspace.lock:
            return inspect_project(workspace.project(project))

    @app.post("/api/deployment/propose")
    def propose(body: Setup):
        """Generate reviewable files; applying them uses the existing batch save route."""
        with workspace.lock:
            return generate(workspace.project(body.project), body)


MANIFEST_HELP = """# Harnest deployment input. Edit this file, then use Deploy → Preview deployment plan.
# agents: compiled images and memory/CPU; subagents and stdio MCP run inside that image.
# services: mode: connect for existing endpoints, mode: provision to run an image.
# environment/provides: literal non-secret settings or {secret: VARIABLE_NAME}.
# Runtime secrets come from the process launching Studio/CLI, never from this file.
# depends_on: service names whose provides bindings are injected into the agent.
# network.hosts: hostname-to-IP overrides (host-gateway is supported locally only).
# network.dns: optional custom resolver IPs; leave empty to retain default service DNS.
# backend: local uses Docker Compose. For Kubernetes set backend: kubernetes,
# plus context and namespace. Kubernetes output belongs in deploy/kubernetes.yaml.
"""
