# Plugin Interface (D.8)

Autopilot plugins are **your own Python files** that run inside the app
with full access to core services (database, config, cache, hardware,
event bus). They are **standalone commands**, not pipeline stages — the
v1 render pipeline order is fixed and honest by design. Typical uses:
post-render renames/moves, project reports, cleanup jobs, notifications.

## Contract

1. Create `plugins/<name>.py`.
2. Register it in `config/plugins_config.json`:
   `{"name": "<name>", "enabled": true}`
3. The file must contain:
   - `PLUGIN_API = 1` (integer; the loader refuses mismatches so old
     plugins fail loudly instead of silently misbehaving)
   - exactly **one** `BaseModule` subclass with a
     `run(context) -> dict` method (no `__init__` boilerplate
     needed — the loader passes your plugin name to BaseModule)

```python
from core.service_container import BaseModule

PLUGIN_API = 1

class MyPlugin(BaseModule):
    def is_optional_module(self) -> bool:
        return True

    def run(self, context):
        count = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM projects")["n"]
        return {"success": True, "data": {"projects": count}}
```

## Running

```bash
python main.py plugin --list                 # load status of every plugin
python main.py plugin <name>                 # run with empty context
python main.py plugin <name> --arg k=v --arg x=y   # context pairs
python main.py modules                       # modules + plugins status
```

Exit code is 0 on success, 1 on plugin failure, 2 on bad `--arg`.

## Rules that keep plugins safe to load

- **RULE 1 applies to you too**: never import from `modules/*`. Use the
  injected services: `self.db`, `self.config`, `self.cache`,
  `self.hardware`, `self.event_bus`, `self.log`. Stay in your own file.
- **Return a dict.** A standard response (`self.make_response(...)`,
  see `plugins/hello_autopilot.py`) or any plain dict — the
  orchestrator normalizes either into the standard response shape.
- **Crashes are isolated**: an exception becomes a failed response plus
  a `plugin.failed` event; the app never dies from a plugin.
  `plugin.started` / `plugin.completed` also ride the event bus.
- **Load report honesty**: missing file, syntax error, missing
  `BaseModule` subclass, or wrong `PLUGIN_API` all show up as
  `[XX] <name> (<reason>)` in `plugin --list`. Disabled entries show
  `[--]`.
- **Frozen exe**: plugins load from their file path, so dropping a
  `.py` into `_internal\plugins\` beside the embedded example (and
  registering it in `_internal\config\plugins_config.json`) works
  without rebuilding.

## Security note

A plugin is arbitrary Python running with YOUR user rights. Only
install plugins you wrote yourself or have read end-to-end. Autopilot
performs no sandboxing — the interface is a power-user feature.
