'cc capability'

from harnest.extensions import Extension


class CcExtension(Extension):
    """Own application-scoped behavior without importing a framework."""


extension = CcExtension()
