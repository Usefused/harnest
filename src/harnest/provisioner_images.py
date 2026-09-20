"""Freeze mutable image tags before applying or recording a deployment revision."""

from __future__ import annotations

import json
import re

from .provisioner_config import ProvisionError
from .provisioner_plan import Plan

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def pin_plan(plan: Plan, runner) -> tuple[Plan, dict]:
    """Deploy exactly the image identities recorded in history, including locally built images."""

    deployment = plan.deployment.model_copy(deep=True)
    images = {}
    resolved = {}
    for name, node in deployment.workloads().items():
        requested = node.image
        if requested not in resolved:
            resolved[requested] = pin_image(requested, deployment.backend, runner)
        node.image = resolved[requested]
        images[name] = {"requested": requested, "resolved": node.image}
    pinned = Plan(deployment, plan.environment, "")
    pinned.identity = plan.identity
    return pinned, images


def pin_image(image: str, backend: str, runner) -> str:
    """Pinned references bypass lookups; tags resolve once through the selected backend tooling."""

    if "@" in image:
        if not _DIGEST.fullmatch(image.rsplit("@", 1)[1]):
            raise ProvisionError("Image references must use a valid sha256 digest")
        return image
    if _DIGEST.fullmatch(image):
        if backend != "local":
            raise ProvisionError("Kubernetes requires registry image digests, not local image IDs")
        return image
    if backend == "local":
        return _local_image(image, runner)
    try:
        value = json.loads(runner(["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", image], ""))
        digest = value["digest"]
    except (ValueError, KeyError, TypeError):
        raise ProvisionError("Cannot resolve image digest; configure a registry image@sha256:digest") from None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ProvisionError("Registry returned an invalid image digest")
    return image + "@" + digest


def _local_image(image: str, runner) -> str:
    """Use a local content ID so rebuilding the same tag cannot change a historical deployment."""

    args = ["docker", "image", "inspect", "--format", "{{json .Id}}", image]
    try:
        text = runner(args, "")
    except ProvisionError:
        runner(["docker", "pull", image], "")
        text = runner(args, "")
    try:
        identity = json.loads(text)
    except ValueError:
        raise ProvisionError("Cannot resolve the local image ID") from None
    if not isinstance(identity, str) or not _DIGEST.fullmatch(identity):
        raise ProvisionError("Docker returned an invalid image ID")
    return identity
