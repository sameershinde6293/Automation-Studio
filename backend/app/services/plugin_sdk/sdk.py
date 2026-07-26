from typing import Callable, Dict, Any

class PluginSDK:
    def __init__(self):
        self.registered_hooks: Dict[str, list[Callable]] = {}

    def register_hook(self, hook_name: str, callback: Callable):
        if hook_name not in self.registered_hooks:
            self.registered_hooks[hook_name] = []
        self.registered_hooks[hook_name].append(callback)

    def trigger_hook(self, hook_name: str, **kwargs) -> list[Any]:
        results = []
        for callback in self.registered_hooks.get(hook_name, []):
            try:
                results.append(callback(**kwargs))
            except Exception as e:
                # Log error and continue
                pass
        return results

plugin_sdk = PluginSDK()
