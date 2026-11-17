/* 轻量、安全的 Markdown 渲染器（先整体转义 HTML，再做格式化），无外部依赖。 */
(function () {
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(text) {
    const codes = [];
    let s = text.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = esc(s);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + esc(codes[+i]) + "</code>");
    return s;
  }

  function splitRow(line) {
    let l = line.trim();
    if (l.startsWith("|")) l = l.slice(1);
    if (l.endsWith("|")) l = l.slice(0, -1);
    return l.split("|").map((c) => c.trim());
  }

  function render(src) {
    const lines = (src || "").replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)\s*$/);
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
        i++;
        out.push('<pre><code>' + esc(buf.join("\n")) + "</code></pre>");
        continue;
      }
      if (/^\s*$/.test(line)) { i++; continue; }

      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { const n = Math.min(h[1].length, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }

      if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) { out.push("<hr>"); i++; continue; }

      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
        continue;
      }

      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + render(buf.join("\n")) + "</blockquote>");
        continue;
      }

      const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
      if (listRe.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const items = [];
        while (i < lines.length && (listRe.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
          const m = lines[i].match(listRe);
          if (m && m[1].length < 2) items.push(m[3]);
          else items[items.length - 1] += "\n" + lines[i].trim().replace(/^([-*+]|\d+[.)])\s+/, "• ");
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>` + items.map((it) => "<li>" + inline(it).replace(/\n/g, "<br>") + "</li>").join("") + `</${tag}>`);
        continue;
      }

      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[i])) {
        buf.push(lines[i++]);
      }
      if (!buf.length) buf.push(lines[i++]);
      out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
    }
    return out.join("\n");
  }

  window.renderMarkdown = render;
  window.escapeHtml = esc;
})();
