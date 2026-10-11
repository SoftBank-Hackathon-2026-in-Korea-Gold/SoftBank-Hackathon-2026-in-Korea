import { afterEach, describe, expect, it, vi } from 'vitest';
import { subscribe } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('SSE error events', () => {
  it('delivers server failures and handles connection errors without spurious log rows', () => {
    let stream;
    vi.stubGlobal('localStorage', { getItem: () => 'test token' });
    vi.stubGlobal('EventSource', class {
      constructor(url) { this.url = url; this.listeners = {}; stream = this; }
      addEventListener(type, callback) { this.listeners[type] = callback; }
      close() { this.closed = true; }
    });
    const onEvent = vi.fn();
    const onFail = vi.fn();
    const close = subscribe('job', onEvent, onFail);
    const raw = { payload: { target: 'local', message: 'failed' }, ts: 1 };
    const serverError = { data: JSON.stringify(raw) };
    stream.listeners.error(serverError);
    stream.onerror(serverError);
    expect(onEvent).toHaveBeenCalledExactlyOnceWith('error', raw.payload, raw);
    expect(onFail).not.toHaveBeenCalled();
    stream.listeners.error({});
    stream.onerror({});
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onFail).toHaveBeenCalledOnce();
    expect(stream.url).toBe('/deploy/job/events?token=test%20token');
    close();
    expect(stream.closed).toBe(true);
  });
});
