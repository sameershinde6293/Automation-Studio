import asyncio
import logging
from typing import Dict, Any, Set
from app.infrastructure.database.database import SessionLocal
from app.domain.models.workflow import ExecutionStatus, WorkflowExecution, NodeExecution
from app.domain.repositories.workflow_repository import (
    workflow_repo, node_repo, edge_repo, workflow_execution_repo, node_execution_repo, NodeExecutionCreate
)
from app.infrastructure.events.event_bus import event_bus
from .executors import executor_registry

logger = logging.getLogger("creator_os.workflow")

class WorkflowEngine:
    def __init__(self):
        self.active_tasks: Dict[int, asyncio.Task] = {}

    def submit(self, execution_id: int):
        task = asyncio.create_task(self.run_execution(execution_id))
        self.active_tasks[execution_id] = task
        return task

    def cancel(self, execution_id: int):
        if execution_id in self.active_tasks:
            self.active_tasks[execution_id].cancel()

    def _fetch_graph(self, execution_id: int):
        with SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if not execution:
                raise ValueError(f"Execution {execution_id} not found")
            nodes = node_repo.get_by_workflow(db, execution.workflow_id)
            edges = edge_repo.get_by_workflow(db, execution.workflow_id)
            return nodes, edges

    def _update_execution_status(self, execution_id: int, status: ExecutionStatus, error: str = None):
        with SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            execution.status = status
            if error:
                execution.error = error
            db.commit()

    def _update_node_status(self, execution_id: int, node_id: int, status: ExecutionStatus, result: Any = None, error: str = None):
        with SessionLocal() as db:
            node_exec = node_execution_repo.get_by_execution_and_node(db, execution_id, node_id)
            if not node_exec:
                node_exec = node_execution_repo.create(db, NodeExecutionCreate(
                    execution_id=execution_id, node_id=node_id, status=status.value
                ))
            node_exec.status = status
            if result:
                node_exec.output_data = result
            if error:
                node_exec.error = error
            db.commit()

    async def _run_node_with_retry(self, execution_id: int, node: Any, context: Dict[int, Any]):
        max_retries = 3
        base_delay = 1
        await asyncio.to_thread(self._update_node_status, execution_id, node.id, ExecutionStatus.RUNNING)
        
        for attempt in range(max_retries):
            try:
                executor = executor_registry.get_executor(node.node_type)
                result = await executor.execute(node, context)
                await asyncio.to_thread(self._update_node_status, execution_id, node.id, ExecutionStatus.COMPLETED, result)
                return node.id, result
            except asyncio.CancelledError:
                await asyncio.to_thread(self._update_node_status, execution_id, node.id, ExecutionStatus.CANCELLED)
                raise
            except Exception as e:
                if attempt == max_retries - 1:
                    await asyncio.to_thread(self._update_node_status, execution_id, node.id, ExecutionStatus.FAILED, None, str(e))
                    raise
                await asyncio.to_thread(self._update_node_status, execution_id, node.id, ExecutionStatus.RUNNING, None, f"Retry {attempt+1}")
                await asyncio.sleep(base_delay * (2 ** attempt))

    async def run_execution(self, execution_id: int):
        try:
            nodes, edges = await asyncio.to_thread(self._fetch_graph, execution_id)
        except Exception as e:
            logger.error(f"Failed to fetch graph for {execution_id}: {e}")
            return

        deps = {n.id: set() for n in nodes}
        for e in edges:
            deps[e.target_id].add(e.source_id)

        completed = set()
        running = set()
        tasks: Dict[int, asyncio.Task] = {}
        context = {}

        await asyncio.to_thread(self._update_execution_status, execution_id, ExecutionStatus.RUNNING)
        logger.info(f"Execution {execution_id} STARTED")

        try:
            while len(completed) < len(nodes):
                ready_nodes = [
                    n for n in nodes 
                    if n.id not in completed and n.id not in running and deps[n.id].issubset(completed)
                ]

                for node in ready_nodes:
                    running.add(node.id)
                    tasks[node.id] = asyncio.create_task(
                        self._run_node_with_retry(execution_id, node, context)
                    )

                if not tasks:
                    if running:
                        pass # waiting
                    else:
                        raise Exception("Workflow deadlock detected: No tasks running and nodes remain incomplete.")

                done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    node_id, result = task.result()
                    del tasks[node_id]
                    running.remove(node_id)
                    completed.add(node_id)
                    context[node_id] = result

            await asyncio.to_thread(self._update_execution_status, execution_id, ExecutionStatus.COMPLETED)
            logger.info(f"Execution {execution_id} COMPLETED")
            
        except asyncio.CancelledError:
            await asyncio.to_thread(self._update_execution_status, execution_id, ExecutionStatus.CANCELLED)
            for t in tasks.values():
                t.cancel()
            logger.info(f"Execution {execution_id} CANCELLED")
        except Exception as e:
            await asyncio.to_thread(self._update_execution_status, execution_id, ExecutionStatus.FAILED, str(e))
            for t in tasks.values():
                t.cancel()
            logger.error(f"Execution {execution_id} FAILED: {e}")

workflow_engine = WorkflowEngine()
