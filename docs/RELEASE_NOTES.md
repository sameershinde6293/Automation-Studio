# Release Notes

## Version 0.3.0-alpha
The Workflow Engine has been implemented and successfully unit-tested. It processes Directed Acyclic Graphs (DAGs) and executes dependencies in parallel using standard `asyncio` task queues. Resiliency is built in with an automatic retry policy, and tasks support checkpointing and cancellation.
