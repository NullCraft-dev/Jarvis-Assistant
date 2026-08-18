"""只供 MCP stdio 集成验收使用的本地 echo server。"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jarvis-mcp-test")


@mcp.tool()
def echo(text: str) -> dict[str, str]:
    """Return the supplied text without side effects."""
    return {"echo": text}


if __name__ == "__main__":
    mcp.run(transport="stdio")
