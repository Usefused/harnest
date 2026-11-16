import importlib
import importlib.metadata
import json
import platform
import sys

# argv[1] carries the selected framework's modules from frameworks.json.
framework_modules = json.loads(sys.argv[1])
dependencies = [
    ("harnest", "harnest"),
    ("litellm", "litellm"),
    ("opentelemetry.sdk", "opentelemetry-sdk"),
    ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry-exporter-otlp-proto-http"),
    ("opentelemetry.instrumentation.fastapi", "opentelemetry-instrumentation-fastapi"),
    ("opentelemetry.instrumentation.logging", "opentelemetry-instrumentation-logging"),
]
dependencies.extend((item["module"], item["distribution"]) for item in framework_modules)

checks = []
for module, distribution in dependencies:
    try:
        importlib.import_module(module)
        checks.append({
            "name": distribution,
            "ok": True,
            "version": importlib.metadata.version(distribution),
            "error": "",
        })
    except Exception as exc:
        checks.append({
            "name": distribution,
            "ok": False,
            "version": "",
            "error": f"{type(exc).__name__}: {exc}",
        })

print(json.dumps({
    "executable": sys.executable,
    "python": platform.python_version(),
    "supported": sys.version_info >= (3, 11),
    "packages": checks,
}, separators=(",", ":")))
