# Frontend Test Coverage — Milestone 3

## Test Files Written
- `workflowStore.test.ts` — node CRUD, undo/redo, dirty state, serialization
- `baseNode.test.tsx` — rendering, config editing
- `canvas.test.tsx` — React Flow rendering
- `clipboard.test.ts` — copy/paste/duplicate
- `importExport.test.ts` — JSON import/export roundtrip

## Coverage Areas
- WorkflowStore (core state + persistence)
- BaseNode configuration & validation
- Canvas rendering & interaction
- Clipboard operations
- Import/Export & serialization

All tests are written in Vitest + React Testing Library and are ready to run once the environment allows `npm install`.