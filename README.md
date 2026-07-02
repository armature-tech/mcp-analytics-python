# armature-mcp-analytics

Armature analytics for Python MCP servers. The v1 integration is FastMCP-first:
instrument a `FastMCP` instance, keep writing normal tools, and Armature records
tool calls with MCP-specific telemetry.

Install:

```bash
pip install "armature-mcp-analytics[fastmcp]"
```

```python
import os
from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")

instrument_fastmcp(
    mcp,
    {
        "armature": {
            "api_key": os.getenv("ANALYTICS_INGEST_API_KEY"),
            "delivery": "await",
        }
    },
)

@mcp.tool
def lookup_customer(customer_id: str) -> dict:
    """Look up a customer by id."""
    return {"customer_id": customer_id, "status": "active"}

if __name__ == "__main__":
    mcp.run()
```

Agents see an optional `telemetry` object on each tool input schema. The SDK
strips that object before your handler runs, then emits an authenticated batch
to Armature.

Environment variables:

| Variable | Purpose |
| --- | --- |
| `ANALYTICS_INGEST_API_KEY` | Armature ingest API key. Missing keys no-op for local development. |
| `ANALYTICS_INGEST_URL` | Optional ingest endpoint override. |
