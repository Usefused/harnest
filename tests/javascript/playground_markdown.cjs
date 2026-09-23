"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const assets = path.resolve(__dirname, "../../src/harnest/_playground");

// Keep the DOM double small: parsing is the real bundled library, not a mock.
class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.textContent = "";
    this.innerHTML = "";
    this.className = "";
    this.dataset = {};
    this.classList = { add() {}, remove() {} };
  }
  append(...children) {
    for (const child of children) child.parent = this;
    this.children.push(...children);
  }
  closest() { return this.parent; }
  remove() { this.parent?.children.splice(this.parent.children.indexOf(this), 1); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute() {}
  removeAttribute() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function playground() {
  const elements = new Map();
  const document = {
    createElement: (tag) => new Element(tag),
    querySelector: (selector) => {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll: () => [],
  };
  const context = vm.createContext({ document, console, URL, URLSearchParams, atob });
  for (const filename of ["markdown-it.min.js", "markdown.js", "playground.js"]) {
    let source = fs.readFileSync(path.join(assets, filename), "utf8");
    // Exercise production functions without starting network calls or UI listeners.
    if (filename === "playground.js") source = source.replace(/initialize\(\);\s*$/, "");
    vm.runInContext(source, context, { filename });
  }
  return (source) => vm.runInContext(source, context);
}

test("renders common Markdown, tables, nesting, and escaped code", () => {
  const run = playground();
  const source = "# Heading\n\n**bold** and *italic* and ~~old~~\n\n- outer\n  - inner\n\n1. first\n\n> quote\n\n```python\nprint('<script>')\n```\n\n| A | B |\n| :- | -: |\n| x | y |";
  const html = run(`harnestMarkdown.render(${JSON.stringify(source)})`);
  for (const tag of ["h1", "strong", "em", "s", "ul", "ol", "blockquote", "pre", "table"]) {
    assert.match(html, new RegExp(`<${tag}[ >]`));
  }
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /class="markdown-align-right"/);
  assert.doesNotMatch(html, /style=/);
});

test("raw HTML, executable URLs, and images cannot create active content", () => {
  const run = playground();
  const payloads = [
    '<script>alert(1)</script><img src=x onerror=alert(1)>',
    '[x](javascript:alert%281%29)', '[x](JaVaScRiPt:alert%281%29)',
    '[x](jav&#x61;script:alert%281%29)', '[x](data:text/html,attack)',
    '[x](vbscript:attack)', '[x](file:///etc/passwd)',
    '[x](/sessions)', '[x](//attacker.invalid)',
    '![private](https://attacker.invalid/tracking)',
    '<svg onload=alert(1)>', '<iframe src="https://attacker.invalid"></iframe>',
  ];
  for (const source of payloads) {
    const html = run(`harnestMarkdown.render(${JSON.stringify(source)})`);
    assert.doesNotMatch(html, /<(script|img|svg|iframe)\b/i, source);
    assert.doesNotMatch(html, /href="(?:javascript|vbscript|data|file|\/)/i, source);
  }
  const html = run('harnestMarkdown.render("[Docs](https://usefused.com/docs/harnest)")');
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("stream chunks retain Markdown source and tolerate incomplete fences", () => {
  const run = playground();
  run('beginStreamingOutput(); appendStreamingText("**hel"); appendStreamingText("lo**\\n\\n```js\\nconst x = ");');
  assert.match(run("runtime.streamingBubble.innerHTML"), /<strong>hello<\/strong>/);
  run('appendStreamingText("1;\\n```"); finishStreamingOutput({});');
  const bubble = run("ui.conversation.children[0].children[1]");
  assert.match(bubble.innerHTML, /const x = 1;/);
  assert.equal(bubble.markdownSource, "**hello**\n\n```js\nconst x = 1;\n```");
  assert.equal(run("ui.conversation.children.length"), 1);
});

test("response fallback, session history, and tool boundaries use the same renderer", () => {
  const run = playground();
  run('renderOutput([], "**response**"); renderSessionMessage({role:"assistant",content:"**history**"});');
  run('beginStreamingOutput(); showTypingIndicator(); appendStreamingText("**before**"); beginToolBoundary(); appendStreamingText("**after**"); finishStreamingOutput({outputText:"**before****after**"});');
  run('beginStreamingOutput(); showTypingIndicator(); finishStreamingOutput({outputText:"**fallback**"});');
  const bubbles = run("ui.conversation.children.map(turn => turn.children[1].innerHTML)");
  for (const word of ["response", "history", "before", "after", "fallback"]) {
    assert.ok(bubbles.includes(`<p><strong>${word}</strong></p>\n`), word);
  }
  assert.equal(bubbles.length, 5);
});

test("user prompts stay literal and debug results are not interpreted as Markdown", () => {
  const run = playground();
  const bubble = run('appendTurn("user", "**literal** <script>")');
  assert.equal(bubble.textContent, "**literal** <script>");
  assert.equal(bubble.innerHTML, "");
  run('appendResult({value:"**literal** <script>"})');
  assert.doesNotMatch(run("ui.conversation.children.at(-1).innerHTML"), /<strong>|<script>/);
});
