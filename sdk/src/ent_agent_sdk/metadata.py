"""Build metadata (SDK-14): which SDK and policy-bundle versions an agent image was built with.

Written into the image at build time (`python -m ent_agent_sdk.metadata write`) and read back at runtime or by CI:

    docker run --rm <image> python -m ent_agent_sdk.metadata read

Values come from build arguments exposed as environment variables:
`ENT_POLICY_VERSION`, `ENT_GIT_SHA`, `ENT_BUILD_TIME`. The same fields are attached to every trace as resource
attributes, so traces say which SDK/policy version produced them.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

DEFAULT_PATH = "/app/.ent/metadata.json"
TRACKED_PACKAGES = (
    "langgraph", "langchain-core", "langchain-openai", "openai", "fastapi", "uvicorn", "boto3",
    "opentelemetry-sdk",
)


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def collect() -> dict[str, Any]:
    """Metadata for the running environment."""
    return {
        "sdk": {"name": "ent-agent-sdk", "version": _version("ent-agent-sdk") or "unknown"},
        "policy_bundle_version": os.environ.get("ENT_POLICY_VERSION", "unknown"),
        "git_sha": os.environ.get("ENT_GIT_SHA", "unknown"),
        "build_time": os.environ.get("ENT_BUILD_TIME", "unknown"),
        "python": platform.python_version(),
        "frameworks": {package: _version(package) for package in TRACKED_PACKAGES},
    }


def write(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    """Write the metadata file (used while building an image)."""
    data = collect()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def read(path: str | Path | None = None) -> dict[str, Any]:
    """The baked-in metadata if present, otherwise the current environment's."""
    target = Path(path or os.environ.get("ENT_METADATA_PATH", DEFAULT_PATH))
    if target.is_file():
        return json.loads(target.read_text(encoding="utf-8"))
    return collect()


def resource_attributes() -> dict[str, str]:
    """Trace resource attributes for the known fields."""
    data = read()
    attributes = {
        "ent.policy.version": data.get("policy_bundle_version"),
        "ent.git.sha": data.get("git_sha"),
        "ent.build.time": data.get("build_time"),
    }
    return {k: v for k, v in attributes.items() if v and v != "unknown"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ent_agent_sdk.metadata")
    sub = parser.add_subparsers(dest="command", required=True)
    write_parser = sub.add_parser("write", help="write the metadata file")
    write_parser.add_argument("path", nargs="?", default=DEFAULT_PATH)
    sub.add_parser("read", help="print the metadata as JSON")
    args = parser.parse_args(argv)
    data = write(args.path) if args.command == "write" else read()
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
