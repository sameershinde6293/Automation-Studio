# Creator OS - User Guide

## Introduction
Creator OS is a modular desktop application designed for AI orchestration, media processing, and automated workflows.

## Features
- **Workflows:** Build Directed Acyclic Graphs (DAGs) to automate tasks.
- **AI Runtime:** Interact with local (Ollama) and cloud (OpenAI) LLMs.
- **Media Pipeline:** Automatically process images, audio, and video assets.
- **Plugins:** Extend Creator OS with custom scripts and tools.

## Getting Started
1. Launch the application (see [INSTALLATION_GUIDE.md](INSTALLATION_GUIDE.md)).
2. Navigate to the **Workflows** tab to begin designing your automation graph.
3. Configure API keys in `.env` to enable OpenAI. With none configured, the
   provider fallback chain ends at the built-in `mock` provider, so AI
   workflows still run.

## Learn by example

Four ready-to-run workflows ship in [`examples/`](../examples/README.md),
covering AI chaining, resilient HTTP with retries and failure branching,
scheduled batch processing, and a dependency-free smoke test. All four are
verified against a live backend on every release:

```bash
python scripts/verify_examples.py
```

## Template syntax

Variables seeded by the `start` node are referenced **through the node**:

```
{{ Start.variables.my_var }}    correct
{{ my_var }}                    renders empty
{{ NodeName.field }}            an upstream node's output
{{ item }}                      current element inside a loop
```

An empty render is not an error and produces no warning, so check
`state.variables` and each node's `output_data` in the execution detail if a
node receives something unexpected.

## When something goes wrong

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) and [FAQ.md](FAQ.md).
