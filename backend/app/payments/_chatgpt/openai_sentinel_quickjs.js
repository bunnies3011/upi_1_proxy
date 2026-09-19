const EXPOSE_PATCH = "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});";
// `_n` = turnstile VM, `Nt`/`jt` = session-observer VM, `D` = WeakMap proof
// binder, `$` = getter của WeakMap đó.
//
// `t.sessionObserverToken` public API đọc state nội bộ theo flow (`ne.get(flow)`)
// nên không dùng được từ ngoài — phải gọi thẳng VM. SDK thật chạy so-proof theo
// HAI pass (xem `se()` → `Et()` → `jt(collector_dx, $(challenge))`, rồi
// `token()` → `Nt(snapshot_dx)`), nên cần cả `jt` (pass nạp key) lẫn `$`
// (lấy key) chứ không chỉ `Nt` như bản trước.
const EXPOSE_REPLACEMENT =
  "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t.__debug_n=_n," +
  "t.__debug_Nt=Nt,t.__debug_jt=jt,t.__debug_soKey=$,t.__debug_bindProof=D,t}({});";
const INSTANCE_PATCH = "var P=new _;";
const INSTANCE_REPLACEMENT = "var P=new _;globalThis.__debugP=P;";
const SDK_GLOBAL_PATCH = "var SentinelSDK=";
const SDK_GLOBAL_REPLACEMENT = "globalThis.SentinelSDK=";

// Trần thời gian chờ pass "collector" của VM session-observer. Ta không cần nó
// resolve — chỉ cần nó nạp xong bảng opcode + khoá XOR (chạy đồng bộ ở đầu
// `jt`). Đặt thấp hơn nhiều so với guard 60s bên trong SDK.
const SO_COLLECTOR_TIMEOUT_MS = 5000;

function bytesToBase64(bytes) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let out = "";
  let i = 0;
  while (i < bytes.length) {
    const b0 = bytes[i++] || 0;
    const b1 = bytes[i++] || 0;
    const b2 = bytes[i++] || 0;
    const n = (b0 << 16) | (b1 << 8) | b2;
    out += chars[(n >> 18) & 63];
    out += chars[(n >> 12) & 63];
    out += i - 2 < bytes.length ? chars[(n >> 6) & 63] : "=";
    out += i - 1 < bytes.length ? chars[n & 63] : "=";
  }
  return out;
}

function base64ToBytes(base64) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const clean = String(base64 || "").replace(/[^A-Za-z0-9+/=]/g, "");
  const bytes = [];
  for (let i = 0; i < clean.length; i += 4) {
    const c0 = chars.indexOf(clean[i]);
    const c1 = chars.indexOf(clean[i + 1]);
    const c2 = chars.indexOf(clean[i + 2]);
    const c3 = chars.indexOf(clean[i + 3]);
    const n = ((c0 & 63) << 18) | ((c1 & 63) << 12) | (((c2 < 0 ? 0 : c2) & 63) << 6) | ((c3 < 0 ? 0 : c3) & 63);
    bytes.push((n >> 16) & 255);
    if (clean[i + 2] !== "=") bytes.push((n >> 8) & 255);
    if (clean[i + 3] !== "=") bytes.push(n & 255);
  }
  return bytes;
}

function createStorage() {
  const map = new Map();
  return {
    get length() {
      return map.size;
    },
    clear() {
      map.clear();
    },
    getItem(key) {
      return map.has(String(key)) ? map.get(String(key)) : null;
    },
    setItem(key, value) {
      map.set(String(key), String(value));
    },
    removeItem(key) {
      map.delete(String(key));
    },
  };
}

function createElement(tagName) {
  const tag = String(tagName || "div").toLowerCase();
  return {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    style: {},
    children: [],
    src: "",
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((x) => x !== child);
      return child;
    },
    setAttribute() {},
    getAttribute() {
      return null;
    },
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect() {
      return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    },
  };
}

/**
 * Ghi đè một global kể cả khi runtime khai báo nó là accessor chỉ-có-getter.
 *
 * Node 22 định nghĩa `navigator` và `crypto` bằng `Object.defineProperty` với
 * `get` riêng và không có `set` (`writable=false, configurable=true`). Gán
 * thẳng (`globalThis.navigator = …`) ở non-strict mode KHÔNG ném lỗi — nó im
 * lặng không làm gì, nên persona của repo không bao giờ tới được sdk.js.
 */
function defineGlobal(name, value) {
  Object.defineProperty(globalThis, name, {
    value,
    writable: true,
    configurable: true,
    enumerable: true,
  });
}

/**
 * Tạo function "trông như native": `fn.toString()` ra `function name() { [native code] }`.
 *
 * sdk.js đọc `navigator[key].toString()` cho config[10]; với method của
 * `Navigator.prototype` thì Chrome thật luôn trả chuỗi `[native code]`. Function
 * JS thường sẽ lộ nguyên source `function () {}`.
 */
function nativeFunction(name) {
  const fn = function () {};
  Object.defineProperty(fn, "name", { value: name, configurable: true });
  fn.toString = () => `function ${name}() { [native code] }`;
  return fn;
}

/**
 * Dựng `navigator` có prototype thật thay vì object literal phẳng.
 *
 * sdk.js tính config[10] = `R(Object.keys(Object.getPrototypeOf(navigator)))`
 * rồi ghép `key + "−" + navigator[key].toString()`. Chrome đặt MỌI thuộc tính
 * của navigator trên `Navigator.prototype` dưới dạng accessor ENUMERABLE, nên
 * `Object.keys()` trả về vài chục key. Object literal phẳng có prototype là
 * `Object.prototype` ⇒ `Object.keys()` rỗng ⇒ `R([])` = undefined ⇒ config[10]
 * ra đúng chuỗi `"undefined"` — một tell rõ ràng.
 *
 * Chỉ nhận giá trị `.toString()` được (không null/undefined): Chrome thật cũng
 * có key trả null (`doNotTrack`) nhưng thêm vào đây thì `R()` bốc trúng sẽ ném
 * TypeError trong VM.
 */
function makeNavigator(props) {
  const proto = {};
  for (const key of Object.keys(props)) {
    const value = props[key];
    if (value === null || value === undefined) continue;
    Object.defineProperty(proto, key, {
      get() {
        return value;
      },
      enumerable: true,
      configurable: true,
    });
  }
  Object.defineProperty(proto, Symbol.toStringTag, {
    value: "Navigator",
    configurable: true,
  });
  return Object.create(proto);
}

function installRuntime(payload) {
  const screen = {
    width: Number(payload.screen_width || 1366),
    height: Number(payload.screen_height || 768),
    availWidth: Number(payload.screen_width || 1366),
    availHeight: Number(payload.screen_height || 768),
    colorDepth: 24,
    pixelDepth: 24,
  };
  // sdk.js lấy config[5] = 1 src ngẫu nhiên trong `document.scripts`. Mảng rỗng
  // ⇒ config[5] = null, không trang thật nào như vậy. Seed đúng các bundle mà
  // chatgpt.com load cạnh sdk.js.
  const scripts = [
    String(payload.sdk_url || "https://sentinel.openai.com/sentinel/sdk.js"),
    "https://cdn.oaistatic.com/assets/manifest.js",
    "https://cdn.oaistatic.com/assets/vendor.js",
    "https://cdn.oaistatic.com/assets/main.js",
    "https://cdn.oaistatic.com/assets/runtime.js",
    "https://cdn.oaistatic.com/assets/polyfills.js",
  ].map((src) => {
    const el = createElement("script");
    el.src = src;
    el.textContent = "";
    return el;
  });
  const documentElement = createElement("html");
  documentElement.clientWidth = screen.width;
  documentElement.clientHeight = screen.height;
  const document = {
    readyState: "complete",
    hidden: false,
    visibilityState: "visible",
    referrer: "https://auth.openai.com/",
    URL: "https://auth.openai.com/",
    cookie: `oai-did=${encodeURIComponent(payload.device_id || "")}`,
    scripts,
    currentScript: { src: scripts[0].src, getAttribute() { return null; } },
    documentElement,
    body: createElement("body"),
    head: createElement("head"),
    createElement(tag) {
      const el = createElement(tag);
      if (String(tag).toLowerCase() === "script") scripts.push(el);
      return el;
    },
    createElementNS(_ns, tag) {
      return this.createElement(tag);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    getElementById() {
      return null;
    },
    getElementsByTagName() {
      return [];
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return true;
    },
  };

  const performance = {
    now: () => Number(payload.performance_now || 12345.67),
    timeOrigin: Number(payload.time_origin || 1710000000000),
    memory: { jsHeapSizeLimit: Number(payload.js_heap_size_limit || 4294967296) },
  };

  class TextEncoderPoly {
    encode(text) {
      const str = String(text || "");
      const out = new Uint8Array(str.length);
      for (let i = 0; i < str.length; i += 1) out[i] = str.charCodeAt(i) & 255;
      return out;
    }
  }

  class TextDecoderPoly {
    decode(input) {
      if (!input) return "";
      let out = "";
      for (let i = 0; i < input.length; i += 1) {
        out += String.fromCharCode(input[i]);
      }
      return out;
    }
  }

  class URLSearchParamsPoly {
    constructor(search) {
      this._pairs = [];
      const s = String(search || "").replace(/^\?/, "");
      if (!s) return;
      const parts = s.split("&");
      for (const p of parts) {
        if (!p) continue;
        const i = p.indexOf("=");
        if (i < 0) {
          this._pairs.push([decodeURIComponent(p), ""]);
        } else {
          this._pairs.push([
            decodeURIComponent(p.slice(0, i)),
            decodeURIComponent(p.slice(i + 1)),
          ]);
        }
      }
    }
    keys() {
      return this._pairs.map((x) => x[0])[Symbol.iterator]();
    }
  }

  class URLPoly {
    constructor(input, base) {
      const raw = String(input || "");
      if (/^https?:\/\//i.test(raw)) {
        this.href = raw;
      } else {
        const b = String(base || "https://auth.openai.com/").replace(/\/$/, "");
        this.href = `${b}/${raw.replace(/^\//, "")}`;
      }
      const m = this.href.match(/^(https?:)\/\/([^\/]+)(\/[^?#]*)?(\?[^#]*)?(#.*)?$/i);
      this.protocol = m ? m[1] : "https:";
      this.host = m ? m[2] : "auth.openai.com";
      this.hostname = this.host;
      this.pathname = m && m[3] ? m[3] : "/";
      this.search = m && m[4] ? m[4] : "";
      this.hash = m && m[5] ? m[5] : "";
      this.origin = `${this.protocol}//${this.host}`;
    }
    toString() {
      return this.href;
    }
  }

  globalThis.window = globalThis;
  globalThis.self = globalThis;
  globalThis.top = globalThis;
  globalThis.parent = globalThis;
  globalThis.document = document;
  // Node 22 khai báo `navigator` (và `crypto`) là accessor CHỈ-CÓ-GETTER trên
  // globalThis: `globalThis.navigator = {...}` im lặng không làm gì, persona
  // injection không bao giờ tới được VM và sdk.js đọc ra `Node.js/22`. Phải đi
  // qua `Object.defineProperty` — xem `defineGlobal()`.
  defineGlobal("navigator", (function () {
    const ua = String(payload.user_agent || "Mozilla/5.0");
    // Parse brands từ payload.sec_ch_ua_brands (mảng {brand, version}) — Python pass
    // từ user_agent_profile. Nếu không có → suy ra Chromium/Google Chrome version
    // từ UA string (regex Chrome/<major>) + grease brand mặc định.
    let brands = Array.isArray(payload.sec_ch_ua_brands) ? payload.sec_ch_ua_brands : null;
    if (!brands || !brands.length) {
      const m = ua.match(/Chrome\/(\d+)/);
      const major = m ? m[1] : "145";
      brands = [
        { brand: "Chromium", version: major },
        { brand: "Google Chrome", version: major },
        { brand: "Not_A Brand", version: "24" },
      ];
    }
    const platform = String(payload.sec_ch_ua_platform || "Windows");
    const platformVersion = String(payload.sec_ch_ua_platform_version || "15.0.0");
    const isMobile = Boolean(payload.sec_ch_ua_mobile);
    const archStr = String(payload.sec_ch_ua_arch || "x86");
    const bitness = String(payload.sec_ch_ua_bitness || "64");
    const model = String(payload.sec_ch_ua_model || "");
    const fullVersion = ((brands.find(b => b.brand === "Google Chrome") || {}).version || "145") + ".0.0.0";
    const uaData = {
      brands: brands.map(b => ({ brand: String(b.brand), version: String(b.version) })),
      mobile: isMobile,
      platform: platform,
      getHighEntropyValues: function (hints) {
        const out = {
          brands: brands.map(b => ({ brand: String(b.brand), version: String(b.version) })),
          mobile: isMobile,
          platform: platform,
        };
        (hints || []).forEach(function (h) {
          if (h === "platformVersion") out.platformVersion = platformVersion;
          else if (h === "architecture") out.architecture = archStr;
          else if (h === "bitness") out.bitness = bitness;
          else if (h === "model") out.model = model;
          else if (h === "uaFullVersion") out.uaFullVersion = fullVersion;
          else if (h === "fullVersionList")
            out.fullVersionList = brands.map(b => ({
              brand: String(b.brand),
              version: String(b.version) + ".0.0.0",
            }));
          else if (h === "wow64") out.wow64 = false;
        });
        return Promise.resolve(out);
      },
      toJSON: function () {
        return { brands: brands, mobile: isMobile, platform: platform };
      },
    };
    Object.defineProperty(uaData, Symbol.toStringTag, {
      value: "NavigatorUAData",
      configurable: true,
    });
    // `makeNavigator` đặt hết lên prototype dạng accessor enumerable — xem
    // docstring của nó. Các key phụ bên dưới là những thuộc tính Chrome trên
    // Windows luôn có; chúng chỉ tồn tại để `R(Object.keys(...))` của sdk.js bốc
    // trúng một key THẬT với giá trị hợp lý, nên cố ý chỉ dùng primitive +
    // native function (không dựng sub-object giả như `permissions`/`plugins`,
    // vì thêm object rỗng sẽ biến "thiếu API" thành "gọi method không tồn tại").
    return makeNavigator({
      userAgent: ua,
      appCodeName: "Mozilla",
      appName: "Netscape",
      appVersion: ua.replace(/^Mozilla\//, ""),
      product: "Gecko",
      productSub: "20030107",
      vendorSub: "",
      language: String(payload.language || "en-US"),
      languages: Array.isArray(payload.languages) ? payload.languages : ["en-US", "en"],
      hardwareConcurrency: Number(payload.hardware_concurrency || 12),
      deviceMemory: Number(payload.device_memory || 8),
      maxTouchPoints: 0,
      cookieEnabled: true,
      onLine: true,
      pdfViewerEnabled: true,
      platform: "Win32",
      vendor: "Google Inc.",
      webdriver: false,
      javaEnabled: nativeFunction("javaEnabled"),
      sendBeacon: nativeFunction("sendBeacon"),
      vibrate: nativeFunction("vibrate"),
      getGamepads: nativeFunction("getGamepads"),
      clearAppBadge: nativeFunction("clearAppBadge"),
      setAppBadge: nativeFunction("setAppBadge"),
      requestMediaKeySystemAccess: nativeFunction("requestMediaKeySystemAccess"),
      // Chrome 90+ Client Hints API. sdk.js modern có thể probe userAgentData
      // (low-entropy luôn có sẵn, high-entropy qua getHighEntropyValues).
      userAgentData: uaData,
    });
  })());
  globalThis.location = {
    href: "https://auth.openai.com/",
    origin: "https://auth.openai.com",
    pathname: "/",
    search: "",
  };
  globalThis.screen = screen;
  globalThis.performance = performance;
  globalThis.localStorage = createStorage();
  globalThis.sessionStorage = createStorage();
  globalThis.__sentinel_init_pending = [];
  globalThis.__sentinel_token_pending = [];

  // sdk.js đo thời gian thật: VM turnstile (`On`) đặt guard 500ms trả về counter
  // `kn`, VM session-observer (`jt`) đặt guard 60s. Stub setTimeout ĐỒNG BỘ (gọi
  // cb ngay) làm guard 500ms thắng race tức thì ⇒ `t` luôn ra `"0"` thay vì proof
  // thật. Phải delegate sang timer thật của Node.
  //
  // Đổi lại, guard 60s còn treo sẽ giữ event loop của `_WRAPPER_JS` sống thêm
  // 60s sau khi đã ghi kết quả (wrapper không gọi `process.exit`), nên timer DÀI
  // được `unref()`. Timer ngắn giữ nguyên: vòng poll `__vm_done` của wrapper
  // chạy ở 1ms và chính nó là thứ giữ event loop sống — unref tất cả sẽ làm Node
  // thoát sớm với stdout rỗng.
  const nativeSetTimeout = globalThis.setTimeout;
  const UNREF_DELAY_MS = 1000;
  globalThis.setTimeout = function (cb, ms, ...args) {
    const delay = Number(ms) || 0;
    const id = nativeSetTimeout(cb, delay, ...args);
    if (delay >= UNREF_DELAY_MS && id && typeof id.unref === "function") id.unref();
    return id;
  };
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  globalThis.requestIdleCallback = (cb) => {
    if (typeof cb === "function") cb({ didTimeout: false, timeRemaining: () => 50 });
    return 1;
  };
  globalThis.cancelIdleCallback = () => {};
  globalThis.addEventListener = () => {};
  globalThis.removeEventListener = () => {};
  globalThis.dispatchEvent = () => true;
  globalThis.postMessage = () => {};

  globalThis.atob = (input) => String.fromCharCode(...base64ToBytes(input));
  globalThis.btoa = (input) => {
    const str = String(input || "");
    const bytes = [];
    for (let i = 0; i < str.length; i += 1) bytes.push(str.charCodeAt(i) & 255);
    return bytesToBase64(bytes);
  };
  globalThis.TextEncoder = globalThis.TextEncoder || TextEncoderPoly;
  globalThis.TextDecoder = globalThis.TextDecoder || TextDecoderPoly;
  globalThis.URL = globalThis.URL || URLPoly;
  globalThis.URLSearchParams = globalThis.URLSearchParams || URLSearchParamsPoly;
  globalThis.Event =
    globalThis.Event ||
    class Event {
      constructor(type) {
        this.type = type;
      }
    };
  globalThis.CustomEvent =
    globalThis.CustomEvent ||
    class CustomEvent extends globalThis.Event {
      constructor(type, init) {
        super(type);
        this.detail = init && Object.prototype.hasOwnProperty.call(init, "detail") ? init.detail : null;
      }
    };
  globalThis.MessageChannel =
    globalThis.MessageChannel ||
    class MessageChannel {
      constructor() {
        this.port1 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
        this.port2 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
      }
    };
  globalThis.matchMedia =
    globalThis.matchMedia ||
    ((query) => ({
      media: String(query || ""),
      matches: false,
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent() {
        return false;
      },
    }));
  globalThis.getComputedStyle =
    globalThis.getComputedStyle ||
    (() => ({
      getPropertyValue() {
        return "";
      },
    }));
  globalThis.history = globalThis.history || { length: 1, state: null, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {} };
  globalThis.chrome = globalThis.chrome || { runtime: {}, app: {} };
  globalThis.CSS = globalThis.CSS || { supports() { return true; } };
  globalThis.indexedDB =
    globalThis.indexedDB ||
    {
      open() {
        return { onerror: null, onsuccess: null, onupgradeneeded: null, result: {}, error: null };
      },
      deleteDatabase() {
        return {};
      },
    };
  globalThis.fetch = async () => {
    throw new Error("fetch should not be called");
  };

  const randomFill = (arr) => {
    for (let i = 0; i < arr.length; i += 1) {
      arr[i] = Math.floor(Math.random() * 256);
    }
    return arr;
  };
  // `crypto` cũng là accessor chỉ-có-getter trên Node 22. Node đã có
  // `webcrypto` thật (CSPRNG + randomUUID) — tốt hơn `Math.random()`, nên chỉ
  // vá khi runtime thiếu hẳn.
  if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== "function") {
    defineGlobal("crypto", {
      randomUUID:
        globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
          ? globalThis.crypto.randomUUID.bind(globalThis.crypto)
          : undefined,
      getRandomValues: randomFill,
    });
  }
}

function loadPatchedSdk(sdkSource) {
  let sdk = String(sdkSource || "");
  // Replacer dạng HÀM, không phải chuỗi: `EXPOSE_REPLACEMENT` chứa `$` (tên hàm
  // getter WeakMap trong bundle minify) và `String.replace` sẽ diễn giải các
  // chuỗi `$…` trong replacement string.
  sdk = sdk.replace(SDK_GLOBAL_PATCH, () => SDK_GLOBAL_REPLACEMENT);
  sdk = sdk.replace(INSTANCE_PATCH, () => INSTANCE_REPLACEMENT);
  sdk = sdk.replace(EXPOSE_PATCH, () => EXPOSE_REPLACEMENT);
  eval(sdk);
}

/**
 * VM của sdk.js báo lỗi bằng cách RESOLVE (không reject) với `btoa(<pc> + ": " +
 * err)`, nên một so/t "hợp lệ" về mặt kiểu vẫn có thể là thông báo lỗi đã mã
 * hoá. Giải base64 và trả về chuỗi lỗi nếu nhận ra pattern đó, ngược lại null.
 */
function decodeVmError(value) {
  if (typeof value !== "string" || !value) return null;
  let decoded;
  try {
    decoded = globalThis.atob(value);
  } catch (err) {
    return null;
  }
  return /^\d+:\s.*Error/.test(decoded) ? decoded : null;
}

async function run(payload, sdkSource) {
  installRuntime(payload);
  loadPatchedSdk(sdkSource);

  if (payload.action === "requirements") {
    const requestP = await globalThis.__debugP.getRequirementsToken();
    return { request_p: requestP };
  }

  if (payload.action === "solve") {
    const sdk = globalThis.SentinelSDK;
    const challenge = payload.challenge || {};
    const requestP = String(payload.request_p || "").trim();
    if (!requestP) throw new Error("missing request_p");
    const finalP = await globalThis.__debugP.getEnforcementToken(challenge);
    // bindProof PHẢI chạy trước cả __debug_n và VM session-observer: cả 2 đọc
    // proof đã bind qua WeakMap `D` (`$(challenge)` chính là khoá XOR của VM).
    // Đảo thứ tự → VM thấy proof undefined.
    sdk.__debug_bindProof(challenge, requestP);

    const dx = challenge && challenge.turnstile ? challenge.turnstile.dx : null;
    const tValue = dx ? await sdk.__debug_n(challenge, dx) : null;
    // Tripwire: guard 500ms của `On()` resolve bằng counter `kn` (chuỗi toàn
    // chữ số). Thấy giá trị như vậy nghĩa là timer trong runtime lại bị stub
    // đồng bộ — proof rác, cần biết ngay thay vì âm thầm gửi lên server.
    const tSuspect = typeof tValue === "string" && /^\d+$/.test(tValue);

    // ── so-proof (session-observer VM), 2 pass ──────────────────────────
    // sdk.js thật: `se()` → `Et(challenge)` → `jt(collector_dx, $(challenge))`
    // nạp bảng opcode + KHOÁ XOR vào register nội bộ; sau đó `token()` →
    // `Nt(snapshot_dx)` → `jt(snapshot_dx)` KHÔNG khoá, tái dùng state pass 1.
    // Bản trước gọi `Nt(collector_dx)` một pass: register khoá còn rỗng nên
    // `Rt(atob(dx), "undefined")` ra rác, `JSON.parse` ném SyntaxError và VM
    // resolve `btoa("0: SyntaxError…")` — so-token luôn sai mà không ai biết.
    let soValue = null;
    let soError = null;
    const soSpec = challenge && challenge.so ? challenge.so : null;
    const soReady =
      soSpec &&
      soSpec.collector_dx &&
      soSpec.snapshot_dx &&
      typeof sdk.__debug_jt === "function" &&
      typeof sdk.__debug_Nt === "function" &&
      typeof sdk.__debug_soKey === "function";
    if (soReady) {
      try {
        const soKey = sdk.__debug_soKey(challenge) || "";
        if (!soKey) throw new Error("so key rỗng — __debug_bindProof không khớp challenge");
        // Pass 1 (collector). Chỉ cần nó nạp xong opcode + khoá — việc này chạy
        // đồng bộ ở đầu `jt`. Bản thân collector có thể không bao giờ resolve
        // (cài hook rồi chờ event) nên phải chặn trên bằng timeout riêng, nếu
        // không sẽ dính guard 60s của SDK.
        await Promise.race([
          sdk.__debug_jt(soSpec.collector_dx, soKey).catch(() => null),
          new Promise((resolve) =>
            globalThis.setTimeout(() => resolve(null), SO_COLLECTOR_TIMEOUT_MS),
          ),
        ]);
        // Pass 2 (snapshot) — không truyền khoá, tái dùng state pass 1.
        soValue = await sdk.__debug_Nt(soSpec.snapshot_dx);
        const vmError = decodeVmError(soValue);
        if (vmError) {
          soError = vmError;
          soValue = null;
        }
      } catch (err) {
        soError = String((err && err.message) || err);
        soValue = null;
      }
    } else if (soSpec && soSpec.required) {
      soError = "challenge.so thiếu collector_dx/snapshot_dx hoặc hook __debug_jt/__debug_soKey";
    }

    return {
      final_p: finalP,
      t: tValue,
      so: soValue,
      so_error: soError,
      t_suspect: tSuspect,
    };
  }

  throw new Error(`unsupported action: ${payload.action}`);
}

(async () => {
  try {
    const payload = JSON.parse(String(globalThis.__payload_json || "{}"));
    const sdkSource = String(globalThis.__sdk_source || "");
    const result = await run(payload, sdkSource);
    globalThis.__vm_output_json = JSON.stringify(result);
  } catch (error) {
    const detail = {
      name: error && error.name ? String(error.name) : "Error",
      message: error && error.message ? String(error.message) : String(error),
      stack: error && error.stack ? String(error.stack) : String(error),
    };
    const message = `${detail.name}: ${detail.message}\n${detail.stack}`;
    globalThis.__vm_error = message;
  } finally {
    globalThis.__vm_done = true;
  }
})();
