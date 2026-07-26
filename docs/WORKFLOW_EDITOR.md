# Workflow Editor (M3)

Production-quality visual editor built on React Flow.

## Features
- Infinite canvas with pan/zoom/snap/grid/minimap
- 22 fully functional node types with editable configuration
- Drag-to-connect, reconnect, cycle prevention
- Full keyboard shortcuts, box/multi selection, clipboard
- Real backend integration (save/load/validate/execute)
- Properties panel, execution visualization, dirty state
- Autosave + versioned persistence

## Architecture
- `WorkflowCanvas.tsx` — React Flow core
- `BaseNode.tsx` — unified node component
- `workflowStore.ts` — Zustand + backend sync
- `PropertiesPanel.tsx` / `ExecutionPanel.tsx`

All nodes support serialization and execution state.