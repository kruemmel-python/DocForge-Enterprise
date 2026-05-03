from docforge_enterprise.lmstudio import extract_json


def test_extract_json_from_markdown_fence() -> None:
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_repairs_trailing_comma_and_text() -> None:
    assert extract_json("Here is the result:\n{\"a\": 1, \"b\": [2,],}\nthanks") == {"a": 1, "b": [2]}


def test_extract_json_accepts_pythonish_dict() -> None:
    assert extract_json("{'ok': True, 'value': None}") == {"ok": True, "value": None}
