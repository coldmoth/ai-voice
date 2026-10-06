"""Catalog boundary checks; no microphone or remote requests."""
import importlib.util
from pathlib import Path

import pytest


def preview():
    path = Path(__file__).resolve().parents[1] / "scripts/design-preview.py"
    spec = importlib.util.spec_from_file_location("design_preview", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_only_accepts_trained_public_russian_models():
    module = preview()
    model = {"_id": "ea0d727025a045aa8a6a9f9c3985437e", "title": "Папич",
             "languages": ["ru"], "state": "trained", "visibility": "public"}
    assert module.catalog_item(model)["name"] == "Папич"
    for field, bad in [("languages", ["en"]), ("state", "training"),
                       ("visibility", "private"), ("_id", "../secret"),
                       ("title", None)]:
        assert module.catalog_item(dict(model, **{field: bad})) is None


def test_catalog_query_validation():
    module = preview()
    assert module.search_parameters("  Папич  ", "2") == {"title": "Папич", "language": "ru",
                                                           "page_size": 12, "page_number": 2}
    for query, page in [("", "1"), ("a" * 121, "1"), ("x", "-1"),
                        ("x", "11"), ("x", "bad")]:
        with pytest.raises(ValueError):
            module.search_parameters(query, page)
