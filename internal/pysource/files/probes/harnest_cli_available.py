from importlib.util import find_spec
import sys
if find_spec("harnest") is None or find_spec("harnest.cli") is None:
    sys.exit(42)
