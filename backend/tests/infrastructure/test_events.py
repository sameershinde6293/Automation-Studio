from app.infrastructure.events.event_bus import EventBus

def test_event_bus():
    bus = EventBus()
    state = {"called": False, "value": None}
    
    def test_handler(value):
        state["called"] = True
        state["value"] = value
        
    bus.subscribe("test_event", test_handler)
    bus.publish("test_event", value="hello")
    
    assert state["called"] is True
    assert state["value"] == "hello"
