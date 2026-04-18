# src/agent/mcp — warstwa Model Context Protocol

Lokalny (in-process) serwer MCP wystawiajacy ``ToolRegistry`` agenta
przez transport streamable-http + klient wolany w petli tool-use.

## Dlaczego MCP

1. **Prompt caching.** ``list_tools`` jest cachowane po stronie klienta.
   Ta sama (immutable) lista ``ToolDefinition`` leci do kazdego
   ``ChatRequest.tools`` → Anthropic widzi stabilny prefix i cachuje.
2. **External observability.** Dowolny klient MCP (mcp-inspector, Claude
   Desktop, Cursor IDE) moze podpiac sie do dzialajacego agenta i
   zobaczyc te same narzedzia — przydatne do debugowania.
3. **Separacja definicji od dispatchu.** Agent nie buduje ``Tool``
   descriptors recznie; robi to ``FastMCP`` z ``input_schema()`` naszego
   ``Tool``.

## Komponenty

```
┌──────────────────────────────────────────────────────────────┐
│  Agent  (src/agent/agent.py)                                  │
│  ─────────────────────────────────────────────────────────── │
│  start_mcp  → McpRuntime.start() + McpAgentClient.connect()   │
│  _run_tool_loop → _mcp_client.list_tools / .call_tool         │
│  stop_mcp   → client.close() + runtime.stop()                 │
└───────┬──────────────────────────────────────┬───────────────┘
        │ build                                │ dispatch
        ▼                                      ▼
┌──────────────────────────┐     ┌──────────────────────────────┐
│  build_mcp_server        │     │  McpAgentClient              │
│  (server.py)             │     │  (client.py)                 │
│  ─ FastMCP instance      │     │  ─ streamablehttp_client     │
│  ─ mount_registry_on_mcp │     │  ─ list_tools cache per conn │
└──────────┬───────────────┘     │  ─ call_tool(name, args)     │
           │ montuje Tool→MCP    └──────────────────────────────┘
           ▼
┌──────────────────────────┐
│  mount_registry_on_mcp   │
│  (adapter.py)            │
│  ─ Tool.input_schema     │
│    → FastMCPTool.params  │
│  ─ dispatch_fn           │
│    → registry.dispatch   │
└──────────────────────────┘
```

### Publiczne eksporty (``src.agent.mcp``)

| Symbol                    | Rola                                          |
|---------------------------|-----------------------------------------------|
| ``McpSettings``           | dataclass: host / port / transport / enabled  |
| ``build_mcp_server``      | factory: ``ToolRegistry`` → ``FastMCP``       |
| ``mount_registry_on_mcp`` | adapter Tool → FastMCPTool (dla advanced)     |
| ``McpRuntime``            | lifecycle ``asyncio.Task`` serwera            |
| ``McpAgentClient``        | klient streamable-http uzywany przez ``Agent``|
| ``InMemoryMcpTransport``  | stub do testow — bez HTTP, bezposrednio registry |
| ``DEFAULT_CALL_TIMEOUT_S``| timeout per ``call_tool`` (30 s)              |
| ``MCP_HTTP_PATH``         | ``"/mcp"``                                    |

## Konfiguracja

``config.yaml``:

```yaml
mcp:
  enabled: true            # Faza 7: MUSI byc true; false rzuca RuntimeError
  host: 127.0.0.1
  port: 8765
  transport: streamable-http
  startup_timeout_s: 5
```

``McpSettings`` wyswietla ``url`` jako ``http://127.0.0.1:8765/mcp``.

## Flow dispatchu

### Normalny flow (produkcja)

```
Agent.run_session
  ├─ start_mcp
  │   ├─ build_mcp_server(registry, ctx_provider, settings)
  │   │   └─ mount_registry_on_mcp → FastMCPTool per narzedzie
  │   ├─ McpRuntime.start  → uvicorn.Server na 127.0.0.1:8765
  │   └─ McpAgentClient.connect  → streamablehttp_client + initialize
  ├─ _run_tool_loop
  │   ├─ client.list_tools  (cache)
  │   ├─ provider.chat(..., tools=cached_defs)
  │   ├─ for tool_call in response.tool_calls:
  │   │   └─ client.call_tool(name, args)
  │   │       └─ HTTP POST /mcp → FastMCP → registry.dispatch(ctx)
  │   │           → Tool.execute(args, ctx) → ToolResult
  │   └─ jesli submit_plan: wyjdz z petli
  └─ stop_mcp
```

### Test flow (``InMemoryMcpTransport``)

```python
from src.agent.mcp import InMemoryMcpTransport

agent = Agent(...)
# _register_default_tools woluje sie w __init__, wiec registry jest zapelniony
transport = InMemoryMcpTransport(
    registry=agent.tool_registry,
    ctx_provider=agent._tool_ctx_provider,
)
agent.attach_mcp_client(transport)
await transport.connect()
result = await agent.run_session(commit_info)
```

``InMemoryMcpTransport.call_tool`` idzie wprost do
``ToolRegistry.dispatch`` — zero HTTP, zero FastMCP, zero kolizji portow
na CI.

## Kontrakt bledow

W **obu** transportach:

- ``Tool.execute`` zwraca ``ToolResult(ok=False, error=...)``
  → ``call_tool`` zwraca **to samo** ``ToolResult``. Brak wyjatku w petli.
- ``Tool.execute`` rzuca wyjatek → ``ToolRegistry.dispatch`` lapie,
  loguje i mapuje na ``ToolResult(ok=False, error=f"{Exception}: msg")``.
- Sieciowy blad / timeout w ``McpAgentClient`` → ``ToolResult(ok=False)``
  z ``error="mcp timeout..."`` / ``"mcp call failed: ..."``. Model LLM
  widzi "ERROR: ..." i moze sie poprawic.

## Ograniczenia (Faza 7)

- Tylko **tools** (nie MCP resources / prompts).
- Brak retry na bledach sieciowych (jeden call = jedna proba).
- Brak cachowania wynikow ``call_tool``.
- Ctx provider jest jeden na agenta — sesja nie moze miec rownoleglych
  tool callow (i tak petla jest sekwencyjna).

## Plany na przyszlosc

- Wystawienie ``templates/examples/*.md`` jako MCP resources (opcjonalne,
  zob. Faza 7 w ``REFACTOR_PLAN.md``).
- Obserwatorski mode: second klient MCP (np. mcp-inspector) podpiety
  rownolegle z ``McpAgentClient`` dla live-debuggingu.
