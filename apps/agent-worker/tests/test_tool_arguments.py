from jarvis_worker.agent.core.tool_arguments import normalize_tool_arguments


def test_rag_document_ids_null_uses_schema_default() -> None:
    assert normalize_tool_arguments("rag.search", {"query": "question", "document_ids": None}) == {
        "query": "question"
    }


def test_single_rag_document_id_string_becomes_array() -> None:
    assert normalize_tool_arguments(
        "rag.search", {"query": "question", "document_ids": " doc-1 "}
    ) == {"query": "question", "document_ids": ["doc-1"]}


def test_unrelated_arguments_are_not_changed() -> None:
    arguments = {"path": "README.md", "value": None}
    assert normalize_tool_arguments("workspace.read_file", arguments) == arguments
