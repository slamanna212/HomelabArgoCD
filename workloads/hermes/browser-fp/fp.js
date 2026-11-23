/* Browser identity shim — Linux desktop persona.
 *
 * Injected at document_start into the main world of every page by supervisor.py.
 * Persona: Linux x86_64 desktop, Intel UHD 630 (Mesa), 12 cores, 8-16 GB, no camera.
 * Every value here is chosen to agree with the rest of the machine (UA string,
 * timezone, egress, screen 1600x1000, window 1440x900) rather than to look exotic.
 *
 * Idempotent: safe to run repeatedly on the same document.
 */
(() => {
  if (window.__fpId !== undefined) return;
  Object.defineProperty(window, '__fpId', { value: 1, enumerable: false, configurable: false });

  const CORE = 12;
  const MEM = 8; /* navigator.deviceMemory is capped at 8 by Chrome */
  const GPU_VENDOR = 'Google Inc. (Intel)';
  const GPU_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)';

  const native = (fn, name) => {
    try {
      Object.defineProperty(fn, 'name', { value: name, configurable: true });
      Object.defineProperty(fn, 'toString', {
        value: () => `function ${name}() { [native code] }`,
        configurable: true,
        writable: true,
      });
    } catch (e) {}
    return fn;
  };
  const define = (proto, prop, getter) => {
    try {
      Object.defineProperty(proto, prop, { get: getter, configurable: true, enumerable: true });
    } catch (e) {}
  };
  const defineFn = (proto, prop, fn, name) => {
    try {
      Object.defineProperty(proto, prop, { value: native(fn, name || prop), configurable: true, writable: true });
    } catch (e) {}
  };

  /* ---- hardware narrative ---- */
  define(Navigator.prototype, 'hardwareConcurrency', () => CORE);
  define(Navigator.prototype, 'deviceMemory', () => MEM);

  /* ---- WebGL / ANGLE: software rasteriser reported as a real desktop GPU ---- */
  const OVER = {};
  OVER[37445] = GPU_VENDOR;      /* UNMASKED_VENDOR_WEBGL */
  OVER[37446] = GPU_RENDERER;    /* UNMASKED_RENDERER_WEBGL */
  OVER[3379] = 16384;            /* MAX_TEXTURE_SIZE */
  OVER[34076] = 16384;           /* MAX_CUBE_MAP_TEXTURE_SIZE */
  OVER[34930] = 16;              /* MAX_TEXTURE_IMAGE_UNITS */
  OVER[35660] = 16;              /* MAX_COMBINED_TEXTURE_IMAGE_UNITS */
  OVER[35661] = 32;              /* MAX_VERTEX_TEXTURE_IMAGE_UNITS */
  OVER[36347] = 4096;            /* MAX_VERTEX_UNIFORM_VECTORS */
  OVER[36348] = 30;              /* MAX_VARYING_VECTORS */
  OVER[3386] = 'VIEWPORT';       /* MAX_VIEWPORT_DIMS */

  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    const patched = function getParameter(p) {
      if (p === 3386) return new Int32Array([16384, 16384]);
      const v = OVER[p];
      if (v !== undefined) return v;
      return orig.call(this, p);
    };
    defineFn(proto, 'getParameter', patched, 'getParameter');
  };
  try {
    patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}

  /* ---- WebGPU adapter info (leaks the software backend otherwise) ---- */
  try {
    const gpu = navigator.gpu;
    if (gpu && gpu.requestAdapter) {
      const origReq = gpu.requestAdapter.bind(gpu);
      const patchedReq = async function requestAdapter(...args) {
        const adapter = await origReq(...args);
        try {
          if (adapter) {
            Object.defineProperty(adapter, 'info', {
              configurable: true,
              value: { vendor: 'intel', architecture: 'gen-9', device: '', description: 'Intel(R) UHD Graphics 630' },
            });
          }
        } catch (e) {}
        return adapter;
      };
      Object.defineProperty(gpu, 'requestAdapter', {
        value: native(patchedReq, 'requestAdapter'),
        configurable: true,
        writable: true,
      });
    }
  } catch (e) {}

  /* ---- media devices: a desktop has audio hardware ---- */
  try {
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      const DEVICES = [
        { deviceId: 'default', kind: 'audioinput', label: 'Built-in Audio Analog Stereo', groupId: '5d2c1a4b9e7f3a6c1b8d0e2f4a6c8e1d' },
        { deviceId: 'default', kind: 'audiooutput', label: 'Built-in Audio Analog Stereo', groupId: '5d2c1a4b9e7f3a6c1b8d0e2f4a6c8e1d' },
        { deviceId: 'monitor', kind: 'audioinput', label: 'Monitor of Built-in Audio Analog Stereo', groupId: 'a1b2c3d4e5f60718293a4b5c6d7e8f90' },
      ];
      const patched = async function enumerateDevices() {
        return DEVICES.map((d) => ({
          deviceId: d.deviceId,
          kind: d.kind,
          label: d.label,
          groupId: d.groupId,
        }));
      };
      Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', {
        value: native(patched, 'enumerateDevices'),
        configurable: true,
        writable: true,
      });
    }
  } catch (e) {}

  /* ---- page state: never report a backgrounded/unfocused window ---- */
  define(Document.prototype, 'visibilityState', () => 'visible');
  define(Document.prototype, 'hidden', () => false);
  defineFn(Document.prototype, 'hasFocus', function hasFocus() { return true; }, 'hasFocus');
  define(Document.prototype, 'webkitHidden', () => false);
  define(Document.prototype, 'webkitVisibilityState', () => 'visible');

  /* ---- window geometry: a real window is always placed and sized ---- */
  try {
    const geo = (prop, value) => {
      for (const obj of [window, Window.prototype]) {
        try {
          Object.defineProperty(obj, prop, { get: () => value, configurable: true });
          return;
        } catch (e) {}
      }
    };
    if (window.outerWidth === 0) geo('outerWidth', window.innerWidth || 1440);
    if (window.outerHeight === 0) geo('outerHeight', (window.innerHeight || 813) + 87);
    if (window.screenX === 0 && window.screenY === 0) {
      geo('screenX', 80);
      geo('screenY', 60);
    }
  } catch (e) {}

  /* ---- chrome.runtime: present in Google Chrome, absent in unbranded Chromium ---- */
  try {
    const chromeObj = window.chrome || (window.chrome = {});
    if (!chromeObj.runtime) {
      const noop = function () {};
      const listener = { addListener: noop, removeListener: noop, hasListener: () => false };
      chromeObj.runtime = {
        id: undefined,
        connect: () => ({ postMessage: noop, disconnect: noop, onMessage: listener, onDisconnect: listener }),
        sendMessage: noop,
        onConnect: listener,
        onMessage: listener,
        getManifest: () => ({}),
        getURL: (p) => p,
      };
    }
  } catch (e) {}
})();
