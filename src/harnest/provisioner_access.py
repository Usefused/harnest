"""Credential-free access instructions derived from provisioned agent ports."""

import shlex


def agent_access(plan) -> list[dict]:
    """Describe configured access without mistaking internal service DNS for a public endpoint."""
    return [_agent(plan, name, node) for name, node in plan.deployment.agents.items()]


def _agent(plan, name, node) -> dict:
    """Keep agents without published ports explicit instead of fabricating reachable links."""
    ports = [_port(plan, name, label, port, node.publish.get(label)) for label, port in node.ports.items()]
    return {"name": name, "ports": ports,
            "note": "No network ports configured. Add a port to expose this agent." if not ports else ""}


def _port(plan, name, label, container_port, host_port) -> dict:
    """Expose loopback bindings locally and copyable port-forward instructions for Kubernetes."""
    protocol = label if label in {"http", "https"} else None
    result = {"name": label, "container_port": container_port, "protocol": protocol, "url": None,
              "host": None, "port": None, "command": None}
    if plan.deployment.backend == "local":
        if host_port is None:
            return {**result, "scope": "internal", "note": f"Publish the {label} port in harnest-deployment.yaml to access it from this computer."}
        return {**result, "scope": "local", "host": "127.0.0.1", "port": host_port,
                "url": f"{protocol}://127.0.0.1:{host_port}" if protocol else None,
                "note": "Available on the computer running Docker. Routes and authentication are defined by the agent image."}
    # This command is presented, never executed automatically. Keep the reviewed
    # context and namespace explicit and select an unprivileged local port.
    local_port = container_port if container_port >= 1024 else 10000 + container_port
    command = ["kubectl", "--context", plan.deployment.context, "--namespace", plan.deployment.namespace,
               "port-forward", "--address", "127.0.0.1", "service/" + plan.resource_name(name), f"{local_port}:{container_port}"]
    return {**result, "scope": "port-forward", "host": "127.0.0.1", "port": local_port,
            "url": f"{protocol}://127.0.0.1:{local_port}" if protocol else None,
            "command": shlex.join(command),
            "note": "Run this command on your computer and keep it running, then use the local endpoint. Change the left-hand port if it is occupied. No public ingress is created."}
