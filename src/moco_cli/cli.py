"""Command line interface for MOCO."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    API_BASE_URL_TEMPLATE,
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    DEFAULT_TIMEOUT_SECONDS,
    DOCS_REPO_URL,
    DOCS_SNAPSHOT_DATE,
    DOMAIN_ENV_VAR,
    ENDPOINT_COUNT,
    IMPERSONATE_USER_ID_ENV_VAR,
    RAW_ENDPOINTS,
    VALID_HTTP_METHODS,
)
from .transform import (
    Endpoint,
    apply_path_parameters,
    build_endpoint_catalog,
    endpoint_to_payload,
    normalize_error_message,
    parse_json_body,
    parse_key_value_pairs,
)


class CliInputError(ValueError):
    """Raised for invalid CLI argument combinations."""


class MocoApiError(RuntimeError):
    """Raised when MOCO API returns non-success status."""


class MocoAuthError(MocoApiError):
    """Raised when authentication/authorization fails."""


class MocoRateLimitError(MocoApiError):
    """Raised when MOCO API rate limit is exceeded."""


class MocoNotFoundError(MocoApiError):
    """Raised when a resource or endpoint is not found."""


class MocoValidationError(MocoApiError):
    """Raised for 422 validation errors from MOCO API."""


_ENDPOINTS: tuple[Endpoint, ...] = build_endpoint_catalog(RAW_ENDPOINTS)
_ENDPOINTS_BY_KEY: dict[str, Endpoint] = {endpoint.key: endpoint for endpoint in _ENDPOINTS}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="moco",
        description=(
            "Complete MOCO API CLI wrapper (all documented endpoints) with "
            "impersonation, JSON output, and generic endpoint calls"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv(API_KEY_ENV_VAR),
        help=f"MOCO API key (or env {API_KEY_ENV_VAR})",
    )
    parser.add_argument(
        "--domain",
        default=os.getenv(DOMAIN_ENV_VAR),
        help=(
            "MOCO account domain slug or host (or env MOCO_DOMAIN). "
            "Examples: mycompany or mycompany.mocoapp.com"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv(BASE_URL_ENV_VAR),
        help=(
            "Full API base URL override (or env MOCO_BASE_URL). "
            "Example: https://mycompany.mocoapp.com/api/v1"
        ),
    )
    parser.add_argument(
        "--impersonate-user-id",
        default=os.getenv(IMPERSONATE_USER_ID_ENV_VAR),
        help=(
            "Act on behalf of a user via X-IMPERSONATE-USER-ID header "
            f"(or env {IMPERSONATE_USER_ID_ENV_VAR})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Request timeout in seconds",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output as JSON",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    endpoints_parser = subparsers.add_parser(
        "endpoints",
        help="List all documented MOCO API endpoints",
    )
    endpoints_parser.add_argument(
        "--method",
        choices=VALID_HTTP_METHODS,
        help="Filter by HTTP method",
    )
    endpoints_parser.add_argument(
        "--section",
        help="Filter by section name (substring match, case-insensitive)",
    )
    endpoints_parser.add_argument(
        "--contains",
        help="Filter by path/key substring (case-insensitive)",
    )
    endpoints_parser.add_argument(
        "--limit",
        type=int,
        help="Optional result limit",
    )
    endpoints_parser.add_argument(
        "--show-source",
        action="store_true",
        help="Include source file and line from the docs repository",
    )

    call_parser = subparsers.add_parser(
        "call",
        help="Call any MOCO endpoint using endpoint key or method/path",
    )
    call_parser.add_argument(
        "--endpoint",
        help="Endpoint key from 'moco endpoints' (recommended)",
    )
    call_parser.add_argument(
        "--method",
        choices=VALID_HTTP_METHODS,
        help="HTTP method (required when --endpoint is not used)",
    )
    call_parser.add_argument(
        "--path",
        help="Request path (required when --endpoint is not used), e.g. /projects/{id}",
    )
    call_parser.add_argument(
        "--path-param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Path parameter assignment, repeatable",
    )
    call_parser.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Query parameter assignment, repeatable",
    )
    call_parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional request header, repeatable",
    )
    call_parser.add_argument(
        "--body",
        help="JSON request body as inline string",
    )
    call_parser.add_argument(
        "--body-file",
        help="Path to JSON file used as request body",
    )
    call_parser.add_argument(
        "--output",
        help="Optional output file for binary/text responses",
    )

    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Validate argument combinations."""
    if args.timeout <= 0:
        raise CliInputError("--timeout must be greater than 0.")

    if args.command == "endpoints" and args.limit is not None and args.limit <= 0:
        raise CliInputError("--limit must be greater than 0.")

    if args.command == "call":
        if not args.api_key:
            raise CliInputError(f"API key missing. Use --api-key or {API_KEY_ENV_VAR}.")

        if not args.base_url and not args.domain:
            raise CliInputError(
                f"Provide --domain (or env {DOMAIN_ENV_VAR}) or --base-url (or env {BASE_URL_ENV_VAR})."
            )

        if args.endpoint and (args.method or args.path):
            raise CliInputError(
                "Use either --endpoint or --method/--path, not both."
            )

        if not args.endpoint and (not args.method or not args.path):
            raise CliInputError(
                "Use --endpoint or provide both --method and --path."
            )

        if args.endpoint and args.endpoint not in _ENDPOINTS_BY_KEY:
            raise CliInputError(
                f"Unknown endpoint key '{args.endpoint}'. Use 'moco endpoints' to list available keys."
            )

        if args.body and args.body_file:
            raise CliInputError("Use --body or --body-file, not both.")

        if args.output:
            output_path = Path(args.output).expanduser()
            if output_path.exists() and output_path.is_dir():
                raise CliInputError("--output points to a directory; provide a file path.")


def _normalize_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        candidate = args.base_url
    else:
        assert args.domain
        domain = args.domain.strip()
        if domain.startswith(("http://", "https://")):
            candidate = domain
            if "/api/" not in candidate:
                candidate = candidate.rstrip("/") + "/api/v1"
        elif "." in domain:
            candidate = f"https://{domain}/api/v1"
        else:
            candidate = API_BASE_URL_TEMPLATE.format(domain=domain)

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliInputError("Resolved base URL is invalid. Check --domain/--base-url.")

    return candidate.rstrip("/")


def _render_table(headers: list[str], rows: list[list[Any]]) -> str:
    normalized_rows = [
        ["-" if value is None else str(value) for value in row] for row in rows
    ]
    widths = [len(h) for h in headers]

    for row in normalized_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    header_line = " | ".join(
        header.ljust(widths[i]) for i, header in enumerate(headers)
    )
    separator = "-+-".join("-" * width for width in widths)
    body = [
        " | ".join(value.ljust(widths[i]) for i, value in enumerate(row))
        for row in normalized_rows
    ]

    return "\n".join([header_line, separator, *body])


def _base_payload(command: str, base_url: str) -> dict[str, Any]:
    return {
        "command": command,
        "location": {"base_url": base_url},
    }


def _filtered_endpoints(args: argparse.Namespace) -> list[Endpoint]:
    endpoints = list(_ENDPOINTS)

    if args.method:
        endpoints = [endpoint for endpoint in endpoints if endpoint.method == args.method]

    if args.section:
        needle = args.section.lower()
        endpoints = [
            endpoint
            for endpoint in endpoints
            if needle in endpoint.section.lower()
        ]

    if args.contains:
        needle = args.contains.lower()
        endpoints = [
            endpoint
            for endpoint in endpoints
            if needle in endpoint.path.lower() or needle in endpoint.key.lower()
        ]

    if args.limit is not None:
        endpoints = endpoints[: args.limit]

    return endpoints


def _build_call_target(args: argparse.Namespace) -> tuple[str, str, Endpoint | None]:
    if args.endpoint:
        endpoint = _ENDPOINTS_BY_KEY[args.endpoint]
        return endpoint.method, endpoint.path, endpoint

    assert args.method and args.path
    return args.method, args.path, None


def _headers(api_key: str, impersonate_user_id: str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Token token={api_key}",
        "Accept": "*/*",
    }

    if impersonate_user_id:
        headers["X-IMPERSONATE-USER-ID"] = str(impersonate_user_id)

    return headers


def _parse_body(args: argparse.Namespace) -> Any:
    body_file_content: str | None = None

    if args.body_file:
        path = Path(args.body_file).expanduser()
        if not path.exists():
            raise CliInputError(f"Body file not found: {path}")
        if path.is_dir():
            raise CliInputError(f"Body file path points to a directory: {path}")
        body_file_content = path.read_text(encoding="utf-8")

    try:
        return parse_json_body(args.body, body_file_content)
    except ValueError as error:
        raise CliInputError(str(error)) from error


def _parse_response_body(body: bytes, content_type: str) -> tuple[str, Any]:
    content_type_lower = content_type.lower()

    if "json" in content_type_lower:
        text = body.decode("utf-8", errors="replace")
        try:
            return "json", json.loads(text)
        except json.JSONDecodeError:
            return "text", text

    if content_type_lower.startswith("text/"):
        return "text", body.decode("utf-8", errors="replace")

    stripped = body.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        text = body.decode("utf-8", errors="replace")
        try:
            return "json", json.loads(text)
        except json.JSONDecodeError:
            return "text", text

    return "binary", body


def _resolve_output_path(output: str) -> Path:
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def _request(
    session: ClientSession,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Sequence[tuple[str, str]],
    json_body: Any,
) -> tuple[int, dict[str, str], bytes]:
    async with session.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
    ) as response:
        body = await response.read()
        headers_out = dict(response.headers)
        status = response.status

        content_type = headers_out.get("Content-Type", "")
        kind, parsed = _parse_response_body(body, content_type)

        if status in {401, 403}:
            raise MocoAuthError("Invalid API key or insufficient permissions.")

        if status == 404:
            raise MocoNotFoundError("Resource or endpoint not found.")

        if status == 422:
            message = normalize_error_message(parsed) or "Validation failed (HTTP 422)."
            raise MocoValidationError(message)

        if status == 429:
            raise MocoRateLimitError("Request limit exceeded.")

        if status >= 400:
            message = normalize_error_message(parsed) or f"HTTP {status}"
            raise MocoApiError(message)

        return status, headers_out, body


async def run_command(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the selected command."""
    if args.command == "endpoints":
        base_url = (
            _normalize_base_url(args)
            if args.base_url or args.domain
            else "-"
        )
        endpoints = _filtered_endpoints(args)
        return {
            **_base_payload(args.command, base_url),
            "documentation": {
                "repo": DOCS_REPO_URL,
                "snapshot_date": DOCS_SNAPSHOT_DATE,
                "total_documented_endpoints": ENDPOINT_COUNT,
            },
            "count": len(endpoints),
            "endpoints": [endpoint_to_payload(endpoint) for endpoint in endpoints],
        }

    base_url = _normalize_base_url(args)
    method, path_template, endpoint = _build_call_target(args)

    try:
        path_param_pairs = parse_key_value_pairs(
            args.path_param,
            allow_duplicate_keys=False,
        )
        query_pairs = parse_key_value_pairs(
            args.query,
            allow_duplicate_keys=True,
        )
        header_pairs = parse_key_value_pairs(
            args.header,
            allow_duplicate_keys=False,
        )
    except ValueError as error:
        raise CliInputError(str(error)) from error

    path_param_values = dict(path_param_pairs)

    path_parameters = endpoint.path_parameters if endpoint else build_endpoint_catalog(
        ((method, path_template, "Ad-hoc", "runtime", 0),)
    )[0].path_parameters

    try:
        resolved_path = apply_path_parameters(
            path_template,
            path_parameters,
            path_param_values,
        )
    except ValueError as error:
        raise CliInputError(str(error)) from error

    if not resolved_path.startswith("/"):
        raise CliInputError("Resolved --path must start with '/'.")

    url = f"{base_url}{resolved_path}"
    json_body = _parse_body(args)

    request_headers = _headers(args.api_key, args.impersonate_user_id)
    request_headers.update(dict(header_pairs))

    timeout = ClientTimeout(total=args.timeout)

    async with ClientSession(timeout=timeout) as session:
        status_code, response_headers, response_bytes = await _request(
            session,
            method,
            url,
            headers=request_headers,
            params=query_pairs,
            json_body=json_body,
        )

    response_content_type = response_headers.get("Content-Type", "")
    response_kind, parsed_body = _parse_response_body(response_bytes, response_content_type)

    response_payload: dict[str, Any] = {
        "status_code": status_code,
        "content_type": response_content_type,
        "kind": response_kind,
        "headers": response_headers,
    }

    if response_kind == "json":
        response_payload["body"] = parsed_body
    elif response_kind == "text":
        text_payload = str(parsed_body)
        if args.output:
            output_path = _resolve_output_path(args.output)
            output_path.write_text(text_payload, encoding="utf-8")
            response_payload["output_file"] = str(output_path)
            response_payload["bytes_written"] = len(text_payload.encode("utf-8"))
        else:
            response_payload["body"] = text_payload
    else:
        assert isinstance(parsed_body, bytes)
        if args.output:
            output_path = _resolve_output_path(args.output)
            output_path.write_bytes(parsed_body)
            response_payload["output_file"] = str(output_path)
            response_payload["bytes_written"] = len(parsed_body)
        else:
            response_payload["bytes"] = len(parsed_body)
            response_payload["preview_hex"] = parsed_body[:64].hex()

    return {
        **_base_payload(args.command, base_url),
        "request": {
            "endpoint": endpoint_to_payload(endpoint) if endpoint else None,
            "method": method,
            "path_template": path_template,
            "path": resolved_path,
            "query": query_pairs,
            "headers": {
                key: value
                for key, value in request_headers.items()
                if key.lower() != "authorization"
            },
            "has_body": json_body is not None,
        },
        "response": response_payload,
    }


def print_human(command: str, payload: dict[str, Any], args: argparse.Namespace) -> None:
    """Print result in human-readable format."""
    print(f"API: {payload['location']['base_url']}")

    if command == "endpoints":
        documentation = payload["documentation"]
        print(
            "Docs snapshot: "
            f"{documentation['snapshot_date']} "
            f"({documentation['total_documented_endpoints']} endpoints)"
        )
        print(f"Matched: {payload['count']}")

        headers = ["key", "method", "path", "params", "section"]
        rows: list[list[Any]] = []
        for endpoint in payload["endpoints"]:
            params = ",".join(
                param["name"] for param in endpoint["path_parameters"]
            )
            rows.append(
                [
                    endpoint["key"],
                    endpoint["method"],
                    endpoint["path"],
                    params,
                    endpoint["section"],
                ]
            )

        if rows:
            print(_render_table(headers, rows))
        else:
            print("No endpoints matched the given filter.")

        if args.show_source and rows:
            print("Sources:")
            for endpoint in payload["endpoints"]:
                print(
                    f"- {endpoint['key']}: "
                    f"{endpoint['source_file']}:{endpoint['source_line']}"
                )
        return

    request = payload["request"]
    response = payload["response"]

    print(f"Method: {request['method']}")
    print(f"Path: {request['path']}")
    if request["endpoint"]:
        print(f"Endpoint key: {request['endpoint']['key']}")
    print(f"Status: {response['status_code']}")
    print(f"Content-Type: {response.get('content_type') or '-'}")

    if response["kind"] == "json":
        print(json.dumps(response["body"], ensure_ascii=False, indent=2, sort_keys=True))
        return

    if response["kind"] == "text":
        if "output_file" in response:
            print(f"Output file: {response['output_file']}")
            print(f"Bytes written: {response['bytes_written']}")
        else:
            print(response.get("body") or "")
        return

    if "output_file" in response:
        print(f"Output file: {response['output_file']}")
        print(f"Bytes written: {response['bytes_written']}")
    else:
        print(f"Bytes: {response['bytes']}")
        print(f"Preview (hex): {response['preview_hex']}")
        print("Tip: use --output <file> to save binary responses.")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_args(args)
        payload = asyncio.run(run_command(args))
    except CliInputError as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2
    except MocoAuthError:
        print("Error: Invalid API key or missing permission.", file=sys.stderr)
        return 2
    except MocoRateLimitError:
        print("Error: Request limit exceeded.", file=sys.stderr)
        return 2
    except MocoValidationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except MocoNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (MocoApiError, ClientError, TimeoutError) as error:
        print(f"Error while calling MOCO API: {error}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human(args.command, payload, args)

    return 0
