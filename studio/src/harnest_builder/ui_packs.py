"""Resolve immutable UI packs without granting publisher code implicit host authority."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response

from .pack_manifest import load_pack

DEFAULT_ROOT = Path(__file__).parent / "ui_packs/default"
DEFAULT_PACK = "fused-studio"
FRAME_POLICY = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts"
MEDIA = {".js": "text/javascript", ".css": "text/css", ".json": "application/json"}


class UIPacks:
    """Use the ordinary pack loader for Fused's UI and every custom contribution."""

    def __init__(self, packs, trusted=()):
        """Reserve the bundled identity and keep launch-time code trust outside manifests."""
        self.default = load_pack(DEFAULT_ROOT)
        if DEFAULT_PACK in packs.packs:
            raise ValueError("The bundled fused-studio pack identity is reserved")
        self.packs = {DEFAULT_PACK: self.default, **packs.packs}
        unknown = set(trusted) - self.packs.keys()
        if unknown:
            raise ValueError("Unknown trusted UI pack: " + ", ".join(sorted(unknown)))
        self.trusted = {DEFAULT_PACK, *trusted}

    def catalog(self, safe=False):
        """Resolve explicit replacements and disable executable entries lacking host trust."""
        packs = {DEFAULT_PACK: self.default} if safe else self.packs
        result = {"apiVersion": "harnest.dev/studio-ui/v1", "themes": [], "layouts": [], "contributions": [], "styles": [], "packs": []}
        for identity, pack in packs.items():
            self._contribute(result, identity, pack)
        result["contributions"] = resolve_contributions(result["contributions"])
        return result

    def _contribute(self, result, identity, pack):
        """Qualify local IDs once so consumers never infer ownership from load order."""
        ui = pack["manifest"].get("ui")
        if ui is None:
            return
        trusted = identity in self.trusted
        base = f"/ui-assets/{identity}/{pack['digest']}/"
        result["packs"].append({"id": identity, "version": pack["manifest"]["version"], "digest": pack["digest"], "trusted": trusted})
        for kind in ("themes", "layouts", "contributions"):
            for item in ui[kind]:
                if kind == "contributions" and item["mode"] == "module" and not trusted:
                    continue
                result[kind].append({**item, "id": identity + "/" + item["id"], "pack": identity, "base": base})
        if trusted:
            result["styles"].extend(base + path for path in ui["styles"])

    def asset(self, identity, revision, relative):
        """Serve only immutable declared assets; sandbox HTML even on direct navigation."""
        pack = self.packs.get(identity)
        if pack is None or revision != pack["digest"] or relative not in pack["ui_assets"]:
            raise HTTPException(404, "UI asset not found")
        text = pack["ui_assets"][relative]
        ui = pack["manifest"]["ui"]
        framed = any(item["entry"] == relative and item["mode"] == "frame" for item in ui["contributions"])
        if framed:
            return Response(text, media_type="text/html", headers={"Content-Security-Policy": FRAME_POLICY})
        if identity not in self.trusted:
            raise HTTPException(403, "Executable UI assets require explicit launch-time trust")
        # Shell fragments are text, so opening their URL cannot execute arbitrary HTML.
        return Response(text, media_type=MEDIA.get(Path(relative).suffix, "text/plain"))


def resolve_contributions(items):
    """Reject ambiguous replacements, cycles, slot changes, and multiple shell owners."""
    by_id = {item["id"]: item for item in items}
    replaced = {}
    for item in items:
        target = item["replaces"]
        if target is None:
            continue
        if target not in by_id or by_id[target]["slot"] != item["slot"]:
            raise ValueError("Replacement must name an existing contribution in the same slot: " + target)
        if target in replaced:
            raise ValueError("Conflicting UI replacements for " + target)
        replaced[target] = item["id"]
    _check_cycles(replaced)
    active = [item for item in items if item["id"] not in replaced]
    _check_owners(active)
    return active


def _check_owners(active):
    """Require one shell and one welcome view after applying replacement chains."""
    for slot in ("shell", "welcome"):
        if sum(item["slot"] == slot for item in active) != 1:
            raise ValueError("Exactly one UI contribution must own " + slot)


def _check_cycles(replaced):
    """Check replacement chains independently of pack discovery order."""
    for start in replaced:
        seen = set()
        current = start
        while current in replaced:
            if current in seen:
                raise ValueError("Cyclic UI replacement: " + current)
            seen.add(current)
            current = replaced[current]
