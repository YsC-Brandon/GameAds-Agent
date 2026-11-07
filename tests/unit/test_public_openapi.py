"""Public-release validation never substitutes for the frozen acceptance checks."""

import json
import xml.etree.ElementTree as ET

import pytest
from jsonschema.exceptions import ValidationError

from scripts.verify_public_openapi import verify


@pytest.fixture
def captured(tmp_path):
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic validation fixture", "version": "1.0"},
        "paths": {
            "/demo": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "integer"}}}
                    },
                    "responses": {
                        "200": {
                            "description": "Synthetic result",
                            "content": {"application/json": {"schema": {"type": "integer"}}},
                        }
                    },
                }
            }
        },
    }
    sample = {"path": "/demo", "method": "post", "status": 200, "request": 1, "response": 2}

    def write(*, documents=None, samples=None):
        root = ET.Element("testsuites")
        props = ET.SubElement(root, "properties")
        for doc in documents if documents is not None else [document]:
            ET.SubElement(props, "property", name="runtime_openapi", value=json.dumps(doc))
        ET.SubElement(
            props,
            "property",
            name="contract_samples",
            value=json.dumps(samples if samples is not None else [sample]),
        )
        path = tmp_path / "synthetic.xml"
        ET.ElementTree(root).write(path, encoding="utf-8")
        return path

    return document, sample, write


def test_public_runtime_validation_reports_limited_scope(captured):
    _, _, write = captured
    result = verify(write())
    assert result["status"] == "PASS"
    assert result["samples"] == 1
    assert result["frozen_contract_comparison"].startswith("NOT RUN")
    assert result["phase4_acceptance"] == "NOT ACCEPTED"


@pytest.mark.parametrize("case", ["missing", "duplicate", "empty", "request", "response", "remote"])
def test_public_runtime_validation_fails_closed(captured, case):
    document, sample, write = captured
    options = {}
    if case == "missing":
        options["documents"] = []
    elif case == "duplicate":
        options["documents"] = [document, document]
    elif case == "empty":
        options["samples"] = []
    elif case in ("request", "response"):
        sample[case] = "not an integer"
    else:
        document["components"] = {
            "schemas": {"External": {"$ref": "https://example.invalid/schema"}}
        }
    with pytest.raises((ValueError, ValidationError)):
        verify(write(**options))
