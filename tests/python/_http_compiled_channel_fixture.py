"""Create an authenticated, deterministic agent for real channel transport tests."""

from pathlib import Path
import textwrap


def write_agent(root: Path) -> None:
    """Exercise the real runtime and approval boundary without model/provider credentials."""
    (root / "lifecycle").mkdir(parents=True)
    (root / "agent-card.yaml").write_text("name: Channel test\ndescription: Deterministic channel acceptance\nversion: 0.1.0\n")
    (root / "agent.py").write_text(textwrap.dedent('''
        from harnest.graph import START, Edge, Event, Graph
        from harnest.agent.approval import request_human_approval

        async def respond(value):
            """Return a deterministic response through the compiled framework."""
            if value == "approval":
                async with request_human_approval(action="channel.test", message="Approval required", arguments={}):
                    return Event(output="approved", message="approved")
            return Event(output=value, message=value)

        root_agent = Graph(name="channel_test", nodes={"respond": respond}, edges=(Edge(START, "respond"),))
    ''').lstrip())
    (root / "lifecycle" / "authentication.py").write_text(textwrap.dedent('''
        import hmac
        from harnest import lifecycle
        from harnest.auth import AuthPrincipal, AuthenticationError

        @lifecycle.authenticate
        def authenticate(connection, principal):
            """Bind only a credentialed worker's explicit actor to runtime authority."""
            token = connection.headers.get("authorization", "")
            actor = connection.headers.get("x-channel-actor", "")
            if not hmac.compare_digest(token, "Bearer test-worker") or not actor:
                raise AuthenticationError()
            return AuthPrincipal(actor)
    ''').lstrip())
