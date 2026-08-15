#!/usr/bin/env python3
# pyright: basic
# ruff: noqa: T201, PERF401, E501
"""Compare two OpenAPI documents and fail on breaking public-surface changes.

This is the enforcement script behind the CI *OpenAPI diff* gate.  It
compares the OpenAPI document generated from the pull-request branch (head)
against the one generated from the merge base (base) and exits non-zero when
the PR changes the public API surface in a prohibited way.

Policy (documented in README, section "API contract (OpenAPI) diff gate")
-----------------------------------------------------------------------
*Breaking / non-additive changes FAIL the check:*

- removed path or removed HTTP method on an existing path
- changed or removed ``operationId``
- removed parameter, parameter becoming required, changed parameter
  ``in``/type/``$ref``, removed enum value
- removed request body, request body becoming required, removed media type
- removed response status code, removed response media type, changed
  response schema
- removed schema, removed property, property becoming required, added
  required property, changed property type, removed enum value
- removed component (security scheme, parameter, request body, response,
  header) and added security requirement on an operation

*Additive-only changes are ALLOWED (reported, never fail):*

- new path, new HTTP method, new operation, new (optional) parameter
- new optional property, new schema, new response status code / media type
- new enum value, added security schemes, relaxed ``required`` flags

Anything else (summaries, descriptions, ``format``/``default``/``example``
changes, ``info.version`` bumps, tag renames) is purely informational.

Allowlist
---------
Exceptions are recorded in ``scripts/openapi_diff_allowlist.json`` (or a
file passed via ``--allowlist``).  The file maps a finding signature to a
free-text reason:

    {
      "removed_path:DELETE /api/v1/legacy-endpoint":
          "removed in v0.5.0, see CHANGELOG"
    }

A signature has the shape ``<kind>:<detail>`` and is printed by this script
when a breaking finding is detected.  Every allowlist entry must be used by
at least one finding, otherwise the script fails (stale-allowlist guard).

Usage
-----
    uv run python scripts/check_openapi_diff.py \\
        --base openapi-base.json --head openapi-head.json [--allowlist FILE]

Exit codes: 0 = no prohibited changes, 1 = breaking changes found (or the
allowlist is stale), 2 = usage/input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# OpenAPI HTTP methods, in canonical order.
HTTP_METHODS = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)

# Component sections whose removal is treated as breaking.  "schemas" is
# handled separately because it is compared property-by-property.
COMPONENT_SECTIONS = (
    "securitySchemes",
    "parameters",
    "requestBodies",
    "responses",
    "headers",
)

_SEVERITY_LABEL = {
    "breaking": "BREAKING",
    "additive": "ADDITIVE",
    "info": "INFO",
}


class Finding:
    """A single classified difference between the two documents."""

    __slots__ = ("detail", "kind", "severity")

    def __init__(self, kind: str, detail: str, severity: str) -> None:
        self.kind = kind
        self.detail = detail
        self.severity = severity  # "breaking" | "additive" | "info"

    @property
    def signature(self) -> str:
        return f"{self.kind}:{self.detail}"

    def __str__(self) -> str:
        label = _SEVERITY_LABEL[self.severity]
        return f"[{label}] {self.kind}: {self.detail}"


def load_spec(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"error: cannot read OpenAPI document {path}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if not isinstance(spec, dict) or "paths" not in spec:
        print(
            f"error: {path} does not look like an OpenAPI document",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return spec


def parameter_key(parameter: dict) -> str:
    return f"{parameter.get('in', '?')}:{parameter.get('name', '?')}"


def schema_type(schema: dict) -> tuple:
    """A comparable fingerprint for a schema: type or $ref target."""
    if not isinstance(schema, dict):
        return ("<missing>",)
    if "$ref" in schema:
        return ("$ref", schema["$ref"])
    if "type" in schema:
        return ("type", schema["type"])
    return ("<untyped>",)


def schema_items(schema: dict) -> dict:
    """Return the items schema for arrays, or an empty dict."""
    if isinstance(schema, dict) and schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return items
    return {}


def compare_parameter(
    path: str,
    method: str,
    base_param: dict,
    head_param: dict,
    findings: list[Finding],
) -> None:
    key = parameter_key(base_param)
    detail = f"{path} {method.upper()} {key}"

    if base_param.get("required") and not head_param.get("required"):
        findings.append(Finding("relaxed_parameter", detail, "additive"))
    elif not base_param.get("required") and head_param.get("required"):
        findings.append(
            Finding("parameter_became_required", detail, "breaking")
        )

    if base_param.get("in") != head_param.get("in"):
        findings.append(
            Finding(
                "changed_parameter_in",
                f"{detail} ({base_param.get('in')} -> {head_param.get('in')})",
                "breaking",
            )
        )

    base_schema = base_param.get("schema") or {}
    head_schema = head_param.get("schema") or {}
    if schema_type(base_schema) != schema_type(head_schema):
        findings.append(
            Finding(
                "changed_parameter_type",
                f"{detail} ({schema_type(base_schema)} -> "
                f"{schema_type(head_schema)})",
                "breaking",
            )
        )
    base_items = schema_items(base_schema)
    head_items = schema_items(head_schema)
    if (
        base_items
        and head_items
        and schema_type(base_items) != schema_type(head_items)
    ):
        findings.append(
            Finding(
                "changed_parameter_item_type",
                f"{detail} (items {schema_type(base_items)} -> "
                f"{schema_type(head_items)})",
                "breaking",
            )
        )

    base_enums = base_schema.get("enum")
    head_enums = head_schema.get("enum")
    if isinstance(base_enums, list) and isinstance(head_enums, list):
        removed = [v for v in base_enums if v not in head_enums]
        if removed:
            findings.append(
                Finding(
                    "removed_enum_value", f"{detail} ({removed})", "breaking"
                )
            )


def compare_request_body(
    path: str,
    method: str,
    base_body: dict,
    head_body: dict,
    findings: list[Finding],
) -> None:
    detail = f"{path} {method.upper()}"

    if not base_body.get("required") and head_body.get("required"):
        findings.append(
            Finding("request_body_became_required", detail, "breaking")
        )
    elif base_body.get("required") and not head_body.get("required"):
        findings.append(
            Finding("request_body_became_optional", detail, "additive")
        )

    base_media = set((base_body.get("content") or {}).keys())
    head_media = set((head_body.get("content") or {}).keys())
    for media in sorted(base_media - head_media):
        findings.append(
            Finding(
                "removed_media_type", f"{detail} request {media}", "breaking"
            )
        )
    for media in sorted(head_media - base_media):
        findings.append(
            Finding("added_media_type", f"{detail} request {media}", "additive")
        )


def compare_responses(
    path: str,
    method: str,
    base_responses: dict,
    head_responses: dict,
    findings: list[Finding],
) -> None:
    detail = f"{path} {method.upper()}"
    base_codes = set(base_responses.keys())
    head_codes = set(head_responses.keys())

    for code in sorted(base_codes - head_codes):
        findings.append(
            Finding("removed_response", f"{detail} {code}", "breaking")
        )
    for code in sorted(head_codes - base_codes):
        findings.append(
            Finding("added_response", f"{detail} {code}", "additive")
        )

    for code in sorted(base_codes & head_codes):
        base_content = (base_responses[code] or {}).get("content") or {}
        head_content = (head_responses[code] or {}).get("content") or {}
        base_media = set(base_content.keys())
        head_media = set(head_content.keys())
        for media in sorted(base_media - head_media):
            findings.append(
                Finding(
                    "removed_response_media_type",
                    f"{detail} {code} {media}",
                    "breaking",
                )
            )
        for media in sorted(head_media - base_media):
            findings.append(
                Finding(
                    "added_response_media_type",
                    f"{detail} {code} {media}",
                    "additive",
                )
            )
        for media in sorted(base_media & head_media):
            base_schema = (base_content[media] or {}).get("schema") or {}
            head_schema = (head_content[media] or {}).get("schema") or {}
            if schema_type(base_schema) != schema_type(head_schema):
                findings.append(
                    Finding(
                        "changed_response_schema",
                        f"{detail} {code} {media} "
                        f"({schema_type(base_schema)} -> "
                        f"{schema_type(head_schema)})",
                        "breaking",
                    )
                )


def compare_operation(
    path: str,
    method: str,
    base_op: dict,
    head_op: dict,
    findings: list[Finding],
) -> None:
    detail = f"{path} {method.upper()}"

    base_opid = base_op.get("operationId")
    head_opid = head_op.get("operationId")
    if base_opid and head_opid and base_opid != head_opid:
        findings.append(
            Finding(
                "changed_operation_id",
                f"{detail} {base_opid} -> {head_opid}",
                "breaking",
            )
        )
    elif base_opid and not head_opid:
        findings.append(
            Finding("removed_operation_id", f"{detail} {base_opid}", "breaking")
        )

    # ── parameters ────────────────────────────────────────────────────
    base_params = {parameter_key(p): p for p in base_op.get("parameters") or []}
    head_params = {parameter_key(p): p for p in head_op.get("parameters") or []}
    for key in sorted(base_params.keys() - head_params.keys()):
        findings.append(
            Finding("removed_parameter", f"{detail} {key}", "breaking")
        )
    for key in sorted(head_params.keys() - base_params.keys()):
        new_param = head_params[key]
        severity = "breaking" if new_param.get("required") else "additive"
        findings.append(Finding("added_parameter", f"{detail} {key}", severity))
    for key in sorted(base_params.keys() & head_params.keys()):
        compare_parameter(
            path, method, base_params[key], head_params[key], findings
        )

    # ── requestBody ───────────────────────────────────────────────────
    base_body = base_op.get("requestBody")
    head_body = head_op.get("requestBody")
    if base_body is not None and head_body is None:
        findings.append(Finding("removed_request_body", detail, "breaking"))
    elif base_body is None and head_body is not None:
        severity = "breaking" if head_body.get("required") else "additive"
        findings.append(Finding("added_request_body", detail, severity))
    elif base_body is not None and head_body is not None:
        compare_request_body(path, method, base_body, head_body, findings)

    # ── responses ─────────────────────────────────────────────────────
    base_responses = base_op.get("responses") or {}
    head_responses = head_op.get("responses") or {}
    compare_responses(path, method, base_responses, head_responses, findings)

    # ── security ──────────────────────────────────────────────────────
    base_security = {
        tuple(sorted(r.keys())) for r in base_op.get("security") or []
    }
    head_security = {
        tuple(sorted(r.keys())) for r in head_op.get("security") or []
    }
    if not base_security and head_security:
        findings.append(
            Finding("added_security_requirement", detail, "breaking")
        )
    elif base_security and not head_security:
        findings.append(
            Finding("removed_security_requirement", detail, "additive")
        )
    elif base_security != head_security:
        findings.append(
            Finding("changed_security_requirement", detail, "breaking")
        )


def compare_schema(
    name: str,
    base_schema: dict,
    head_schema: dict,
    findings: list[Finding],
    _seen: set | None = None,
) -> None:
    """Compare one named component schema (property-level)."""
    seen = _seen if _seen is not None else set()
    if name in seen:
        return
    seen.add(name)

    if schema_type(base_schema) != schema_type(head_schema):
        findings.append(
            Finding(
                "changed_schema_type",
                f"{name} ({schema_type(base_schema)} -> "
                f"{schema_type(head_schema)})",
                "breaking",
            )
        )

    base_required = set(base_schema.get("required") or [])
    head_required = set(head_schema.get("required") or [])
    base_props = base_schema.get("properties") or {}
    head_props = head_schema.get("properties") or {}

    for prop in sorted(base_props.keys() - head_props.keys()):
        findings.append(
            Finding("removed_property", f"{name}.{prop}", "breaking")
        )
    for prop in sorted(head_props.keys() - base_props.keys()):
        severity = "breaking" if prop in head_required else "additive"
        findings.append(Finding("added_property", f"{name}.{prop}", severity))
    for prop in sorted(base_props.keys() & head_props.keys()):
        base_prop = base_props[prop]
        head_prop = head_props[prop]
        was_required = prop in base_required
        is_required = prop in head_required
        if not was_required and is_required:
            findings.append(
                Finding(
                    "property_became_required", f"{name}.{prop}", "breaking"
                )
            )
        elif was_required and not is_required:
            findings.append(
                Finding(
                    "property_became_optional", f"{name}.{prop}", "additive"
                )
            )
        if schema_type(base_prop) != schema_type(head_prop):
            findings.append(
                Finding(
                    "changed_property_type",
                    f"{name}.{prop} ({schema_type(base_prop)} -> "
                    f"{schema_type(head_prop)})",
                    "breaking",
                )
            )
        base_items = schema_items(base_prop)
        head_items = schema_items(head_prop)
        if (
            base_items
            and head_items
            and schema_type(base_items) != schema_type(head_items)
        ):
            findings.append(
                Finding(
                    "changed_property_item_type",
                    f"{name}.{prop} (items {schema_type(base_items)} -> "
                    f"{schema_type(head_items)})",
                    "breaking",
                )
            )
        base_enums = base_prop.get("enum")
        head_enums = head_prop.get("enum")
        if isinstance(base_enums, list) and isinstance(head_enums, list):
            removed = [v for v in base_enums if v not in head_enums]
            if removed:
                findings.append(
                    Finding(
                        "removed_enum_value",
                        f"{name}.{prop} ({removed})",
                        "breaking",
                    )
                )


def diff_specs(base: dict, head: dict) -> list[Finding]:
    findings: list[Finding] = []

    # ── info / servers (informational only) ───────────────────────────
    if base.get("info") != head.get("info"):
        findings.append(
            Finding(
                "info_changed",
                f"base={base.get('info')} head={head.get('info')}",
                "info",
            )
        )
    base_servers = {s.get("url") for s in base.get("servers") or []}
    head_servers = {s.get("url") for s in head.get("servers") or []}
    for url in sorted(base_servers - head_servers):
        findings.append(Finding("removed_server", url, "breaking"))
    for url in sorted(head_servers - base_servers):
        findings.append(Finding("added_server", url, "additive"))

    # ── paths ─────────────────────────────────────────────────────────
    base_paths = base.get("paths") or {}
    head_paths = head.get("paths") or {}
    base_path_keys = set(base_paths.keys())
    head_path_keys = set(head_paths.keys())

    for path in sorted(base_path_keys - head_path_keys):
        findings.append(Finding("removed_path", path, "breaking"))
    for path in sorted(head_path_keys - base_path_keys):
        findings.append(Finding("added_path", path, "additive"))

    for path in sorted(base_path_keys & head_path_keys):
        base_item = base_paths[path] or {}
        head_item = head_paths[path] or {}
        base_methods = {m for m in HTTP_METHODS if m in base_item}
        head_methods = {m for m in HTTP_METHODS if m in head_item}
        for method in sorted(
            base_methods - head_methods, key=HTTP_METHODS.index
        ):
            findings.append(
                Finding(
                    "removed_method", f"{path} {method.upper()}", "breaking"
                )
            )
        for method in sorted(
            head_methods - base_methods, key=HTTP_METHODS.index
        ):
            findings.append(
                Finding("added_method", f"{path} {method.upper()}", "additive")
            )
        for method in HTTP_METHODS:
            if method in base_item and method in head_item:
                compare_operation(
                    path, method, base_item[method], head_item[method], findings
                )

    # ── components.schemas ────────────────────────────────────────────
    base_schemas = (base.get("components") or {}).get("schemas") or {}
    head_schemas = (head.get("components") or {}).get("schemas") or {}
    base_schema_keys = set(base_schemas.keys())
    head_schema_keys = set(head_schemas.keys())

    for name in sorted(base_schema_keys - head_schema_keys):
        findings.append(Finding("removed_schema", name, "breaking"))
    for name in sorted(head_schema_keys - base_schema_keys):
        findings.append(Finding("added_schema", name, "additive"))
    for name in sorted(base_schema_keys & head_schema_keys):
        compare_schema(name, base_schemas[name], head_schemas[name], findings)

    # ── other components ──────────────────────────────────────────────
    base_components = base.get("components") or {}
    head_components = head.get("components") or {}
    for section in COMPONENT_SECTIONS:
        base_items = base_components.get(section) or {}
        head_items = head_components.get(section) or {}
        for name in sorted(set(base_items.keys()) - set(head_items.keys())):
            findings.append(
                Finding("removed_component", f"{section}.{name}", "breaking")
            )
        for name in sorted(set(head_items.keys()) - set(base_items.keys())):
            findings.append(
                Finding("added_component", f"{section}.{name}", "additive")
            )

    return findings


def load_allowlist(path: Path) -> dict[str, str]:
    try:
        with path.open(encoding="utf-8") as fh:
            allowlist = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read allowlist {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(allowlist, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in allowlist.items()
    ):
        print(
            "error: allowlist must be a JSON object mapping "
            "signature -> reason string",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return allowlist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        required=True,
        type=Path,
        help="Base (merge-base) OpenAPI document.",
    )
    parser.add_argument(
        "--head",
        required=True,
        type=Path,
        help="Head (PR branch) OpenAPI document.",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="JSON file mapping breaking-finding signatures to reasons.",
    )
    args = parser.parse_args(argv)

    base = load_spec(args.base)
    head = load_spec(args.head)

    findings = diff_specs(base, head)

    breaking = [f for f in findings if f.severity == "breaking"]
    additive = [f for f in findings if f.severity == "additive"]
    info = [f for f in findings if f.severity == "info"]

    allowlist: dict[str, str] = {}
    if args.allowlist is not None:
        allowlist = load_allowlist(args.allowlist)

    remaining: list[Finding] = []
    for finding in breaking:
        if finding.signature in allowlist:
            print(
                f"[ALLOWLISTED] {finding} "
                f"— reason: {allowlist[finding.signature]}"
            )
        else:
            remaining.append(finding)

    stale = sorted(set(allowlist.keys()) - {f.signature for f in breaking})
    if stale:
        print(
            "error: stale allowlist entries (no matching finding): "
            f"{', '.join(stale)}",
            file=sys.stderr,
        )
        return 1

    for finding in sorted(additive, key=str):
        print(f"  {finding}")
    for finding in sorted(info, key=str):
        print(f"  {finding}")

    print("-" * 72)
    print(
        f"OpenAPI diff summary: {len(remaining)} breaking, "
        f"{len(additive)} additive, {len(info)} info"
    )
    if remaining:
        print(
            "\nProhibited public-surface changes (see README "
            "'API contract (OpenAPI) diff gate'):"
        )
        for finding in remaining:
            print(f"  {finding}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
