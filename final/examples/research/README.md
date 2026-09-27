# Research examples

Run commands from the final/ directory.

## Offline execution fixture

~~~sh
python examples/research/offline_demo.py
~~~

This deterministic example injects a scripted model and two explicitly labelled
local fixture documents. It exercises two search/read/assess rounds, source
registration, numbered citations, and a Markdown artifact. It needs no API key,
network, database, or Docker. The report is written to
runtime/research-demo/report.md.

The fixture demonstrates the engine contract and its control flow. It is not
a real LLM evaluation or external fact investigation. Production providers never
fall back to these scripted responses.

## Real research through the durable API

Configure a real LLM in config/conf.yaml, start the application, then use
[scripts/demo_research.py](../../scripts/demo_research.py). Check its --help
for the current command options. It submits a durable run and shows the generated
plan. Add --approve-plan to approve it and download the result, or review the
plan in the workbench and continue with --run-id. Research can use uploaded knowledge
base passages or real Tavily search results when a search key is configured.

The browser research workbench supports editing the plan before approving it.
See [the quickstart](../../docs/research-quickstart.md) for configuration and startup.

## Provider integration

ResearchEngine(llm, search=..., reader=..., sandbox=...) accepts explicit
providers. Search returns dictionaries with a real URL or stable
url_or_doc_id="doc:...", a title, and retrieved content / raw_content.
Optional reader(url, token=...) returns page text. Callables receive a
cancellation token. The default production adapter is
ResearchEngine.from_agent(agent).

The production adapter honors the tool manifest and each approved step's
tool policy. Python runs only in a Docker sandbox with networking disabled and
a read-only root filesystem; otherwise the engine labels the artifact as
generated code that was not executed.
