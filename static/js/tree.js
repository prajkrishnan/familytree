/* Interactive family tree: union-aware layout, pan/zoom, culling, drawer. */
(function () {
  'use strict';

  const app = document.getElementById('tree-app');
  if (!app) return;

  const canvas = document.getElementById('tree-canvas');
  const stage = document.getElementById('tree-stage');
  const svg = document.getElementById('tree-links');
  const cardsLayer = document.getElementById('tree-cards');
  const drawer = document.getElementById('drawer');
  const backdrop = document.getElementById('drawer-backdrop');
  const empty = document.getElementById('tree-empty');
  const statusEl = document.getElementById('tree-status');
  const toastEl = document.getElementById('tree-toast');
  const searchInput = document.getElementById('tree-search');
  const searchResults = document.getElementById('search-results');

  const CARD_W = 172;
  const CARD_H = 96;
  const SPOUSE_GAP = 34;
  const H_GAP = 26;
  const GROUP_GAP = 54;
  const V_GAP = 78;
  const PAD = 60;
  const DEFAULT_DEPTH = 2;
  const MAX_CARDS = 2400;

  const state = {
    data: null,
    persons: new Map(),
    unions: new Map(),
    childByUnion: new Map(),
    unionsByPerson: new Map(),
    focus: null,
    expanded: new Set(),
    collapsed: new Set(),
    cards: [],
    connectors: [],
    spouseLinks: [],
    contentW: 0,
    contentH: 0,
    tx: 0,
    ty: 0,
    scale: 1,
    rootBlock: null,
    openId: null,
    cardCounter: 0,
    fittedOnce: false,
  };

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function initials(p) {
    const g = (p.given || p.nickname || '?').trim();
    const s = (p.surname || '').trim();
    return ((g[0] || '?') + (s[0] || '')).toUpperCase();
  }

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { toastEl.hidden = true; }, 3500);
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function setData(data) {
    state.data = data;
    state.focus = data.focus;
    state.persons = new Map();
    state.unions = new Map();
    state.childByUnion = new Map();
    state.unionsByPerson = new Map();
    data.persons.forEach(function (p) { state.persons.set(p.id, p); });
    data.unions.forEach(function (u) {
      state.unions.set(u.id, u);
      [u.partner1_id, u.partner2_id].forEach(function (pid) {
        if (!pid) return;
        if (!state.unionsByPerson.has(pid)) state.unionsByPerson.set(pid, []);
        state.unionsByPerson.get(pid).push(u);
      });
    });
    data.children.forEach(function (c) {
      if (!state.childByUnion.has(c.u)) state.childByUnion.set(c.u, []);
      state.childByUnion.get(c.u).push(c.c);
    });
  }

  function childrenOf(unionId) {
    return state.childByUnion.get(unionId) || [];
  }

  function kidsOfPerson(pid) {
    const out = new Set();
    (state.unionsByPerson.get(pid) || []).forEach(function (u) {
      childrenOf(u.id).forEach(function (c) { if (c !== pid) out.add(c); });
    });
    return out;
  }

  /* ------------------------------------------------------------------ */
  /* Layout                                                              */
  /* ------------------------------------------------------------------ */

  function finishBlock(block) {
    const barWidth = block.barItems.length * CARD_W + (block.barItems.length - 1) * SPOUSE_GAP;
    let groupsWidth = 0;
    let groupHeight = 0;
    block.groups.forEach(function (g) {
      g.width = g.blocks.reduce(function (sum, b) { return sum + b.width; }, 0)
        + H_GAP * Math.max(0, g.blocks.length - 1);
      g.height = g.blocks.reduce(function (h, b) { return Math.max(h, b.height); }, 0);
      groupsWidth += g.width;
      groupHeight = Math.max(groupHeight, g.height);
    });
    if (block.groups.length > 1) groupsWidth += GROUP_GAP * (block.groups.length - 1);
    block.width = Math.max(barWidth, groupsWidth);
    block.height = CARD_H + (block.groups.length ? V_GAP + groupHeight : 0);
    return block;
  }

  function buildRootUnion(unionId) {
    const union = state.unions.get(unionId);
    if (!union) return null;
    const barItems = [];
    [union.partner1_id, union.partner2_id].forEach(function (pid) {
      if (pid && state.persons.has(pid)) barItems.push({ id: pid, unionId: union.id });
    });
    if (!barItems.length) return null;
    state.cardCounter += barItems.length;
    const barIds = new Set(barItems.map(function (b) { return b.id; }));
    const path = new Set(barIds);
    const kids = childrenOf(union.id).filter(function (k) {
      return !barIds.has(k) && state.persons.has(k);
    });
    const groups = [];
    if (kids.length) {
      const blocks = kids.map(function (kid) {
        return buildPerson(kid, 1, path);
      }).filter(Boolean);
      if (blocks.length) {
        groups.push({ unionId: union.id, sourceA: 0, sourceB: barItems.length > 1 ? 1 : null, blocks: blocks });
      }
    }
    const block = {
      role: 'root',
      barItems: barItems,
      personIndex: 0,
      personId: barItems[0].id,
      groups: groups,
      childTotal: kids.length,
      hasChildren: kids.length > 0,
      expanded: groups.length > 0,
      spouseLinks: barItems.length > 1 ? [[0, 1]] : [],
    };
    return finishBlock(block);
  }

  function buildPerson(pid, depth, path) {
    if (state.cardCounter > MAX_CARDS) return null;
    const person = state.persons.get(pid);
    if (!person || path.has(pid)) return null;
    const unions = state.unionsByPerson.get(pid) || [];
    const nextPath = new Set(path);
    nextPath.add(pid);

    const spouseItems = [];
    const seenSpouses = new Set();
    unions.forEach(function (u) {
      const other = u.partner1_id === pid ? u.partner2_id : u.partner1_id;
      if (!other || other === pid || seenSpouses.has(other)) return;
      if (!state.persons.has(other) || path.has(other)) return;
      seenSpouses.add(other);
      spouseItems.push({ id: other, unionId: u.id });
    });

    const left = [];
    const right = [];
    spouseItems.forEach(function (s, i) { (i % 2 === 0 ? left : right).push(s); });
    const leftOrdered = left.slice().reverse();
    const barItems = leftOrdered.map(function (s) { return { id: s.id, unionId: s.unionId }; });
    const personIndex = barItems.length;
    barItems.push({ id: pid, unionId: null });
    right.forEach(function (s) { barItems.push({ id: s.id, unionId: s.unionId }); });
    barItems.forEach(function (item) { nextPath.add(item.id); });

    state.cardCounter += barItems.length;

    const childIds = new Set();
    unions.forEach(function (u) {
      childrenOf(u.id).forEach(function (kid) {
        if (kid !== pid && !nextPath.has(kid) && state.persons.has(kid)) childIds.add(kid);
      });
    });

    const collapsed = state.collapsed.has(pid);
    const wantExpand = !collapsed && (depth < DEFAULT_DEPTH || state.expanded.has(pid));
    const groups = [];
    if (childIds.size && wantExpand) {
      unions.forEach(function (u) {
        const other = u.partner1_id === pid ? u.partner2_id : u.partner1_id;
        const kids = childrenOf(u.id).filter(function (kid) {
          return kid !== pid && !nextPath.has(kid) && state.persons.has(kid);
        });
        if (!kids.length) return;
        const blocks = kids.map(function (kid) {
          return buildPerson(kid, depth + 1, nextPath);
        }).filter(Boolean);
        if (!blocks.length) return;
        let spouseIndex = null;
        if (other) {
          spouseIndex = barItems.findIndex(function (item) { return item.id === other; });
          if (spouseIndex < 0) spouseIndex = null;
        }
        groups.push({ unionId: u.id, sourceA: personIndex, sourceB: spouseIndex, blocks: blocks });
      });
    }

    const spouseLinks = [];
    spouseItems.forEach(function (s) {
      const idx = barItems.findIndex(function (item) { return item.id === s.id; });
      if (idx >= 0) spouseLinks.push([personIndex, idx]);
    });

    const block = {
      role: 'person',
      personId: pid,
      barItems: barItems,
      personIndex: personIndex,
      groups: groups,
      childTotal: childIds.size,
      hasChildren: childIds.size > 0,
      expanded: groups.length > 0,
      spouseLinks: spouseLinks,
    };
    return finishBlock(block);
  }

  function placeBlock(block, x, y) {
    block.x = x;
    block.y = y;
    const barWidth = block.barItems.length * CARD_W + (block.barItems.length - 1) * SPOUSE_GAP;
    block.barX = x + (block.width - barWidth) / 2;
    block.cards = [];
    block.barItems.forEach(function (item, index) {
      if (!state.persons.has(item.id)) return;
      const card = {
        id: item.id,
        x: block.barX + index * (CARD_W + SPOUSE_GAP),
        y: y,
        index: index,
        block: block,
      };
      block.cards.push(card);
      state.cards.push(card);
    });
    block.personCard = block.cards.find(function (c) { return c.index === block.personIndex; })
      || block.cards[0];

    if (block.groups.length) {
      let total = 0;
      block.groups.forEach(function (g) { total += g.width; });
      total += GROUP_GAP * (block.groups.length - 1);
      let gx = x + (block.width - total) / 2;
      const childY = y + CARD_H + V_GAP;
      block.groups.forEach(function (g) {
        let bx = gx;
        const targets = [];
        g.blocks.forEach(function (child) {
          placeBlock(child, bx, childY);
          if (child.personCard) {
            targets.push({ x: child.personCard.x + CARD_W / 2, y: childY });
          }
          bx += child.width + H_GAP;
        });
        const a = block.cards[g.sourceA];
        const b = g.sourceB != null ? block.cards[g.sourceB] : null;
        if (a && targets.length) {
          const ax = a.x + CARD_W / 2;
          const bx2 = b ? b.x + CARD_W / 2 : ax;
          state.connectors.push({
            x: (ax + bx2) / 2,
            y: y + CARD_H,
            targets: targets,
          });
        }
        gx += g.width + GROUP_GAP;
      });
    }

    block.spouseLinks.forEach(function (pair) {
      const a = block.cards[pair[0]];
      const b = block.cards[pair[1]];
      if (a && b) state.spouseLinks.push({ a: a, b: b });
    });
  }

  function buildAndPlace() {
    state.cards = [];
    state.connectors = [];
    state.spouseLinks = [];
    state.cardCounter = 0;
    const root = state.data.root;
    let block = null;
    if (root && root.union_id) block = buildRootUnion(root.union_id);
    else if (root && root.person_id) block = buildPerson(root.person_id, 0, new Set());
    if (!block) return null;
    placeBlock(block, PAD, PAD);
    state.rootBlock = block;
    state.contentW = Math.max(block.width + PAD * 2, canvas.clientWidth);
    state.contentH = Math.max(block.height + PAD * 2, canvas.clientHeight);
    stage.style.width = state.contentW + 'px';
    stage.style.height = state.contentH + 'px';
    svg.setAttribute('width', String(state.contentW));
    svg.setAttribute('height', String(state.contentH));
    svg.setAttribute('viewBox', '0 0 ' + state.contentW + ' ' + state.contentH);
    return block;
  }

  /* ------------------------------------------------------------------ */
  /* Rendering                                                           */
  /* ------------------------------------------------------------------ */

  function initialsFromPerson(p) {
    return initials(p);
  }

  function makeCard(person, card) {
    const el = document.createElement('div');
    el.className = 'tree-card g-' + person.gender + (person.living ? ' living' : ' deceased');
    if (person.id === state.focus) el.classList.add('is-focus');
    if (person.self) el.classList.add('is-self');
    if (person.pending) el.classList.add('is-pending');
    el.style.left = card.x + 'px';
    el.style.top = card.y + 'px';
    el.tabIndex = 0;
    el.setAttribute('role', 'button');
    el.setAttribute('aria-label', person.name + ', ' + person.lifespan);
    el.dataset.person = person.id;

    const avatar = document.createElement('div');
    avatar.className = 'tree-avatar';
    if (person.avatar) {
      const img = document.createElement('img');
      img.src = person.avatar;
      img.alt = '';
      img.loading = 'lazy';
      avatar.appendChild(img);
    } else {
      avatar.textContent = initialsFromPerson(person);
    }

    const body = document.createElement('div');
    body.className = 'tree-card-body';
    const name = document.createElement('span');
    name.className = 'tree-name';
    name.textContent = person.name;
    const meta = document.createElement('span');
    meta.className = 'tree-meta';
    meta.textContent = person.lifespan;
    body.appendChild(name);
    body.appendChild(meta);

    const marks = document.createElement('div');
    marks.className = 'tree-marks';
    if (!person.living) {
      const dagger = document.createElement('span');
      dagger.className = 'memorial-mark';
      dagger.textContent = '†';
      dagger.title = 'Deceased';
      marks.appendChild(dagger);
    }
    if (person.pending) {
      const pending = document.createElement('span');
      pending.className = 'pending-mark';
      pending.title = 'Your edit is awaiting approval';
      marks.appendChild(pending);
    }
    if (person.self) {
      const self = document.createElement('span');
      self.className = 'self-mark';
      self.textContent = 'You';
      marks.appendChild(self);
    }

    el.appendChild(avatar);
    el.appendChild(body);
    el.appendChild(marks);

    if (card.block.role === 'person' && card.index === card.block.personIndex && card.block.childTotal > 0) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'tree-expand';
      toggle.dataset.expandFor = person.id;
      toggle.setAttribute('aria-expanded', card.block.expanded ? 'true' : 'false');
      toggle.title = card.block.expanded ? 'Collapse branch' : 'Expand branch';
      toggle.textContent = card.block.expanded ? '−' : '+' + card.block.childTotal;
      el.appendChild(toggle);
    }
    card.el = el;
    return el;
  }

  function renderCards() {
    const fragment = document.createDocumentFragment();
    const seen = new Set();
    state.cards.forEach(function (card) {
      const person = state.persons.get(card.id);
      if (!person) return;
      if (seen.has(person.id)) {
        card.el = null;
        return;
      }
      seen.add(person.id);
      fragment.appendChild(makeCard(person, card));
    });
    cardsLayer.replaceChildren(fragment);
  }

  function renderEdges() {
    const ns = 'http://www.w3.org/2000/svg';
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const group = document.createElementNS(ns, 'g');
    group.setAttribute('class', 'tree-edges');

    state.connectors.forEach(function (conn) {
      const jy = conn.y + Math.min(34, V_GAP / 2);
      const targets = conn.targets;
      let d = 'M ' + conn.x + ' ' + conn.y + ' V ' + jy;
      const xs = targets.map(function (t) { return t.x; });
      const min = Math.min.apply(null, xs);
      const max = Math.max.apply(null, xs);
      if (conn.x < min) d += ' M ' + conn.x + ' ' + jy + ' H ' + min;
      if (conn.x > max) d += ' M ' + conn.x + ' ' + jy + ' H ' + max;
      if (max > min) d += ' M ' + min + ' ' + jy + ' H ' + max;
      targets.forEach(function (t) {
        d += ' M ' + t.x + ' ' + jy + ' V ' + t.y;
      });
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('d', d);
      path.setAttribute('class', 'tree-edge');
      group.appendChild(path);
    });

    state.spouseLinks.forEach(function (link) {
      const x1 = link.a.x + CARD_W / 2;
      const x2 = link.b.x + CARD_W / 2;
      const y = link.a.y + CARD_H / 2;
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('d', 'M ' + x1 + ' ' + y + ' H ' + x2);
      path.setAttribute('class', 'tree-union-edge');
      group.appendChild(path);
    });

    svg.appendChild(group);
  }

  function layoutAndRender(options) {
    const opts = options || {};
    const block = buildAndPlace();
    if (!block) return;
    renderCards();
    renderEdges();
    applyTransform();
    if (opts.fit) fitView();
    document.getElementById('tree-empty').hidden = true;
    stage.style.display = '';
    state.fittedOnce = true;
    updateStatus();
  }

  function updateStatus() {
    if (!statusEl || !state.data) return;
    const focus = state.persons.get(state.focus);
    statusEl.textContent = state.data.total + ' people'
      + (focus ? ' · ' + focus.name : '');
    if (state.cardCounter > MAX_CARDS) {
      statusEl.textContent += ' · display capped, collapse branches to explore';
    }
  }

  /* ------------------------------------------------------------------ */
  /* Transform, pan and zoom                                             */
  /* ------------------------------------------------------------------ */

  function applyTransform() {
    stage.style.transform = 'translate(' + state.tx + 'px,' + state.ty + 'px) scale(' + state.scale + ')';
    scheduleCulling();
  }

  function fitView() {
    if (!state.contentW || !state.contentH) return;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    const pad = 36;
    const scale = clamp(Math.min((cw - pad * 2) / state.contentW, (ch - pad * 2) / state.contentH, 1.15), 0.08, 1.4);
    state.scale = scale;
    state.tx = (cw - state.contentW * scale) / 2;
    state.ty = (ch - state.contentH * scale) / 2;
    applyTransform();
  }

  function focusView() {
    const target = state.cards.find(function (c) { return c.id === state.focus && c.el; })
      || state.cards.find(function (c) { return c.id === state.focus; });
    if (!target) { fitView(); return; }
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    const mobile = window.matchMedia('(max-width: 820px)').matches;
    state.scale = mobile ? 0.82 : 1;
    state.tx = cw / 2 - (target.x + CARD_W / 2) * state.scale;
    state.ty = (mobile ? ch * 0.32 : ch * 0.4) - (target.y + CARD_H / 2) * state.scale;
    applyTransform();
  }

  function centerOnPerson(personId, options) {
    const opts = options || {};
    const target = state.cards.find(function (c) { return c.id === personId && c.el; });
    if (!target) return false;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    if (opts.scale) state.scale = clamp(opts.scale, 0.08, 2.6);
    const yRatio = opts.yRatio || 0.5;
    state.tx = cw / 2 - (target.x + CARD_W / 2) * state.scale;
    state.ty = ch * yRatio - (target.y + CARD_H / 2) * state.scale;
    applyTransform();
    return true;
  }

  function zoomAt(cx, cy, factor) {
    const next = clamp(state.scale * factor, 0.08, 2.6);
    if (next === state.scale) return;
    const k = next / state.scale;
    state.tx = cx - (cx - state.tx) * k;
    state.ty = cy - (cy - state.ty) * k;
    state.scale = next;
    applyTransform();
  }

  function zoomCenter(factor) {
    const rect = canvas.getBoundingClientRect();
    zoomAt(rect.width / 2, rect.height / 2, factor);
  }

  let cullFrame = 0;
  function scheduleCulling() {
    if (cullFrame) return;
    cullFrame = requestAnimationFrame(function () {
      cullFrame = 0;
      updateCulling();
    });
  }

  function updateCulling() {
    if (!state.cards.length) return;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    const margin = 260;
    const x0 = (-state.tx - margin) / state.scale;
    const x1 = (cw - state.tx + margin) / state.scale;
    const y0 = (-state.ty - margin) / state.scale;
    const y1 = (ch - state.ty + margin) / state.scale;
    state.cards.forEach(function (card) {
      if (!card.el) return;
      const visible = card.x + CARD_W >= x0 && card.x <= x1 && card.y + CARD_H >= y0 && card.y <= y1;
      card.el.style.display = visible ? '' : 'none';
    });
  }

  const pointers = new Map();
  let panStart = null;
  let pinchStart = null;

  canvas.addEventListener('pointerdown', function (event) {
    if (event.target.closest('.tree-card') || event.target.closest('.tree-zoom')
        || event.target.closest('.tree-empty') || event.target.closest('.tree-legend')) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    canvas.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 1) {
      panStart = { id: event.pointerId, x: event.clientX, y: event.clientY, tx: state.tx, ty: state.ty };
      canvas.classList.add('grabbing');
    } else if (pointers.size === 2) {
      panStart = null;
      const pts = Array.from(pointers.values());
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
      const rect = canvas.getBoundingClientRect();
      pinchStart = {
        dist: dist,
        midX: (pts[0].x + pts[1].x) / 2 - rect.left,
        midY: (pts[0].y + pts[1].y) / 2 - rect.top,
        scale: state.scale,
        tx: state.tx,
        ty: state.ty,
      };
    }
  });

  canvas.addEventListener('pointermove', function (event) {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2 && pinchStart) {
      const pts = Array.from(pointers.values());
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
      if (pinchStart.dist > 0) {
        const rect = canvas.getBoundingClientRect();
        const midX = (pts[0].x + pts[1].x) / 2 - rect.left;
        const midY = (pts[0].y + pts[1].y) / 2 - rect.top;
        const next = clamp(pinchStart.scale * (dist / pinchStart.dist), 0.08, 2.6);
        const k = next / pinchStart.scale;
        state.scale = next;
        state.tx = midX - (pinchStart.midX - pinchStart.tx) * k;
        state.ty = midY - (pinchStart.midY - pinchStart.ty) * k;
        applyTransform();
      }
      return;
    }
    if (panStart && event.pointerId === panStart.id) {
      state.tx = panStart.tx + (event.clientX - panStart.x);
      state.ty = panStart.ty + (event.clientY - panStart.y);
      applyTransform();
    }
  });

  function endPointer(event) {
    pointers.delete(event.pointerId);
    if (panStart && panStart.id === event.pointerId) panStart = null;
    if (pointers.size < 2) pinchStart = null;
    if (pointers.size === 0) canvas.classList.remove('grabbing');
  }
  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', endPointer);

  canvas.addEventListener('wheel', function (event) {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-event.deltaY * 0.0016);
    zoomAt(event.clientX - rect.left, event.clientY - rect.top, factor);
  }, { passive: false });

  document.getElementById('zoom-in').addEventListener('click', function () { zoomCenter(1.25); });
  document.getElementById('zoom-out').addEventListener('click', function () { zoomCenter(0.8); });
  document.getElementById('zoom-fit').addEventListener('click', fitView);

  /* ------------------------------------------------------------------ */
  /* Cards interaction                                                   */
  /* ------------------------------------------------------------------ */

  cardsLayer.addEventListener('click', function (event) {
    const expand = event.target.closest('[data-expand-for]');
    if (expand) {
      event.stopPropagation();
      toggleBranch(expand.dataset.expandFor);
      return;
    }
    const card = event.target.closest('.tree-card');
    if (card) openPerson(card.dataset.person);
  });

  cardsLayer.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (event.target.closest('[data-expand-for]')) return;
    const card = event.target.closest('.tree-card');
    if (!card) return;
    event.preventDefault();
    openPerson(card.dataset.person);
  });

  function toggleBranch(personId) {
    const block = findBlockForPerson(personId);
    if (!block) return;
    if (block.expanded) {
      state.collapsed.add(personId);
      state.expanded.delete(personId);
    } else {
      state.expanded.add(personId);
      state.collapsed.delete(personId);
    }
    layoutAndRender({ fit: false });
  }

  function findBlockForPerson(personId) {
    for (const card of state.cards) {
      if (card.block && card.block.role === 'person'
          && card.block.personId === personId && card.index === card.block.personIndex) {
        return card.block;
      }
    }
    return null;
  }

  /* ------------------------------------------------------------------ */
  /* Drawer                                                              */
  /* ------------------------------------------------------------------ */

  async function openPerson(personId) {
    if (!personId) return;
    try {
      const res = await fetch('/api/persons/' + encodeURIComponent(personId) + '/card',
        { headers: { 'Accept': 'text/html' } });
      if (!res.ok) throw new Error('not found');
      drawer.innerHTML = await res.text();
      drawer.classList.add('open');
      drawer.setAttribute('aria-hidden', 'false');
      drawer.scrollTop = 0;
      state.openId = personId;
      const isMobile = window.matchMedia('(max-width: 820px)').matches;
      backdrop.hidden = !isMobile;
      drawer.setAttribute('aria-modal', isMobile ? 'true' : 'false');
      const closeButton = drawer.querySelector('[data-drawer-close]');
      if (closeButton) closeButton.focus({ preventScroll: true });
    } catch (err) {
      toast('Could not load that person.');
    }
  }

  function closeDrawer() {
    const lastOpen = state.openId;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    drawer.innerHTML = '';
    backdrop.hidden = true;
    state.openId = null;
    if (lastOpen) {
      const card = document.querySelector('.tree-card[data-person="' + lastOpen + '"]');
      if (card) card.focus({ preventScroll: true });
    }
  }

  drawer.addEventListener('click', function (event) {
    if (event.target.closest('[data-drawer-close]')) {
      closeDrawer();
      return;
    }
    const link = event.target.closest('a[data-focus-link]');
    if (link) {
      event.preventDefault();
      const url = new URL(link.href, window.location.origin);
      const focus = url.searchParams.get('focus');
      const open = url.searchParams.get('open') || focus;
      if (!focus) return;
      closeDrawer();
      loadTree(focus, { fit: true }).then(function () {
        if (open) {
          centerOnPerson(open, { scale: 0.95, yRatio: 0.42 });
          openPerson(open);
        }
      });
    }
  });
  backdrop.addEventListener('click', closeDrawer);

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      if (!drawer.classList.contains('open') && searchResults.hidden) closeDrawer();
      if (drawer.classList.contains('open')) closeDrawer();
      hideSearchResults();
    }
  });

  /* ------------------------------------------------------------------ */
  /* Search                                                              */
  /* ------------------------------------------------------------------ */

  let searchTimer = 0;
  searchInput.addEventListener('input', function () {
    window.clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (!q) { hideSearchResults(); return; }
    searchTimer = window.setTimeout(function () { runSearch(q); }, 180);
  });
  searchInput.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { hideSearchResults(); searchInput.blur(); }
  });
  document.addEventListener('click', function (event) {
    if (!event.target.closest('.tree-search')) hideSearchResults();
  });

  async function runSearch(query) {
    try {
      const res = await fetch('/api/search?q=' + encodeURIComponent(query));
      if (!res.ok) return;
      const data = await res.json();
      renderSearch(data.results || []);
    } catch (err) { /* silent */ }
  }

  function renderSearch(results) {
    searchResults.replaceChildren();
    if (!results.length) {
      const emptyRow = document.createElement('div');
      emptyRow.className = 'search-empty';
      emptyRow.textContent = 'No matches';
      searchResults.appendChild(emptyRow);
    } else {
      results.forEach(function (person) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'search-result';
        const avatar = document.createElement('span');
        avatar.className = 'search-avatar g-' + person.gender;
        avatar.textContent = initials({ given: person.name, surname: '' });
        const text = document.createElement('span');
        text.className = 'search-text';
        const name = document.createElement('strong');
        name.textContent = person.name;
        const sub = document.createElement('small');
        sub.textContent = person.subtitle;
        text.appendChild(name);
        text.appendChild(sub);
        button.appendChild(avatar);
        button.appendChild(text);
        button.addEventListener('click', function () {
          hideSearchResults();
          searchInput.value = '';
          loadTree(person.id, { fit: true }).then(function () {
            centerOnPerson(person.id, { scale: 0.95, yRatio: 0.42 });
            openPerson(person.id);
          });
        });
        searchResults.appendChild(button);
      });
    }
    searchResults.hidden = false;
  }

  function hideSearchResults() {
    searchResults.hidden = true;
    searchResults.replaceChildren();
  }

  /* ------------------------------------------------------------------ */
  /* Loading and data actions                                            */
  /* ------------------------------------------------------------------ */

  async function loadTree(focus, options) {
    const opts = options || {};
    try {
      const url = '/api/tree' + (focus ? '?focus=' + encodeURIComponent(focus) : '');
      const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
      if (res.status === 401) { window.location.href = '/login'; return false; }
      if (!res.ok) throw new Error('load failed');
      const data = await res.json();
      if (!data.persons || !data.persons.length) {
        setData(data);
        showEmptyState();
        return false;
      }
      setData(data);
      state.expanded = new Set();
      state.collapsed = new Set();
      const mobile = window.matchMedia('(max-width: 820px)').matches;
      layoutAndRender({ fit: false });
      if (mobile) focusView();
      else if (opts.fit !== false) fitView();
      updateUrl();
      return true;
    } catch (err) {
      toast('Could not load the tree.');
      return false;
    }
  }

  function showEmptyState() {
    stage.style.display = 'none';
    empty.hidden = false;
    if (statusEl) statusEl.textContent = 'No people yet';
  }

  function updateUrl() {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set('focus', state.focus);
      url.searchParams.delete('open');
      window.history.replaceState(null, '', url.pathname + url.search);
    } catch (err) { /* ignore */ }
  }

  async function toggleExpandAll(expand) {
    state.expanded = new Set();
    state.collapsed = new Set();
    if (expand) {
      const total = state.data.total;
      if (total > 500) {
        const ok = window.confirm('This tree has ' + total + ' people. Expanding every branch may be slow on this device. Continue?');
        if (!ok) return;
      }
      state.persons.forEach(function (person) {
        if (kidsOfPerson(person.id).size) state.expanded.add(person.id);
      });
    } else {
      state.persons.forEach(function (person) {
        if (kidsOfPerson(person.id).size) state.collapsed.add(person.id);
      });
    }
    layoutAndRender({ fit: false });
  }

  /* ------------------------------------------------------------------ */
  /* Wiring                                                              */
  /* ------------------------------------------------------------------ */

  document.getElementById('expand-all').addEventListener('click', function () { toggleExpandAll(true); });
  document.getElementById('collapse-all').addEventListener('click', function () { toggleExpandAll(false); });
  document.getElementById('center-me').addEventListener('click', function () {
    if (app.dataset.self) {
      loadTree(app.dataset.self, { fit: true }).then(function () {
        centerOnPerson(app.dataset.self, { scale: 0.95, yRatio: 0.42 });
        openPerson(app.dataset.self);
      });
    } else {
      toast('Your account is not linked to a person yet. Ask an administrator.');
    }
  });
  document.getElementById('full-tree').addEventListener('click', function () {
    if (app.dataset.rootPerson) loadTree(app.dataset.rootPerson, { fit: true });
  });

  window.addEventListener('resize', function () {
    applyTransform();
    scheduleCulling();
  });

  if (app.dataset.hasPeople === '1') {
    loadTree(app.dataset.focus, { fit: true }).then(function (ok) {
      if (ok && app.dataset.open) openPerson(app.dataset.open);
    });
  } else {
    showEmptyState();
  }
})();
