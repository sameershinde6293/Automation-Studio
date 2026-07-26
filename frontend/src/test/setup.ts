/**
 * Global test setup.
 *
 * jsdom does not implement several browser APIs that React Flow and the
 * execution stream rely on, so they are stubbed here rather than in every
 * individual test file.
 */

import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});

// --- React Flow requires layout + observer APIs jsdom lacks ---------------
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as any).ResizeObserver = (globalThis as any).ResizeObserver ?? ResizeObserverStub;

class DOMMatrixStub {
  m22 = 1;
  constructor(_transform?: string) {}
}
(globalThis as any).DOMMatrixReadOnly = (globalThis as any).DOMMatrixReadOnly ?? DOMMatrixStub;

if (!(globalThis as any).matchMedia) {
  (globalThis as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

if (!(Element.prototype as any).scrollTo) {
  (Element.prototype as any).scrollTo = function scrollTo() {};
}

if (!(globalThis as any).DragEvent) {
  (globalThis as any).DragEvent = class DragEvent extends Event {};
}

// jsdom has no EventSource; the execution API falls back to polling when it is
// absent, and individual tests install their own stub when they need SSE.
if (!(globalThis as any).EventSource) {
  (globalThis as any).EventSource = undefined;
}

/**
 * Minimal controllable EventSource stub for streaming tests.
 * Instances register themselves on `MockEventSource.instances`.
 */
export class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  readyState = 0;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners: Record<string, ((event: MessageEvent) => void)[]> = {};

  constructor(url: string) {
    this.url = url;
    this.readyState = 1;
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    (this.listeners[name] ||= []).push(handler);
  }

  removeEventListener(name: string, handler: (event: MessageEvent) => void) {
    this.listeners[name] = (this.listeners[name] ?? []).filter((h) => h !== handler);
  }

  /**
   * Dispatch a named event.
   *
   * Mirrors real EventSource semantics: a frame carrying an `event:` field goes
   * to the matching addEventListener handlers only. `onmessage` receives just
   * the default (unnamed) channel, so a named frame must not be delivered
   * twice.
   */
  emit(eventName: string, payload: Record<string, any>) {
    const event = { data: JSON.stringify({ event: eventName, ...payload }) } as MessageEvent;
    const named = this.listeners[eventName] ?? [];
    if (named.length > 0) {
      named.forEach((handler) => handler(event));
      return;
    }
    this.onmessage?.(event);
  }

  fail() {
    this.onerror?.(new Event('error'));
  }

  close() {
    this.readyState = 2;
  }

  static reset() {
    MockEventSource.instances = [];
  }
}

export const installMockEventSource = () => {
  MockEventSource.reset();
  (globalThis as any).EventSource = MockEventSource;
  return MockEventSource;
};

export const removeEventSource = () => {
  (globalThis as any).EventSource = undefined;
};
