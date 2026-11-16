import assert from "node:assert/strict";
import { test } from "node:test";
import { offlineGraphHtml } from "../web/graphflow-export.mjs";

test("offline graph report escapes metadata and requires no scripts or network resources", () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>example</text></svg>';
  const html = offlineGraphHtml({
    target: "<script>alert(1)</script>",
    filter: "Imports & calls",
    status: "Partial results",
    relations: 'a.ts -> b.ts\n</pre><iframe src="https://example.test">',
    svg,
  });
  assert.ok(html.includes(svg));
  assert.ok(html.includes("default-src 'none'"));
  assert.ok(html.includes("Imports &amp; calls"));
  assert.ok(html.includes("Partial results"));
  assert.ok(html.includes("a.ts -&gt; b.ts"));
  assert.ok(html.includes("&lt;/pre&gt;&lt;iframe"));
  assert.doesNotMatch(html, /<script|<iframe|<link|<img/);
});
