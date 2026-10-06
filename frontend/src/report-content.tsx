import React, { useEffect, useState } from "react";
import ReactMarkdown, { Components } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

type Heading = { text: string; line: number; level: number };

function headings(body: string): Heading[] {
  let fence = "";
  let math = false;
  const result: Heading[] = [];
  body.split("\n").forEach((line, index) => {
    const code = line.match(/^\s*(`{3,}|~{3,})/);
    if (code) { if (!fence) fence = code[1]; else if (code[1][0] === fence[0] && code[1].length >= fence.length) fence = ""; return; }
    if (fence) return;
    if (line.trim().startsWith("$$")) { if ((line.match(/\$\$/g)?.length ?? 0) % 2) math = !math; return; }
    const match = !math && line.match(/^(#{1,3})\s+(.+?)\s*#*$/);
    if (match) result.push({ text: match[2], level: match[1].length, line: index });
  });
  return result;
}

function scrollToHeading(line: number) {
  const target = document.getElementById(`report-heading-${line}`);
  const disclosure = target?.closest("details");
  if (disclosure) disclosure.open = true;
  target?.scrollIntoView({ behavior: "smooth", block: "start" });
  target?.focus({ preventScroll: true });
}

export function ReportOutline({ body }: { body: string }) {
  const [expanded, setExpanded] = useState(() => window.matchMedia("(min-width:1051px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(min-width:1051px)");
    const change = () => setExpanded(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const entries = headings(body);
  if (!entries.length) return null;
  return <aside className="panel outline"><details open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}><summary><h2>В отчёте</h2></summary><ol>{entries.map((heading) =>
    <li key={heading.line} className={`outline-level-${heading.level}`}><button className="quiet" onClick={() => scrollToHeading(heading.line)}>{heading.text}</button></li>
  )}</ol></details></aside>;
}

// Transform text nodes only: existing links, code, math and URLs remain intact.
type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };
function citationLinks(researchId: string) {
  return () => (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (!node.children || ["link", "code", "inlineCode", "math", "inlineMath"].includes(node.type)) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || !child.value) { visit(child); return [child]; }
        const parts: MarkdownNode[] = [];
        let cursor = 0;
        for (const match of child.value.matchAll(/\[(E\d+)\]/g)) {
          if (match.index! > cursor) parts.push({ type: "text", value: child.value.slice(cursor, match.index) });
          parts.push({ type: "link", url: `#/research/${researchId}/sources?marker=${match[1]}`, children: [{ type: "text", value: match[1] }] });
          cursor = match.index! + match[0].length;
        }
        if (cursor < child.value.length) parts.push({ type: "text", value: child.value.slice(cursor) });
        return parts;
      });
    };
    visit(tree);
  };
}

function MarkdownSection({ body, researchId, startLine }: { body: string; researchId: string; startLine: number }) {
  const blocks: { table: boolean; lines: string[]; line: number }[] = [];
  let fence = "";
  let math = false;
  body.split("\n").forEach((line, index) => {
    const code = line.match(/^\s*(`{3,}|~{3,})/);
    if (code) { if (!fence) fence = code[1]; else if (code[1][0] === fence[0] && code[1].length >= fence.length) fence = ""; }
    if (!fence && line.trim().startsWith("$$") && (line.match(/\$\$/g)?.length ?? 0) % 2) math = !math;
    const table = !fence && !math && line.trim().startsWith("|") && line.trim().endsWith("|");
    if (blocks.at(-1)?.table === table) blocks.at(-1)!.lines.push(line);
    else blocks.push({ table, lines: [line], line: startLine + index });
  });
  const plugins = [remarkMath, citationLinks(researchId)];
  const link: Components["a"] = ({ children, href }) => {
    const citation = href?.match(/\/sources\?marker=(E\d+)$/);
    return <a href={href} className={citation ? "citation" : undefined}
      aria-label={citation ? `Источник ${citation[1]}` : undefined}
      target={href?.startsWith("#") ? undefined : "_blank"}
      rel={href?.startsWith("#") ? undefined : "noopener noreferrer"}>{children}</a>;
  };
  const cell = (text: string) => <ReactMarkdown remarkPlugins={plugins} rehypePlugins={[[rehypeKatex, { strict: "warn", throwOnError: false }]]}
    components={{ p: ({ children }) => <>{children}</>, a: link }}>{text}</ReactMarkdown>;
  return <>{blocks.map((block) => {
    const cells = (line: string) => line.trim().slice(1, -1).split(/(?<!\\)\|/).map((value) => value.trim());
    const separator = block.table && block.lines.length > 1 && cells(block.lines[1]).every((value) => /^:?-{3,}:?$/.test(value));
    if (separator) return <div className="report-table" key={block.line} tabIndex={0} role="region" aria-label="Сравнительная таблица"><table>
      <thead><tr>{cells(block.lines[0]).map((value, index) => <th scope="col" key={index}>{cell(value)}</th>)}</tr></thead>
      <tbody>{block.lines.slice(2).map((row, index) => <tr key={index}>{cells(row).map((value, column) => <td key={column}>{cell(value)}</td>)}</tr>)}</tbody>
    </table></div>;
    const components: Components = { a: link };
    for (const tag of ["h1", "h2", "h3"] as const) {
      components[tag] = ({ children, node }) => React.createElement(tag, {
        id: `report-heading-${block.line + (node?.position?.start.line ?? 1) - 1}`, tabIndex: -1,
      }, children);
    }
    return <ReactMarkdown key={block.line} remarkPlugins={plugins} rehypePlugins={[[rehypeKatex, { strict: "warn", throwOnError: false }]]}
      components={components}>{block.lines.join("\n")}</ReactMarkdown>;
  })}</>;
}

export function ReportContent({ body, researchId }: { body: string; researchId: string }) {
  const lines = body.split("\n");
  const boundaries = headings(body).filter((heading) => heading.level === 2 && ["Источники", "Техническое приложение"].includes(heading.text));
  const sections = boundaries[0]?.line === 0 ? boundaries : [{ text: "", line: 0, level: 0 }, ...boundaries];
  return <>{sections.map((section, index) => {
    const text = lines.slice(section.line, sections[index + 1]?.line).join("\n");
    const content = <MarkdownSection body={text} researchId={researchId} startLine={section.line} />;
    return section.text ? <details className="report-appendix" key={section.line}><summary>{section.text}</summary>{content}</details>
      : <React.Fragment key={section.line}>{content}</React.Fragment>;
  })}</>;
}
