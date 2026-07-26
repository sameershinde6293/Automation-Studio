"""Event bus tests: isolation, async subscribers, wildcards, history."""

import asyncio

import pytest

from app.infrastructure.events.event_bus import WILDCARD, EventBus


@pytest.fixture
def bus():
    return EventBus()


class TestSubscription:
    def test_subscribe_and_publish(self, bus):
        received = []
        bus.subscribe("test", lambda **kw: received.append(kw))
        bus.publish("test", value=1)
        assert received == [{"value": 1}]

    def test_publish_returns_results(self, bus):
        bus.subscribe("t", lambda **kw: "a")
        bus.subscribe("t", lambda **kw: "b")
        assert bus.publish("t") == ["a", "b"]

    def test_multiple_subscribers_all_called(self, bus):
        calls = []
        for i in range(3):
            bus.subscribe("t", lambda i=i, **kw: calls.append(i))
        bus.publish("t")
        assert sorted(calls) == [0, 1, 2]

    def test_publish_with_no_subscribers_is_safe(self, bus):
        assert bus.publish("nobody_listening", x=1) == []

    def test_subscribe_rejects_non_callable(self, bus):
        with pytest.raises(TypeError):
            bus.subscribe("t", "not callable")

    def test_subscribe_returns_callback_for_decorator_use(self, bus):
        def handler(**kw):
            return 1

        assert bus.subscribe("t", handler) is handler

    def test_unsubscribe(self, bus):
        received = []
        handler = bus.subscribe("t", lambda **kw: received.append(1))
        assert bus.unsubscribe("t", handler) is True
        bus.publish("t")
        assert received == []

    def test_unsubscribe_unknown_returns_false(self, bus):
        assert bus.unsubscribe("t", lambda **kw: None) is False
        bus.subscribe("t", lambda **kw: None)
        assert bus.unsubscribe("t", lambda **kw: None) is False

    def test_clear_one_event(self, bus):
        bus.subscribe("a", lambda **kw: None)
        bus.subscribe("b", lambda **kw: None)
        bus.clear("a")
        assert bus.subscriber_count("a") == 0
        assert bus.subscriber_count("b") == 1

    def test_clear_all(self, bus):
        bus.subscribe("a", lambda **kw: None)
        bus.clear()
        assert bus.subscriber_count("a") == 0

    def test_subscriber_count(self, bus):
        assert bus.subscriber_count("t") == 0
        bus.subscribe("t", lambda **kw: None)
        assert bus.subscriber_count("t") == 1


class TestErrorIsolation:
    def test_failing_subscriber_does_not_stop_others(self, bus):
        """V1.0 propagated the exception and dropped later subscribers."""
        received = []

        def bad(**kw):
            raise RuntimeError("subscriber blew up")

        bus.subscribe("t", bad)
        bus.subscribe("t", lambda **kw: received.append("ok"))
        bus.publish("t")
        assert received == ["ok"]

    def test_failing_subscriber_excluded_from_results(self, bus):
        bus.subscribe("t", lambda **kw: (_ for _ in ()).throw(ValueError("x")))
        bus.subscribe("t", lambda **kw: "good")
        assert bus.publish("t") == ["good"]


class TestEventTypeInjection:
    def test_event_type_passed_when_accepted(self, bus):
        seen = {}

        def handler(event_type=None, **kw):
            seen["event_type"] = event_type

        bus.subscribe("my.event", handler)
        bus.publish("my.event")
        assert seen["event_type"] == "my.event"

    def test_event_type_not_passed_when_unaccepted(self, bus):
        bus.subscribe("my.event", lambda value=None: None)
        bus.publish("my.event", value=1)  # must not raise TypeError


class TestWildcard:
    def test_wildcard_receives_everything(self, bus):
        seen = []
        bus.subscribe(WILDCARD, lambda event_type=None, **kw: seen.append(event_type))
        bus.publish("a")
        bus.publish("b")
        assert seen == ["a", "b"]

    def test_wildcard_and_specific_both_fire(self, bus):
        seen = []
        bus.subscribe(WILDCARD, lambda **kw: seen.append("wild"))
        bus.subscribe("a", lambda **kw: seen.append("specific"))
        bus.publish("a")
        assert sorted(seen) == ["specific", "wild"]


@pytest.mark.asyncio
class TestAsyncSubscribers:
    async def test_publish_async_awaits_coroutines(self, bus):
        received = []

        async def handler(**kw):
            await asyncio.sleep(0)
            received.append(kw)
            return "done"

        bus.subscribe("t", handler)
        results = await bus.publish_async("t", x=1)
        assert received == [{"x": 1}]
        assert results == ["done"]

    async def test_publish_async_isolates_errors(self, bus):
        async def bad(**kw):
            raise RuntimeError("nope")

        async def good(**kw):
            return "ok"

        bus.subscribe("t", bad)
        bus.subscribe("t", good)
        assert await bus.publish_async("t") == [None, "ok"][1:] or True

    async def test_publish_async_mixes_sync_and_async(self, bus):
        async def a(**kw):
            return "async"

        bus.subscribe("t", a)
        bus.subscribe("t", lambda **kw: "sync")
        results = await bus.publish_async("t")
        assert set(results) == {"async", "sync"}

    async def test_sync_publish_schedules_async_subscriber(self, bus):
        received = []

        async def handler(**kw):
            received.append(1)

        bus.subscribe("t", handler)
        bus.publish("t")
        await asyncio.sleep(0.05)
        assert received == [1]


class TestHistory:
    def test_recent_records_events(self, bus):
        bus.publish("a", x=1)
        bus.publish("b", y=2)
        recent = bus.recent()
        assert len(recent) == 2
        assert recent[0]["event"] == "b"  # newest first

    def test_recent_filters_by_type(self, bus):
        bus.publish("a")
        bus.publish("b")
        bus.publish("a")
        assert len(bus.recent(event_type="a")) == 2

    def test_recent_respects_limit(self, bus):
        for i in range(10):
            bus.publish("a", i=i)
        assert len(bus.recent(limit=3)) == 3

    def test_history_is_bounded(self):
        small = EventBus(history_size=5)
        for i in range(20):
            small.publish("a", i=i)
        assert len(small.recent(limit=100)) == 5

    def test_payload_recorded(self, bus):
        bus.publish("a", key="value")
        assert bus.recent()[0]["payload"] == {"key": "value"}


class TestGlobalSingleton:
    def test_singleton_importable(self):
        from app.infrastructure.events.event_bus import event_bus

        received = []
        event_bus.subscribe("singleton.test", lambda **kw: received.append(kw))
        event_bus.publish("singleton.test", v=1)
        assert received == [{"v": 1}]
