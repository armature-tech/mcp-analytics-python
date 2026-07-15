# Minimal Python MCP server

This complete stdio server exposes one `echo` tool and records its tool calls with Armature.

## Run

~~~bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ANALYTICS_INGEST_API_KEY="..." python server.py
~~~

Launch the command from an MCP client, call `echo`, and open Armature to inspect the session.
