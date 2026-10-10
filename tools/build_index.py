#!/usr/bin/env python3
"""Builds INDEX.json from the ports tree.

Reads every ports/<kind>/<slug>/manifest.json, checks what a schema alone
can't (the directory matches the manifest, secrets are declared before they're
referenced, a plugin's entry exists), hashes each port directory, and writes
one sorted row per port. Nothing in a port is executed.

    tools/build_index.py           # write INDEX.json
    tools/build_index.py --check   # fail if INDEX.json is stale or a port is invalid

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTS = ROOT / "ports"
INDEX = ROOT / "INDEX.json"

KINDS = ("plugin", "mcp", "skill", "acp")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SECRET_REF = re.compile(r"\$\{secret:([a-z][a-z0-9_]*)\}")

# What the store needs before a port is fetched; everything else stays in
# the manifest, fetched with the port.
INDEXED_FIELDS = (
    "name", "version", "summary", "vendor", "category", "symbol", "tint",
    "platforms", "contributors",
)


def port_hash(directory: Path) -> str:
    """sha256 over the sorted (path, file sha256) pairs of a port directory."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def secret_references(value: object) -> set[str]:
    if isinstance(value, str):
        return set(SECRET_REF.findall(value))
    if isinstance(value, dict):
        return set().union(*(secret_references(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(secret_references(v) for v in value))
    return set()


def check(kind: str, slug: str, directory: Path, manifest: dict) -> list[str]:
    problems = []
    if not SLUG.match(slug):
        problems.append(f"directory name {slug!r} is not a lowercase slug")
    if manifest.get("kind") != kind:
        problems.append(f"kind {manifest.get('kind')!r} doesn't match its directory {kind!r}")
    if kind not in manifest:
        problems.append(f"missing its {kind!r} section")

    declared = {secret["key"] for secret in manifest.get("secrets", [])}
    for key in sorted(secret_references(manifest.get(kind)) - declared):
        problems.append(f"references ${{secret:{key}}} but doesn't declare it")

    fetched = {item["into"] for item in manifest.get("fetch", [])}
    if kind == "plugin":
        entry = manifest.get("plugin", {}).get("entry")
        if entry and entry not in fetched and not (directory / entry).is_file():
            problems.append(f"plugin entry {entry!r} is neither in the port nor fetched")
    if kind == "skill":
        root = manifest.get("skill", {}).get("root")
        if root and not (directory / root / "SKILL.md").is_file():
            problems.append(f"skill root {root!r} has no SKILL.md")
    return problems


def build() -> tuple[dict, list[str]]:
    rows, problems = [], []
    for kind in KINDS:
        for directory in sorted(d for d in (PORTS / kind).glob("*") if d.is_dir()):
            slug = directory.name
            port_id = f"{kind}/{slug}"
            try:
                manifest = json.loads((directory / "manifest.json").read_text())
            except (OSError, ValueError) as error:
                problems.append(f"{port_id}: unreadable manifest.json ({error})")
                continue
            problems += [f"{port_id}: {p}" for p in check(kind, slug, directory, manifest)]
            row = {"id": port_id, "kind": kind}
            row.update({k: manifest[k] for k in INDEXED_FIELDS if k in manifest})
            if manifest.get("secrets"):
                row["secrets"] = [s["label"] for s in manifest["secrets"]]
            row["path"] = directory.relative_to(ROOT).as_posix()
            row["sha256"] = port_hash(directory)
            rows.append(row)
    stray = [p for p in PORTS.iterdir() if p.is_dir() and p.name not in KINDS]
    problems += [f"{p.name}: not a port kind" for p in stray]
    return {"ports": rows}, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if INDEX.json is stale or a port is invalid")
    args = parser.parse_args()

    index, problems = build()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1

    rendered = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not INDEX.is_file() or INDEX.read_text() != rendered:
            print("error: INDEX.json is stale; run tools/build_index.py", file=sys.stderr)
            return 1
        print(f"INDEX.json is current ({len(index['ports'])} ports)")
        return 0
    INDEX.write_text(rendered)
    print(f"wrote INDEX.json ({len(index['ports'])} ports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
