"""Build ADD from Markdown: anchored sections and non-floating vector diagrams."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / 'docs' / name for name in (
    'architecture.md', 'sql-data-model.md', 'decisions.md',
    'operations.md', 'load-report.md',
)]
OUTPUT = ROOT / 'docs/ADD.pdf'
MERMAID = ROOT / 'frontend/node_modules/.bin/mmdc'
PUPPETEER = ROOT / 'scripts/puppeteer.json'


def read_document(source: Path) -> dict:
    # HTML anchors work in Markdown; Pandoc header IDs provide PDF destinations.
    markdown = re.sub(
        r'<a id="([^"]+)"></a>\s*\n(#+ [^\n]+)',
        lambda m: f'{m[2]} {{#{m[1]}}}', source.read_text(encoding='utf-8'),
    )
    return json.loads(subprocess.run(
        ['pandoc', '-f', 'markdown', '-t', 'json'], input=markdown,
        text=True, capture_output=True, check=True,
    ).stdout)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def raw_latex(text: str) -> dict:
    return {'t': 'RawBlock', 'c': ['latex', text]}


def build() -> None:
    documents = {path: read_document(path) for path in SOURCES}
    destinations = {}
    for path, doc in documents.items():
        first = True
        for node in walk(doc['blocks']):
            if node.get('t') == 'Header':
                old_id = node['c'][1][0]
                new_id = f'{path.stem}--{old_id}'
                destinations[(path, old_id)] = new_id
                if first:
                    destinations[(path, '')] = new_id
                    first = False
                node['c'][1][0] = new_id
    for path, doc in documents.items():
        for node in walk(doc['blocks']):
            if node.get('t') != 'Link':
                continue
            target = node['c'][2][0]
            file_part, _, fragment = target.partition('#')
            if ':' in file_part:
                continue
            dest_path = (path.parent / unquote(file_part)).resolve() if file_part else path
            if dest_path in documents:
                key = (dest_path, unquote(fragment))
                if key not in destinations:
                    raise ValueError(f'Broken ADD reference: {path.name}: {target}')
                node['c'][2][0] = '#' + destinations[key]

    with tempfile.TemporaryDirectory(prefix='research-add-') as temporary:
        work = Path(temporary)
        config = work / 'mermaid.json'
        config.write_text(json.dumps({
            'theme': 'neutral', 'markdownAutoWrap': False, 'themeVariables': {'fontFamily': 'Arial', 'fontSize': '18px'},
            'flowchart': {'htmlLabels': True, 'nodeSpacing': 20, 'rankSpacing': 18, 'curve': 'linear', 'wrappingWidth': 320},
            'themeCSS': '.edgeLabel .label { font-size: 18px; }',
            'sequence': {'messageFontSize': 18, 'actorFontSize': 18, 'actorMargin': 20, 'messageMargin': 16, 'diagramMarginX': 8,
                         'diagramMarginY': 8, 'useMaxWidth': False},
            'er': {'fontSize': 18, 'entityPadding': 10},
        }))
        header = work / 'header.tex'
        header.write_text(r'''
\usepackage{graphicx}
\usepackage{needspace}
\usepackage{seqsplit}
\setlength{\emergencystretch}{3em}
''')
        blocks = []
        diagram_number = 0
        for path, doc in documents.items():
            blocks.append(raw_latex(r'\clearpage'))
            source_blocks = doc['blocks']
            document_has_rendered_diagram = False
            escapes = {' ': r'{\ }', '\\': r'\textbackslash{}', '{': r'\{', '}': r'\}',
                       '_': r'\_', '%': r'\%', '#': r'\#', '&': r'\&',
                       '$': r'\$', '^': r'\textasciicircum{}', '~': r'\textasciitilde{}'}
            for node in walk(source_blocks):
                if node.get('t') == 'Code':
                    code = ''.join(escapes.get(c, c) for c in node['c'][1])
                    node.update(t='RawInline', c=['latex', r'\texttt{\seqsplit{' + code + '}}'])
            for i, block in enumerate(source_blocks):
                if block['t'] == 'Header' and block['c'][0] == 2:
                    end = next((j for j in range(i + 1, len(source_blocks))
                                if source_blocks[j]['t'] == 'Header' and source_blocks[j]['c'][0] <= 2), len(source_blocks))
                    has_diagram = any(b['t'] == 'CodeBlock' and 'mermaid' in b['c'][0][1]
                                      for b in source_blocks[i:end])
                    # A diagram section starts with its own introduction and stays in order.
                    blocks.append(raw_latex(r'\clearpage' if has_diagram and document_has_rendered_diagram else r'\Needspace{5\baselineskip}'))
                if block['t'] != 'CodeBlock' or 'mermaid' not in block['c'][0][1]:
                    blocks.append(block)
                    continue
                diagram_number += 1
                document_has_rendered_diagram = True
                diagram_source = work / f'diagram-{diagram_number}.mmd'
                diagram_pdf = work / f'diagram-{diagram_number}.pdf'
                diagram_source.write_text(block['c'][1], encoding='utf-8')
                subprocess.run(
                    [str(MERMAID), '-i', str(diagram_source), '-o', str(diagram_pdf),
                     '-p', str(PUPPETEER), '-c', str(config), '-q'],
                    cwd=ROOT, check=True, timeout=90,
                )
                # No figure float: image and the source's semantic caption remain adjacent.
                blocks.append(raw_latex(
                    '\\begin{center}\n'
                    f'\\includegraphics[width=\\linewidth,height=0.62\\textheight,keepaspectratio]{{{diagram_pdf.as_posix()}}}\n'
                    '\\end{center}\n\\nopagebreak[4]'
                ))
                print(f'Rendered {diagram_number}: {path.name}', flush=True)
        combined = work / 'ADD.json'
        combined.write_text(json.dumps({
            'pandoc-api-version': next(iter(documents.values()))['pandoc-api-version'],
            'meta': {'lang': {'t': 'MetaString', 'c': 'ru-RU'}}, 'blocks': blocks,
        }, ensure_ascii=False), encoding='utf-8')
        result = work / 'ADD.pdf'
        subprocess.run(
            ['pandoc', str(combined), '-f', 'json', '-o', str(result), '--pdf-engine=tectonic',
             '-H', str(header), '-V', 'mainfont=Arial', '-V', 'monofont=Menlo',
             '-V', 'colorlinks=true', '-V', 'linkcolor=blue', '-V', 'urlcolor=blue',
             '-V', 'papersize=a4', '-V', 'geometry:margin=18mm', '-V', 'fontsize=10pt',
             '--toc', '--toc-depth=2', '--number-sections'],
            cwd=ROOT, check=True, timeout=300,
        )
        # Keep the last working PDF intact if rendering or compilation fails.
        OUTPUT.write_bytes(result.read_bytes())
        print(f'Готово: {OUTPUT} ({diagram_number} диаграмм)')


if __name__ == '__main__':
    build()
