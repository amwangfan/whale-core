/* MiniDOM: just enough DOM for real Preact (vendored) to render the demo inside the JS sandbox, which
   has no browser. Elements support children, attributes, style, classList, dataset, listeners;
   <canvas> gets node-canvas' getContext. Used by tests/demo-dom.js (built by tests/demo.py). */
(function () {
  globalThis.__DEMO_RUN = String(Math.random()); // the sandbox realm (and old timers) persist between runs
  let canvasLib = null;
  try { canvasLib = require('canvas'); } catch { /* no canvas */ }
  class Node {
    constructor(type, name) { this.nodeType = type; this.nodeName = name; this.childNodes = []; this.parentNode = null; this.ownerDocument = globalThis.document; this._l = {}; }
    get firstChild() { return this.childNodes[0] || null; }
    get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
    get nextSibling() { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) + 1] || null; }
    get previousSibling() { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) - 1] || null; }
    appendChild(c) { return this.insertBefore(c, null); }
    insertBefore(c, ref) {
      if (c.nodeType === 11) { [...c.childNodes].forEach((x) => this.insertBefore(x, ref)); return c; }
      if (c.parentNode) c.parentNode.removeChild(c);
      const i = ref ? this.childNodes.indexOf(ref) : -1;
      if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
      c.parentNode = this;
      return c;
    }
    removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; }
    replaceChild(n, o) { this.insertBefore(n, o); this.removeChild(o); return o; }
    remove() { if (this.parentNode) this.parentNode.removeChild(this); }
    contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
    addEventListener(t, f) { (this._l[t] = this._l[t] || new Set()).add(f); }
    removeEventListener(t, f) { this._l[t]?.delete(f); }
    dispatchEvent(ev) { ev.target = ev.target || this; ev.currentTarget = this; this._l[ev.type]?.forEach((f) => f.call(this, ev)); return true; }
    get textContent() { return this.nodeType === 3 ? this.data : this.childNodes.map((c) => c.textContent).join(this.nodeType === 1 && /^(P|LI|H\d|DIV|TR|SECTION|HEADER|BUTTON)$/.test(this.nodeName) ? ' ' : ''); }
    set textContent(v) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; if (v !== '' && v != null) this.appendChild(globalThis.document.createTextNode(String(v))); }
  }
  class Text extends Node {
    constructor(d) { super(3, '#text'); this.data = String(d); }
    get nodeValue() { return this.data; } set nodeValue(v) { this.data = String(v); }
  }
  class Element extends Node {
    constructor(name, ns) {
      super(1, name.toUpperCase()); this.localName = name.toLowerCase(); this.namespaceURI = ns || 'http://www.w3.org/1999/xhtml'; this.attributes = {};
      const style = {}; style.setProperty = (k, v) => { style[k] = v; }; style.removeProperty = (k) => { delete style[k]; }; this.style = style;
      this.dataset = {};
      const self = this;
      this.classList = { add: (...c) => self._cls((s) => c.forEach((x) => s.add(x))), remove: (...c) => self._cls((s) => c.forEach((x) => s.delete(x))),
        toggle: (c, on) => { let r; self._cls((s) => { r = on ?? !s.has(c); if (r) s.add(c); else s.delete(c); }); return r; }, contains: (c) => (self.attributes.class || '').split(/\s+/).includes(c) };
      if (this.localName === 'canvas' && canvasLib) { this._c = canvasLib.createCanvas(800, 500); this.width = 800; this.height = 500; }
    }
    _cls(fn) { const s = new Set((this.attributes.class || '').split(/\s+/).filter(Boolean)); fn(s); this.attributes.class = [...s].join(' '); }
    get tagName() { return this.nodeName; }
    get id() { return this.attributes.id || ''; }
    get className() { return this.attributes.class || ''; } set className(v) { this.attributes.class = v; }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
    removeAttribute(k) { delete this.attributes[k]; }
    hasAttribute(k) { return k in this.attributes; }
    getContext(k) { if (!this._c) return null; this._c.width = this.width; this._c.height = this.height; return this._c.getContext(k); }
    getBoundingClientRect() { return { left: 0, top: 0, right: 800, bottom: 500, width: 800, height: 500, x: 0, y: 0 }; }
    focus() { globalThis.document.activeElement = this; } blur() {} click() { this.dispatchEvent({ type: 'click', button: 0, preventDefault() {}, stopPropagation() {} }); }
    scrollIntoView() {} setPointerCapture() {} releasePointerCapture() {}
    querySelector() { return null; } querySelectorAll() { return []; }
    get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  }
  const doc = new Node(9, '#document');
  // Preact lower-cases an event name only when `on<name>` exists on the element (as in real DOM)
  ['click', 'input', 'change', 'keydown', 'keyup', 'submit', 'focus', 'blur', 'pointerdown', 'pointermove', 'pointerup', 'pointerleave', 'pointercancel', 'wheel',
    'dragover', 'dragleave', 'drop', 'mouseenter', 'mouseleave', 'scroll', 'load', 'error'].forEach((t) => { Element.prototype['on' + t] = null; });
  globalThis.document = doc;
  doc.ownerDocument = null;
  doc.createElement = (n) => new Element(n);
  doc.createElementNS = (ns, n) => new Element(n, ns);
  doc.createTextNode = (d) => new Text(d);
  doc.createComment = () => new Text('');
  doc.createDocumentFragment = () => { const f = new Node(11, '#fragment'); return f; };
  doc.documentElement = new Element('html');
  doc.body = new Element('body');
  doc.documentElement.appendChild(doc.body);
  doc.appendChild(doc.documentElement);
  doc.hidden = false; doc.visibilityState = 'visible'; doc.activeElement = doc.body; doc.title = '';
  doc.getElementById = (id) => { const walk = (n) => { if (n.nodeType === 1 && n.attributes.id === id) return n; for (const c of n.childNodes) { const r = walk(c); if (r) return r; } return null; }; return walk(doc); };
  const app = new Element('div'); app.setAttribute('id', 'app'); app.setAttribute('class', 'boot'); doc.body.appendChild(app);
  globalThis.window = globalThis;
  globalThis.Node = Node; globalThis.Element = Element; globalThis.Text = Text;
  globalThis.HTMLElement = Element; globalThis.SVGElement = Element;
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '', fontFamily: 'sans-serif' });
  globalThis.requestAnimationFrame = (f) => setTimeout(() => f(Date.now()), 16);
  globalThis.cancelAnimationFrame = (t) => clearTimeout(t);
  globalThis.devicePixelRatio = 1;
  globalThis.scrollTo = () => {};
  const winL = {};
  globalThis.addEventListener = (t, f) => { (winL[t] = winL[t] || new Set()).add(f); };
  globalThis.removeEventListener = (t, f) => winL[t]?.delete(f);
  const ls = new Map();
  globalThis.localStorage = { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)), removeItem: (k) => ls.delete(k) };
  globalThis.__MINIDOM_PAGE__ = globalThis.__MINIDOM_PAGE__ || 'bridge';
  globalThis.location = { protocol: 'file:', host: '', href: `file:///demo.html?page=${globalThis.__MINIDOM_PAGE__}`, pathname: '/demo.html', search: `?page=${globalThis.__MINIDOM_PAGE__}`, hash: '' };
  globalThis.history = { replaceState(_, __, url) { const i = url.indexOf('#'); globalThis.location.hash = i >= 0 ? url.slice(i) : ''; }, pushState(a, b, url) { this.replaceState(a, b, url); } };
  globalThis.matchMedia = (q) => ({ matches: /min-width/.test(q) && !/min-width: 1[4-9]\d\d/.test(q), addEventListener() {}, removeEventListener() {} });
  globalThis.navigator = globalThis.navigator || {};
  if (typeof globalThis.AbortController === 'undefined') {
    globalThis.AbortController = class {
      constructor() { const l = new Set(); this.signal = { aborted: false, addEventListener: (t, f) => l.add(f), removeEventListener: (t, f) => l.delete(f) }; this._l = l; }
      abort() { if (this.signal.aborted) return; this.signal.aborted = true; this._l.forEach((f) => f()); }
    };
  }
  // surface async render errors (Preact re-renders from a Promise microtask) with their stacks
  const then = Promise.prototype.then;
  const wrap = (f) => (typeof f !== 'function' ? f : function (v) {
    try { return f.call(this, v); } catch (e) {
      if (!e || !e.__seen) { try { Object.defineProperty(e, '__seen', { value: true }); } catch { /* frozen */ } console.log('ASYNC ERROR: ' + (e && e.stack ? e.stack.split('\n').slice(0, 8).join(' | ') : e)); }
      throw e;
    }
  });
  // eslint-disable-next-line no-extend-native
  try { Object.defineProperty(Promise.prototype, 'then', { value: function (a, b) { return then.call(this, wrap(a), wrap(b)); }, configurable: true, writable: true }); } catch { /* frozen realm */ }
})();
