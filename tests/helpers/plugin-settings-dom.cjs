// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Small DOM fixture for the plugin settings behavior tests; native layout is checked separately in Electron.
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

function makeDocument() {
  const ids = new Map();
  const document = { activeElement: null, createElement: tag => new Node(tag), createTextNode: text => Object.assign(new Node('#text'), { textContent: text }),
    getElementById: id => ids.get(id) || null, addEventListener() {}, removeEventListener() {} };
  function matches(value, selector) {
    return selector.split(',').some(part => {
      const token = part.trim();
      if (token.startsWith('#')) return value.id === token.slice(1);
      if (token.startsWith('.')) return token.slice(1).split('.').every(name => value.classList.contains(name));
      const attr = token.match(/^([a-z]+)?\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/i);
      if (attr) return (!attr[1] || value.tagName === attr[1].toUpperCase()) && (attr[3] === undefined
        ? value.getAttribute(attr[2]) !== null : value.getAttribute(attr[2]) === attr[3]);
      return value.tagName === token.toUpperCase();
    });
  }
  class Node {
    constructor(tag = 'div') {
      Object.assign(this, { tagName: tag.toUpperCase(), children: [], listeners: {}, attributes: {}, dataset: {}, style: {},
        hidden: false, disabled: false, textContent: '', className: '', title: '', value: '', checked: false, parentElement: null });
      this.classList = { contains: name => this.className.split(/\s+/).includes(name),
        add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
        remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
        toggle: (name, force) => { const on = force === undefined ? !this.classList.contains(name) : force; this.classList[on ? 'add' : 'remove'](name); return on; } };
    }
    get id() { return this._id || ''; }
    set id(value) { if (this._id) ids.delete(this._id); this._id = value; if (value) ids.set(value, this); }
    get options() { return this.children; }
    get parentNode() { return this.parentElement; }
    get isConnected() { return this === document.body || !!this.parentElement?.isConnected; }
    append(...children) { for (const child of children) this.appendChild(typeof child === 'string' ? document.createTextNode(child) : child); }
    appendChild(child) { child.remove(); child.parentElement = this; this.children.push(child); return child; }
    replaceChildren(...children) { this.children.forEach(child => { child.parentElement = null; }); this.children = []; this.textContent = ''; this.append(...children); }
    remove() { if (!this.parentElement) return; this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    contains(value) { return value === this || this.children.some(child => child.contains(value)); }
    setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'id') this.id = String(value); }
    getAttribute(key) { if (key === 'id') return this.id || null; return this.attributes[key] ?? null; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(key, handler) { (this.listeners[key] ||= []).push(handler); }
    removeEventListener(key, handler) { this.listeners[key] = (this.listeners[key] || []).filter(item => item !== handler); }
    async emit(type, extra = {}) { const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extra };
      for (const handler of this.listeners[type] || []) await handler(event); await settle(); }
    async click() { if (!this.disabled) await this.emit('click'); }
    focus() { document.activeElement = this; }
    scrollIntoView() {}
    matches(selector) { return matches(this, selector); }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    checkValidity() { return true; }
    reportValidity() { return true; }
  }
  document.body = new Node('body');
  document.querySelector = selector => document.body.querySelector(selector);
  document.querySelectorAll = selector => document.body.querySelectorAll(selector);
  const add = (id, parent = document.body, tag = 'div') => { const node = document.createElement(tag); node.id = id; parent.appendChild(node); return node; };
  return { document, ids, add, Node };
}
const text = value => String(value?.textContent || '') + (value?.children || []).map(text).join('');
const find = (value, predicate) => { if (predicate(value)) return value; for (const child of value?.children || []) { const match = find(child, predicate); if (match) return match; } };
const button = (value, label) => find(value, item => item.tagName === 'BUTTON' && text(item).includes(label));
module.exports = { makeDocument, settle, text, find, button };
