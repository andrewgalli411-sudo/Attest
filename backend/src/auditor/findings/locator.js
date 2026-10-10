() => {
  // Installs window.__loc: build a durable fingerprint for an element, and collect candidates.
  const VOLATILE_ID = /^(:r[0-9a-z]+:|react-aria|radix-|headlessui-|mui-|ember\d|ng-|__)|\d{5,}/i;
  const TEST_ATTRS = ['data-testid', 'data-test', 'data-cy', 'data-qa'];
  const LANDMARKS = {main: 1, nav: 1, header: 1, footer: 1, aside: 1, form: 1, section: 1, dialog: 1};

  const cssEsc = s => CSS.escape(s);
  const parentOf = el => el.parentElement || (el.getRootNode() instanceof ShadowRoot ? el.getRootNode().host : null);

  const deepAll = (root = document, out = []) => {
    for (const el of root.querySelectorAll('*')) {
      out.push(el);
      if (el.shadowRoot) deepAll(el.shadowRoot, out);
    }
    return out;
  };

  const positional = el => {
    const parts = [];
    while (el && el.nodeType === 1) {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      const root = el.getRootNode();
      if (!el.parentElement && root instanceof ShadowRoot) { parts.unshift('>>>'); el = root.host; }
      else el = el.parentElement;
    }
    return parts.join(' > ').replace(/ > >>> > /g, ' >>> ');
  };

  const stableCss = el => {
    const tag = el.tagName.toLowerCase();
    for (const a of TEST_ATTRS) {
      const v = el.getAttribute(a);
      if (v) return tag + '[' + a + '="' + v.replace(/"/g, '\\"') + '"]';
    }
    if (el.id && !VOLATILE_ID.test(el.id)) return '#' + cssEsc(el.id);
    const name = el.getAttribute('name');
    if (name && /^(input|select|textarea|button)$/.test(tag)) {
      const t = el.getAttribute('type');
      return tag + '[name="' + name.replace(/"/g, '\\"') + '"]' + (t ? '[type="' + t + '"]' : '');
    }
    return null;
  };

  const landmarkPath = el => {
    const out = [];
    for (let p = parentOf(el); p; p = parentOf(p)) {
      const t = p.tagName.toLowerCase(), r = p.getAttribute && p.getAttribute('role');
      if (r || LANDMARKS[t]) out.unshift(r || t);
    }
    return out;
  };

  const ancestry = el => {
    const out = [];
    let p = parentOf(el);
    while (p && out.length < 4 && p.tagName !== 'BODY' && p.tagName !== 'HTML') {
      const c = (p.getAttribute('class') || '').trim().split(/\s+/)[0] || '';
      out.push(p.tagName.toLowerCase() + (c && !VOLATILE_ID.test(c) ? '.' + c : ''));
      p = parentOf(p);
    }
    return out;
  };

  const normHtml = (el, ignore) => {
    const c = el.cloneNode(false);
    for (const a of [...c.attributes]) {
      if (a.name === 'style' || (a.name === 'id' && VOLATILE_ID.test(a.value)) || ignore.includes(a.name)) c.removeAttribute(a.name);
    }
    const attrs = [...c.attributes].map(a => a.name + '=' + a.value).sort().join(' ');
    return c.tagName.toLowerCase() + ' ' + attrs;
  };

  const attrsOf = (el, ignore) => {
    const out = {};
    for (const a of el.attributes) {
      if (a.name === 'style' || ignore.includes(a.name) || (a.name === 'id' && VOLATILE_ID.test(a.value))) continue;
      out[a.name] = a.value;
    }
    return out;
  };

  const visibleText = el => ((el.innerText || el.value || el.getAttribute('placeholder') || '') + '').replace(/\s+/g, ' ').trim().slice(0, 120);

  const build = (el, ignore = []) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      stable_css: stableCss(el),
      css_path: positional(el),
      visible_text: visibleText(el),
      landmark_path: landmarkPath(el),
      ancestry: ancestry(el),
      html_norm: normHtml(el, ignore),
      attrs: attrsOf(el, ignore),
      in_shadow: el.getRootNode() instanceof ShadowRoot,
      bbox: {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height, viewport: [innerWidth, innerHeight]},
    };
  };

  const find = (strategy, loc) => {
    const all = deepAll().filter(e => e.tagName.toLowerCase() === loc.tag);
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    switch (strategy) {
      case 'stable_css': {
        if (!loc.stable_css) return [];
        return all.filter(e => e.matches(loc.stable_css));
      }
      case 'text_landmark':
        return loc.visible_text ? all.filter(e => visibleText(e) === loc.visible_text && same(landmarkPath(e), loc.landmark_path)) : [];
      case 'ancestry':
        return all.filter(e => same(ancestry(e), loc.ancestry) && same(landmarkPath(e), loc.landmark_path));
      case 'positional': {
        return all.filter(e => positional(e) === loc.css_path);
      }
      case 'fuzzy':
        return all;
    }
    return [];
  };

  const collect = (strategy, loc, ignore) => {
    const els = find(strategy, loc).slice(0, 400);
    window.__locEls = els;
    return els.map(e => build(e, ignore));
  };

  const resolve = path => {
    let root = document, el = null;
    for (const seg of path.split(' >>> ')) {
      el = root.querySelector(seg);
      if (!el) return null;
      root = el.shadowRoot || root;
    }
    return el;
  };

  window.__loc = {build, find, collect, deepAll, resolve};
  window.__locEls = [];
  return true;
}
