"""Resolve company builder defaults without mutating process-wide provider state."""

import os
from pathlib import Path, PurePosixPath
import ssl
import tempfile
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .files import LIMIT, canonical_source, linked_path

ENVIRONMENT = {
    "model": "HARNEST_BUILDER_MODEL", "api_base": "HARNEST_BUILDER_API_BASE",
    "timeout": "HARNEST_BUILDER_TIMEOUT_SECONDS", "max_tokens": "HARNEST_BUILDER_MAX_TOKENS",
}


class BuilderDefaults(BaseModel):
    """Allow partial company defaults and secret references, never embedded credentials."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_base: str | None = Field(default=None, max_length=2048)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ca_bundle: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout: int | None = Field(default=None, ge=1, le=1800)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)

    @field_validator("api_base")
    @classmethod
    def endpoint(cls, value):
        """Keep credentials out of distributable URLs and require an HTTP endpoint."""
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Builder api_base must be an HTTP(S) URL without credentials, query, or fragment")
        return value


def validate_ca(text: str) -> None:
    """Accept only certificate PEM blocks so private keys cannot enter company wheels."""
    import re
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.DOTALL)
    remainder = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for block in blocks:
        remainder = remainder.replace(block, "")
    if not blocks or remainder.strip():
        raise ValueError("Builder CA bundle must contain only PEM certificates")
    try:
        ssl.create_default_context(cadata=text)
    except ssl.SSLError as error:
        raise ValueError("Invalid builder CA certificates") from error


def read_ca(root: Path, relative: str) -> str:
    """Bound and freeze a pack-relative certificate without widening source-file access."""
    parts = PurePosixPath(relative)
    if not canonical_source(relative, parts) or ".." in parts.parts or linked_path(root, parts):
        raise ValueError("Builder CA bundle must be an unlinked file inside the pack")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("Builder CA bundle must stay inside the pack")
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Builder CA bundle exceeds 1 MiB")
    text = data.decode("utf-8")
    validate_ca(text)
    return text


class BuilderConfiguration:
    """Own app-scoped defaults and CA snapshots for the lifetime of its assistant process."""

    def __init__(self, packs):
        """Overlay packs in launch order, then honor explicit local environment settings."""
        defaults, ca = {}, None
        for pack in packs.packs.values():
            values = {key: value for key, value in pack["manifest"]["builder"].items() if value is not None}
            defaults.update(values)
            if "ca_bundle" in values:
                ca = pack["builder_ca"]
        self.directory = tempfile.TemporaryDirectory(prefix="harnest-studio-ca-")
        try:
            self.environment = self._environment(defaults, ca)
        except BaseException:
            self.close()
            raise

    def _environment(self, defaults, ca):
        """Resolve credentials only at launch and keep packaged trust independent of publisher paths."""
        environment = {ENVIRONMENT[key]: str(value) for key, value in defaults.items() if key in ENVIRONMENT}
        key_name = defaults.get("api_key_env")
        if key_name and os.getenv(key_name):
            environment["HARNEST_BUILDER_API_KEY"] = os.environ[key_name]
        if ca:
            target = Path(self.directory.name) / "ca.pem"
            target.write_text(ca)
            environment["HARNEST_BUILDER_CA_BUNDLE"] = str(target)
        environment.update({key: value for key, value in os.environ.items() if key.startswith("HARNEST_BUILDER_")})
        if environment.get("HARNEST_BUILDER_CA_BUNDLE"):
            path = Path(environment["HARNEST_BUILDER_CA_BUNDLE"]).expanduser().resolve()
            validate_ca(path.read_text())
            environment["HARNEST_BUILDER_CA_BUNDLE"] = str(path)
        return environment

    def close(self):
        """Remove trust snapshots only after the owning assistant has stopped."""
        self.directory.cleanup()
