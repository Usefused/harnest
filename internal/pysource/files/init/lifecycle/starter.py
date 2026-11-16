from harnest import lifecycle


@lifecycle.agent.after
def observe_result(_context, _result):
    """Observe completed invocations without replacing their result."""
