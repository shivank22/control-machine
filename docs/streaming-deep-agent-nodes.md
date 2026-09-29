# Emitting events from an orchestrator whose nodes are Deep Agents

A Deep Agent is a compiled LangGraph. Once you put it inside another graph, its tokens, tool calls, custom progress, and interrupts stay inside that subgraph unless the parent stream is opened in a way that includes subgraphs.

`create_deep_agent(..., name="design")` returns that compiled graph. Pass `name` so the stream can label the node. Keep the checkpointer on the orchestrator only.

Sources:

- [Streaming — subgraph outputs](https://docs.langchain.com/oss/python/langgraph/streaming#subgraph-outputs)
- [Deep Agents streaming](https://docs.langchain.com/oss/python/deepagents/streaming)
- [Deep Agents event streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming)
- [LangGraph event streaming](https://docs.langchain.com/oss/python/langgraph/event-streaming)
- [Use subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)

## 1. Put each Deep Agent on the parent graph

Two legal shapes. Both can emit inner events. They differ in how state is mapped.

**Shared state keys.** Add the compiled agent as a node. LangGraph treats it as a subgraph. No wrapper.

```python
from langgraph.graph import END, START, StateGraph

orchestrator = (
    StateGraph(OrchestratorState)
    .add_node("supervisor", supervisor_agent)  # create_deep_agent(..., name="supervisor")
    .add_node("design", design_agent)          # create_deep_agent(..., name="design")
    .add_edge(START, "supervisor")
    .add_edge("supervisor", "design")
    .add_edge("design", END)
    .compile(checkpointer=checkpointer)
)
```

**Different state schemas.** Call the agent from a node and pass the parent `RunnableConfig`. That config is what joins the inner run to the parent stream, the parent checkpointer, and parent interrupts. A bare `ainvoke` without `config` finishes the Deep Agent privately. The parent then sees only the value the node returns.

```python
from langchain_core.runnables import RunnableConfig

async def design_node(state: OrchestratorState, config: RunnableConfig):
    result = await design_agent.ainvoke(
        {"messages": state["messages"]},
        config,
    )
    return {"design_report": result["messages"][-1].text}
```

Do not also call `design_agent.astream` inside that node. The parent stream is the consumer. A second stream inside the node takes the events away from it.

## 2. Open the parent stream with subgraphs on

Without `subgraphs=True`, every `stream_mode` — including `"messages"` — returns only the parent graph. Token chunks from the Deep Agent's model node never appear. This shows up only after the agent is wrapped in a graph. Calling `design_agent.stream(...)` directly still shows them, which hides the bug.

Subscribe to every channel you want. Unsubscribed channels are not emitted.

| Mode | What arrives from inside a Deep Agent node |
| --- | --- |
| `updates` | One delta per inner node (`model`, `tools`, middleware). `__interrupt__` when the agent pauses. |
| `messages` | LLM token chunks. `ns` says which agent produced them. |
| `custom` | Payloads from `get_stream_writer()` inside that agent's tools or nodes. |
| `tasks` | Node start and finish, including inner nodes. |

LangGraph 1.1+ `version="v2"` gives every chunk the same shape: `type`, `ns`, `data`.

```python
async for chunk in orchestrator.astream(
    {"messages": [{"role": "user", "content": prompt}]},
    config={"configurable": {"thread_id": thread_id}},
    stream_mode=["updates", "messages", "custom", "tasks"],
    subgraphs=True,
    version="v2",
):
    source = chunk["ns"] or ("orchestrator",)
    if chunk["type"] == "messages":
        token, metadata = chunk["data"]
        # metadata["langgraph_node"] is "model" for assistant text
        ...
    elif chunk["type"] == "updates":
        ...
    elif chunk["type"] == "custom":
        ...
    elif chunk["type"] == "tasks":
        ...
```

`ns` is the path from the orchestrator to the emitter.

| `ns` | Who emitted it |
| --- | --- |
| `()` | The orchestrator graph itself |
| `("design:<task_id>",)` | The design Deep Agent node |
| `("design:<task_id>", "tools:<call_id>")` | A `task` subagent the design agent delegated to |
| `("design:<task_id>", "tools:<call_id>", "model_request:<id>")` | The model step inside that subagent |

The segment before `:` is the stable name (`design`, `tools`, `model`). The suffix is a per-run id. Route the UI on the name, not the suffix.

Deep Agents also emit updates from middleware nodes (`SkillsMiddleware`, todo middleware, and similar). For a user-facing feed, keep `model` and `tools` (and your own node names) and drop the rest. Filter on node name. An empty-namespace filter drops the design agent entirely, because its events never have `ns == ()`.

## 3. Emit your own events from inside the Deep Agent

Built-in modes already cover tokens, tool calls, and node updates. Use a custom event only for progress the graph does not already represent.

Call `get_stream_writer()` inside a tool or node of the Deep Agent. The parent receives it on the `custom` channel, with that agent's namespace, only when the parent stream includes both `stream_mode` containing `"custom"` and `subgraphs=True`.

```python
from langchain.tools import tool
from langgraph.config import get_stream_writer

@tool
def draft_section(title: str) -> str:
    """Draft one section and report progress."""
    writer = get_stream_writer()
    writer({"agent": "design", "status": "drafting", "title": title})
    ...
    writer({"agent": "design", "status": "done", "title": title})
    return text
```

The payload must be JSON-serializable. On Python older than 3.11, `get_stream_writer()` does not work in async code. Pass `RunnableConfig` into `ainvoke` instead, or take a `writer` argument on the node.

## 4. Read the same run through event streaming

`stream_events(..., version="v3")` is the other API. It normalizes the same subgraph output into projections. Stream the orchestrator, not each Deep Agent.

```python
stream = orchestrator.stream_events(
    {"messages": [{"role": "user", "content": prompt}]},
    config={"configurable": {"thread_id": thread_id}},
    version="v3",
)

for subgraph in stream.subgraphs:
    # graph_name is the name= passed to create_deep_agent, or the node name
    print(subgraph.graph_name, subgraph.path)
    for message in subgraph.messages:
        print(message.text)
```

Two projections are easy to mix up:

- `stream.subgraphs` is every nested compiled graph. This is the design node and the supervisor node.
- `stream.subagents` is a Deep Agents `task` delegation inside one of those agents. A design node that is itself a graph node does not appear here.

Each subgraph handle has the same projections as the parent: `.messages`, `.values`, and, on a Deep Agent, nested `.subagents` and `.tool_calls`. Iterate `subagent.subagents` when the design agent delegates further.

Raw protocol events are the full set. Iterate the stream object itself. `params.namespace` is `[]` at the orchestrator and `["design:<id>", ...]` inside a node. `seq` orders events. `timestamp` does not.

| `method` | Payload |
| --- | --- |
| `messages` | Content blocks: start, text delta, reasoning delta, finish |
| `tools` | `tool-started`, `tool-output-delta`, `tool-finished`, `tool-error` |
| `updates` | Per-node state delta |
| `values` | Full state snapshot |
| `tasks` | Pregel task created or finished |
| `lifecycle` | `started`, `running`, `completed`, `failed`, `interrupted` |
| `input` | Human-in-the-loop request and resume |
| `custom` | `get_stream_writer()` payloads |
| `checkpoints` | Checkpoint envelopes, if a checkpointer is attached |

`lifecycle` on a child carries `graph_name` and `cause` (the tool call or edge that entered that node). That is the signal that the design node started and finished, separate from its token stream.

Coordinator and subgraph output interleave. Consume `stream.messages` and `stream.subgraphs` concurrently if the UI must stay live. Draining one projection to the end before the next holds the other.

## 5. Interrupts stay on the parent resume

An `ask_user` interrupt inside the design node surfaces on the parent stream as an `__interrupt__` update and as `stream.interrupts` / a `lifecycle` event with `event == "interrupted"`. Resume the orchestrator, on the same `thread_id`, with `Command(resume=...)`. Do not resume the design agent on its own.

```python
if stream.interrupted:
    stream = orchestrator.stream_events(
        Command(resume={"decisions": [{"type": "respond", "message": answer}]}),
        config=config,
        version="v3",
    )
```

After resume, the same subgraph stream continues: later design tokens and the next interrupt use the same parent stream.

## Checklist

1. Compile supervisor and design with `create_deep_agent(..., name=...)`.
2. Add them as orchestrator nodes, or `ainvoke` them with the parent `config`.
3. Checkpoint the orchestrator only.
4. Stream the orchestrator with `subgraphs=True` and the modes you need (`updates`, `messages`, `custom`, `tasks`), `version="v2"`. Or use `stream_events(..., version="v3")` and read `stream.subgraphs`.
5. Route on `ns` / `subgraph.graph_name`. Keep inner events. Drop middleware node names, not non-empty namespaces.
6. Emit extra progress with `get_stream_writer()` and include `"custom"` in `stream_mode`.
7. Resume interrupts on the orchestrator thread.
