"""权威文献元数据 MCP server（第一版：arXiv）。"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from jarvis_worker.agent.literature.arxiv import parse_feed, search_arxiv_metadata

_parse_feed = parse_feed

mcp = FastMCP("jarvis-literature")


@mcp.tool()
def search_arxiv(
    query: str,
    max_results: int = 5,
    sort_by: str = "submittedDate",
) -> dict[str, Any]:
    """Return bounded arXiv source records with abstract content; do not download files."""
    return search_arxiv_metadata(query, max_results, sort_by)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
