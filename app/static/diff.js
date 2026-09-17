/* Word-level diff (LCS) with a line-level fallback for very long texts.
 * Exposes window.TextDiff = { diff(a, b) -> {ops, similarity, unit}, toHtml(ops) }.
 * ops: [{type: "eq" | "del" | "ins", text}] — "del" is in a (baseline) only, "ins" in b only.
 */
(function () {
  "use strict";
  const MAX_CELLS = 6_000_000; // DP table limit (~12 MB as Uint16)

  const words = (s) => s.match(/\S+\s*/g) || [];
  const lines = (s) => s.match(/[^\n]*\n|[^\n]+$/g) || [];
  const key = (tok) => tok.trim();

  function lcsOps(a, b) {
    // Trim common prefix / suffix first — answers often share openings.
    let start = 0;
    while (start < a.length && start < b.length && key(a[start]) === key(b[start])) start++;
    let endA = a.length, endB = b.length;
    while (endA > start && endB > start && key(a[endA - 1]) === key(b[endB - 1])) { endA--; endB--; }

    const A = a.slice(start, endA), B = b.slice(start, endB);
    const n = A.length, m = B.length;
    if ((n + 1) * (m + 1) > MAX_CELLS) return null;

    const w = m + 1;
    const dp = new Uint16Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      const ki = key(A[i]);
      for (let j = m - 1; j >= 0; j--) {
        dp[i * w + j] = ki === key(B[j])
          ? dp[(i + 1) * w + j + 1] + 1
          : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
      }
    }

    const ops = [];
    for (let k = 0; k < start; k++) ops.push({ type: "eq", text: b[k] });
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (key(A[i]) === key(B[j])) { ops.push({ type: "eq", text: B[j] }); i++; j++; }
      else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) { ops.push({ type: "del", text: A[i] }); i++; }
      else { ops.push({ type: "ins", text: B[j] }); j++; }
    }
    while (i < n) ops.push({ type: "del", text: A[i++] });
    while (j < m) ops.push({ type: "ins", text: B[j++] });
    for (let k = endB; k < b.length; k++) ops.push({ type: "eq", text: b[k] });
    return ops;
  }

  function merge(ops) {
    const out = [];
    for (const op of ops) {
      const last = out[out.length - 1];
      if (last && last.type === op.type) last.text += op.text;
      else out.push({ ...op });
    }
    return out;
  }

  function diff(a, b) {
    a = a || ""; b = b || "";
    for (const [unit, split] of [["word", words], ["line", lines]]) {
      const ta = split(a), tb = split(b);
      const ops = lcsOps(ta, tb);
      if (!ops) continue;
      const same = ops.filter((o) => o.type === "eq").length;
      const total = ta.length + tb.length;
      return { ops: merge(ops), similarity: total ? (2 * same) / total : 1, unit };
    }
    return { ops: null, similarity: null, unit: "none" };
  }

  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function toHtml(ops) {
    return ops.map((o) => {
      if (o.type === "eq") return esc(o.text);
      const tag = o.type === "ins" ? "ins" : "del";
      // Keep trailing whitespace outside the highlight so it doesn't smear.
      const m = o.text.match(/^([\s\S]*?)(\s*)$/);
      return `<${tag}>${esc(m[1])}</${tag}>${esc(m[2])}`;
    }).join("");
  }

  window.TextDiff = { diff, toHtml, escape: esc };
})();
