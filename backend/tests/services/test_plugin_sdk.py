from app.services.plugin_sdk.sdk import plugin_sdk

def test_plugin_sdk():
    state = {"called": False}
    
    def my_hook(val):
        state["called"] = True
        return val * 2
        
    plugin_sdk.register_hook("test_hook", my_hook)
    res = plugin_sdk.trigger_hook("test_hook", val=10)
    
    assert state["called"] is True
    assert res == [20]
