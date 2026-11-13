"""Select compiler context for isolated framework boundary tests."""

from harnest.bundle import bundle_agent
from harnest.frameworks.context import framework_scope


def build_for_framework(value, framework):
    """Exercise the public no-argument builder in a compiler-owned scope."""

    with framework_scope(framework):
        return value.build()


def bundle_for_framework(anchor, definition, *, framework):
    """Exercise resource discovery without running the full compiler in unit tests."""

    with framework_scope(framework):
        return bundle_agent(anchor, definition)
