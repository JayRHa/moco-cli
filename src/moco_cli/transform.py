"""Transform and normalization helpers for the MOCO CLI."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class PathParameter:
    """Single path placeholder with a stable CLI parameter name."""

    token: str
    name: str
    index: int


@dataclass(frozen=True)
class Endpoint:
    """Normalized endpoint representation used by the CLI."""

    key: str
    method: str
    path: str
    section: str
    source_file: str
    source_line: int
    path_parameters: tuple[PathParameter, ...]


def _dig(data: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    """Read nested dict fields safely."""
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _normalize_identifier(value: str) -> str:
    normalized = _SANITIZE_RE.sub("_", value.strip().lower()).strip("_")
    return normalized or "value"


def _singularize(segment: str) -> str:
    """Best-effort singularization for resource names in path params."""
    token = _normalize_identifier(segment)

    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 1:
        return token[:-1]
    return token


def _resource_hint(segments: list[str], placeholder_segment_index: int) -> str:
    for index in range(placeholder_segment_index - 1, -1, -1):
        segment = segments[index]
        if "{" in segment or not segment:
            continue

        base = segment.split(".", 1)[0]
        if base:
            return _singularize(base)

    return "item"


def _path_parameter_plan(path: str) -> tuple[PathParameter, ...]:
    """Create a stable list of path parameter names for a path template."""
    segments = path.strip("/").split("/") if path.strip("/") else []
    placeholders = [match.group(1) for match in _PLACEHOLDER_RE.finditer(path)]

    names_in_use: set[str] = set()
    result: list[PathParameter] = []

    for placeholder_index, token in enumerate(placeholders):
        alias = _normalize_identifier(token)

        if alias == "id":
            segment_index = 0
            count = 0
            for idx, segment in enumerate(segments):
                if "{" in segment:
                    if count == placeholder_index:
                        segment_index = idx
                        break
                    count += 1

            alias = f"{_resource_hint(segments, segment_index)}_id"

        if alias in names_in_use:
            suffix = 2
            while f"{alias}_{suffix}" in names_in_use:
                suffix += 1
            alias = f"{alias}_{suffix}"

        names_in_use.add(alias)
        result.append(PathParameter(token=token, name=alias, index=placeholder_index))

    return tuple(result)


def _endpoint_key(method: str, path: str, used: set[str]) -> str:
    normalized_path = path.strip("/")
    normalized_path = normalized_path.replace("{", "").replace("}", "")
    normalized_path = normalized_path.replace(".", "-")
    normalized_path = _SANITIZE_RE.sub("-", normalized_path).strip("-").lower()
    if not normalized_path:
        normalized_path = "root"

    base_key = f"{method.lower()}-{normalized_path}"
    key = base_key

    if key not in used:
        used.add(key)
        return key

    suffix = 2
    while f"{base_key}-{suffix}" in used:
        suffix += 1

    key = f"{base_key}-{suffix}"
    used.add(key)
    return key


def build_endpoint_catalog(
    raw_endpoints: Sequence[tuple[str, str, str, str, int]],
) -> tuple[Endpoint, ...]:
    """Build normalized endpoint metadata from static raw endpoint tuples."""
    used_keys: set[str] = set()
    endpoints: list[Endpoint] = []

    for method, path, section, source_file, source_line in raw_endpoints:
        endpoints.append(
            Endpoint(
                key=_endpoint_key(method, path, used_keys),
                method=method,
                path=path,
                section=section,
                source_file=source_file,
                source_line=source_line,
                path_parameters=_path_parameter_plan(path),
            )
        )

    return tuple(endpoints)


def endpoint_to_payload(endpoint: Endpoint) -> dict[str, Any]:
    """Convert endpoint metadata into JSON-serializable payload shape."""
    return {
        "key": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "section": endpoint.section,
        "source_file": endpoint.source_file,
        "source_line": endpoint.source_line,
        "path_parameters": [
            {
                "token": param.token,
                "name": param.name,
                "index": param.index,
            }
            for param in endpoint.path_parameters
        ],
    }


def parse_key_value_pairs(
    raw_items: Sequence[str], *, allow_duplicate_keys: bool
) -> list[tuple[str, str]]:
    """Parse repeated CLI key=value arguments preserving input order."""
    pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for item in raw_items:
        if "=" not in item:
            raise ValueError(f"Invalid key/value item '{item}'. Expected KEY=VALUE.")

        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            raise ValueError(f"Invalid key/value item '{item}'. Empty key is not allowed.")

        if not allow_duplicate_keys and key in seen_keys:
            raise ValueError(
                f"Duplicate key '{key}' is not allowed in this argument set."
            )

        seen_keys.add(key)
        pairs.append((key, value))

    return pairs


def parse_json_body(
    body: str | None,
    body_file_content: str | None,
) -> Any:
    """Parse request body input as JSON object/array/scalar."""
    if body is None and body_file_content is None:
        return None

    raw = body if body is not None else body_file_content
    assert raw is not None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON body: {error.msg}") from error


def apply_path_parameters(
    path_template: str,
    path_parameters: Sequence[PathParameter],
    values: Mapping[str, str],
) -> str:
    """Replace placeholders in path templates with supplied values."""
    if not path_parameters:
        return path_template

    missing: list[str] = []

    def replacement(match: re.Match[str]) -> str:
        index = replacement.index
        replacement.index += 1

        if index >= len(path_parameters):
            return match.group(0)

        plan = path_parameters[index]

        if plan.name in values:
            return str(values[plan.name])

        if plan.token in values:
            return str(values[plan.token])

        missing.append(plan.name)
        return match.group(0)

    replacement.index = 0  # type: ignore[attr-defined]

    resolved = _PLACEHOLDER_RE.sub(replacement, path_template)

    if missing:
        raise ValueError(
            "Missing path parameters: "
            + ", ".join(sorted(dict.fromkeys(missing)))
            + "."
        )

    return resolved


def normalize_error_message(raw: Any) -> str | None:
    """Extract a human-meaningful message from arbitrary error payloads."""
    if isinstance(raw, Mapping):
        for key in ("message", "error", "detail", "title"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        errors = raw.get("errors")
        if isinstance(errors, Mapping):
            parts: list[str] = []
            for key, value in errors.items():
                if isinstance(value, str) and value.strip():
                    parts.append(f"{key}: {value.strip()}")
                elif isinstance(value, list):
                    joined = ", ".join(str(item) for item in value if str(item).strip())
                    if joined:
                        parts.append(f"{key}: {joined}")
            if parts:
                return "; ".join(parts)

    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    return None
