"""Validate captured runtime OAS and JSON samples, NOT private frozen contracts."""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate


def verify(junit: Path) -> dict:
    properties = ET.parse(junit).findall(".//property")
    documents = [
        json.loads(p.attrib["value"]) for p in properties if p.get("name") == "runtime_openapi"
    ]
    samples = [
        sample
        for p in properties
        if p.get("name") == "contract_samples"
        for sample in json.loads(p.attrib["value"])
    ]
    if len(documents) != 1 or not samples:
        raise ValueError(
            "Require exactly one runtime OpenAPI document and nonempty captured samples"
        )
    document = documents[0]

    def local_refs(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if not ref.startswith("#/"):
                    raise ValueError("Remote references are not allowed in offline validation")
                target = document
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for value in node.values():
                local_refs(value)
        elif isinstance(node, list):
            for value in node:
                local_refs(value)

    # Reject external references before a validator could try to retrieve them.
    local_refs(document)
    validate(document)
    schemas = document.get("components", {}).get("schemas", {})
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    def check(schema, instance):
        rooted = {"components": document.get("components", {}), **schema}
        Draft202012Validator(rooted, format_checker=FormatChecker()).validate(instance)

    for sample in samples:
        operation = document["paths"][sample["path"]][sample["method"]]
        response = operation["responses"][str(sample["status"])]
        check(response["content"]["application/json"]["schema"], sample["response"])
        if sample["request"] is not None:
            check(
                operation["requestBody"]["content"]["application/json"]["schema"], sample["request"]
            )
    return {
        "status": "PASS",
        "scope": "runtime OAS and captured JSON request/response validation only",
        "schemas": len(schemas),
        "samples": len(samples),
        "frozen_contract_comparison": "NOT RUN: private contract intentionally excluded",
        "phase4_acceptance": "NOT ACCEPTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.junit)
    except Exception as exc:
        # Raw validation messages can include captured data or host paths.
        result = {"status": "FAIL", "error_type": type(exc).__name__}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
