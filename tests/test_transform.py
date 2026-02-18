"""Tests for transformation helpers."""

from __future__ import annotations

from moco_cli.const import RAW_ENDPOINTS
from moco_cli.transform import (
    apply_path_parameters,
    build_endpoint_catalog,
    normalize_error_message,
    parse_json_body,
    parse_key_value_pairs,
)


def test_build_endpoint_catalog_size_and_unique_keys() -> None:
    catalog = build_endpoint_catalog(RAW_ENDPOINTS)

    assert len(catalog) == len(RAW_ENDPOINTS)
    keys = [item.key for item in catalog]
    assert len(keys) == len(set(keys))


def test_path_parameter_names_for_repeated_id_path() -> None:
    catalog = build_endpoint_catalog(
        (
            (
                "GET",
                "/projects/{id}/tasks/{id}",
                "Project Tasks",
                "project_tasks.md",
                48,
            ),
        )
    )

    endpoint = catalog[0]

    assert [param.name for param in endpoint.path_parameters] == [
        "project_id",
        "task_id",
    ]

    resolved = apply_path_parameters(
        endpoint.path,
        endpoint.path_parameters,
        {
            "project_id": "101",
            "task_id": "202",
        },
    )

    assert resolved == "/projects/101/tasks/202"


def test_apply_path_parameters_accepts_original_token_name() -> None:
    catalog = build_endpoint_catalog(
        (("GET", "/projects/{project_id}/tasks/{id}", "X", "x.md", 1),)
    )
    endpoint = catalog[0]

    resolved = apply_path_parameters(
        endpoint.path,
        endpoint.path_parameters,
        {
            "project_id": "111",
            "id": "222",
        },
    )

    assert resolved == "/projects/111/tasks/222"


def test_parse_key_value_pairs_allows_or_rejects_duplicates() -> None:
    pairs = parse_key_value_pairs(["a=1", "a=2", "b=3"], allow_duplicate_keys=True)

    assert pairs == [("a", "1"), ("a", "2"), ("b", "3")]

    try:
        parse_key_value_pairs(["a=1", "a=2"], allow_duplicate_keys=False)
        assert False, "Expected ValueError for duplicate keys"
    except ValueError as error:
        assert "Duplicate key" in str(error)


def test_parse_json_body_variants() -> None:
    assert parse_json_body('{"hello": "world"}', None) == {"hello": "world"}
    assert parse_json_body(None, "[1,2,3]") == [1, 2, 3]
    assert parse_json_body(None, None) is None

    try:
        parse_json_body("{", None)
        assert False, "Expected ValueError"
    except ValueError as error:
        assert "Invalid JSON body" in str(error)


def test_normalize_error_message_handles_nested_error_map() -> None:
    payload = {
        "errors": {
            "start_date": ["can't be blank"],
            "currency": "is invalid",
        }
    }

    message = normalize_error_message(payload)

    assert message is not None
    assert "start_date" in message
    assert "currency" in message
