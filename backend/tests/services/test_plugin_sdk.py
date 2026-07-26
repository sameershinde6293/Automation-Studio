"""Plugin SDK tests: hook isolation, priorities, async hooks, node types."""

import pytest

from app.services.plugin_sdk.sdk import HookResult, PluginSDK


@pytest.fixture
def sdk():
    return PluginSDK()


class TestHookRegistration:
    def test_register_and_trigger(self, sdk):
        sdk.register_hook("h", lambda **kw: "result")
        assert sdk.trigger_hook("h") == ["result"]

    def test_trigger_passes_kwargs(self, sdk):
        sdk.register_hook("h", lambda value=None, **kw: value * 2)
        assert sdk.trigger_hook("h", value=21) == [42]

    def test_unknown_hook_returns_empty(self, sdk):
        assert sdk.trigger_hook("nope") == []

    def test_register_rejects_non_callable(self, sdk):
        with pytest.raises(TypeError):
            sdk.register_hook("h", "not callable")

    def test_legacy_registered_hooks_property(self, sdk):
        def handler(**kw):
            return 1

        sdk.register_hook("h", handler)
        assert sdk.registered_hooks == {"h": [handler]}

    def test_hook_count(self, sdk):
        assert sdk.hook_count("h") == 0
        sdk.register_hook("h", lambda **kw: None)
        sdk.register_hook("h", lambda **kw: None)
        assert sdk.hook_count("h") == 2

    def test_priority_ordering(self, sdk):
        order = []
        sdk.register_hook("h", lambda **kw: order.append("late"), priority=200)
        sdk.register_hook("h", lambda **kw: order.append("early"), priority=1)
        sdk.trigger_hook("h")
        assert order == ["early", "late"]

    def test_unregister_hook(self, sdk):
        handler = sdk.register_hook("h", lambda **kw: 1)
        assert sdk.unregister_hook("h", handler) is True
        assert sdk.trigger_hook("h") == []

    def test_unregister_unknown(self, sdk):
        assert sdk.unregister_hook("h", lambda **kw: None) is False
        sdk.register_hook("h", lambda **kw: None)
        assert sdk.unregister_hook("h", lambda **kw: None) is False

    def test_unregister_plugin_removes_all_its_hooks(self, sdk):
        sdk.register_hook("a", lambda **kw: 1, plugin="p1")
        sdk.register_hook("b", lambda **kw: 1, plugin="p1")
        sdk.register_hook("c", lambda **kw: 1, plugin="p2")
        assert sdk.unregister_plugin("p1") == 2
        assert sdk.trigger_hook("a") == []
        assert sdk.trigger_hook("c") == [1]

    def test_clear_hooks_one(self, sdk):
        sdk.register_hook("a", lambda **kw: 1)
        sdk.register_hook("b", lambda **kw: 1)
        sdk.clear_hooks("a")
        assert sdk.hook_count("a") == 0
        assert sdk.hook_count("b") == 1

    def test_clear_hooks_all(self, sdk):
        sdk.register_hook("a", lambda **kw: 1)
        sdk.clear_hooks()
        assert sdk.hook_count("a") == 0

    def test_list_hooks_metadata(self, sdk):
        sdk.register_hook(
            "h", lambda **kw: 1, plugin="demo", priority=5, metadata={"k": "v"}
        )
        listing = sdk.list_hooks()
        assert listing["h"][0]["plugin"] == "demo"
        assert listing["h"][0]["priority"] == 5
        assert listing["h"][0]["metadata"] == {"k": "v"}
        assert listing["h"][0]["is_async"] is False


class TestErrorIsolation:
    def test_failing_hook_does_not_break_others(self, sdk):
        """V1.0 swallowed the error with a bare 'pass' and no logging."""

        def bad(**kw):
            raise RuntimeError("plugin bug")

        sdk.register_hook("h", bad)
        sdk.register_hook("h", lambda **kw: "ok")
        assert sdk.trigger_hook("h") == ["ok"]

    def test_detailed_results_report_the_failure(self, sdk):
        sdk.register_hook("h", lambda **kw: (_ for _ in ()).throw(ValueError("bad")))
        results = sdk.trigger_hook_detailed("h")
        assert len(results) == 1
        assert results[0].ok is False
        assert "ValueError" in results[0].error

    def test_detailed_results_report_success(self, sdk):
        sdk.register_hook("h", lambda **kw: 7, plugin="p")
        results = sdk.trigger_hook_detailed("h")
        assert results[0].ok is True
        assert results[0].value == 7
        assert results[0].plugin == "p"

    def test_async_hook_flagged_when_called_synchronously(self, sdk):
        async def handler(**kw):
            return 1

        sdk.register_hook("h", handler)
        results = sdk.trigger_hook_detailed("h")
        assert results[0].ok is False
        assert "async" in results[0].error


@pytest.mark.asyncio
class TestAsyncHooks:
    async def test_async_hook_awaited(self, sdk):
        async def handler(**kw):
            return "async-result"

        sdk.register_hook("h", handler)
        results = await sdk.trigger_hook_async("h")
        assert results[0].value == "async-result"

    async def test_sync_hook_also_works_in_async_trigger(self, sdk):
        sdk.register_hook("h", lambda **kw: "sync")
        results = await sdk.trigger_hook_async("h")
        assert results[0].value == "sync"

    async def test_async_hook_error_isolated(self, sdk):
        async def bad(**kw):
            raise RuntimeError("nope")

        async def good(**kw):
            return "ok"

        sdk.register_hook("h", bad)
        sdk.register_hook("h", good)
        results = await sdk.trigger_hook_async("h")
        assert results[0].ok is False
        assert results[1].value == "ok"


class TestNodeTypeExtension:
    def test_register_node_type(self, sdk):
        from app.services.workflow.executors import BaseNodeExecutor, executor_registry

        class Custom(BaseNodeExecutor):
            async def execute(self, node, context):
                return {"ok": True}

        try:
            sdk.register_node_type("sdk_custom", Custom(), plugin="demo")
            assert executor_registry.has("sdk_custom")
            assert sdk.list_node_types() == {"sdk_custom": "demo"}
        finally:
            executor_registry.unregister("sdk_custom")

    def test_unregister_plugin_removes_node_type(self, sdk):
        from app.services.workflow.executors import BaseNodeExecutor, executor_registry

        class Custom(BaseNodeExecutor):
            async def execute(self, node, context):
                return {}

        sdk.register_node_type("sdk_custom2", Custom(), plugin="demo")
        sdk.unregister_plugin("demo")
        assert executor_registry.has("sdk_custom2") is False


class TestHookResult:
    def test_ok_property(self):
        assert HookResult(hook="h", plugin="p", value=1).ok is True
        assert HookResult(hook="h", plugin="p", error="x").ok is False


class TestWellKnownHooks:
    def test_hook_name_constants_exist(self, sdk):
        for attr in (
            "HOOK_WORKFLOW_NODE_BEFORE",
            "HOOK_WORKFLOW_NODE_AFTER",
            "HOOK_MEDIA_ASSET_PROCESSED",
            "HOOK_AI_BEFORE_GENERATE",
            "HOOK_AI_AFTER_GENERATE",
            "HOOK_APP_STARTUP",
            "HOOK_APP_SHUTDOWN",
        ):
            assert isinstance(getattr(sdk, attr), str)

    def test_global_singleton(self):
        from app.services.plugin_sdk.sdk import plugin_sdk

        plugin_sdk.register_hook("singleton.test", lambda **kw: "v")
        assert plugin_sdk.trigger_hook("singleton.test") == ["v"]
