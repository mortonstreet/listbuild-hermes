#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import sys
from collections import Counter
from pathlib import Path


def _set_csv_field_limit() -> None:
    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            return
        except OverflowError:
            field_limit //= 10


def _load_rows(path: Path) -> list[dict[str, str]]:
    _set_csv_field_limit()
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _esc(value: str) -> str:
    return html.escape(value or "")


def _render_sequence(row: dict[str, str], step: int) -> str:
    subject = row.get(f"s{step}", "")
    body = row.get(f"e{step}", "")
    return (
        '<section class="step">'
        f'<div class="step-head">Step {step}</div>'
        '<div class="field">'
        '<div class="label">Subject</div>'
        f'<pre>{_esc(subject)}</pre>'
        "</div>"
        '<div class="field">'
        '<div class="label">Body</div>'
        f'<pre>{_esc(body)}</pre>'
        "</div>"
        "</section>"
    )


def _row_identity(row: dict[str, str]) -> str:
    parts = [
        row.get("full_name", ""),
        row.get("company_name", ""),
        row.get("role", ""),
        row.get("source_role_segment", ""),
    ]
    return " | ".join(part for part in parts if part)


def _render_card(row: dict[str, str]) -> str:
    identity = _row_identity(row).lower()
    search_blob = " ".join(
        [
            row.get("full_name", ""),
            row.get("first_name", ""),
            row.get("last_name", ""),
            row.get("company_name", ""),
            row.get("company_domain", ""),
            row.get("role", ""),
            row.get("source_role_segment", ""),
            row.get("s1", ""),
            row.get("s2", ""),
            row.get("s3", ""),
            row.get("s4", ""),
            row.get("e1", ""),
            row.get("e2", ""),
            row.get("e3", ""),
            row.get("e4", ""),
        ]
    ).lower()
    meta_bits = [
        f'<span class="chip">{_esc(row.get("source_role_segment", ""))}</span>',
        f'<span class="chip">{_esc(row.get("company_name", ""))}</span>',
    ]
    if row.get("role"):
        meta_bits.append(f'<span class="chip">{_esc(row.get("role", ""))}</span>')
    if row.get("person_email"):
        meta_bits.append(f'<span class="chip">{_esc(row.get("person_email", ""))}</span>')
    if row.get("person_linkedin_url"):
        meta_bits.append(
            f'<a class="chip link" href="{_esc(row.get("person_linkedin_url", ""))}" target="_blank" rel="noreferrer">linkedin</a>'
        )
    return (
        f'<article class="lead-card" data-search="{_esc(search_blob)}" data-segment="{_esc(row.get("source_role_segment", "").lower())}" data-identity="{_esc(identity)}">'
        '<div class="lead-head">'
        f'<h3>{_esc(row.get("full_name", "") or "(No Name)")}</h3>'
        f'<p class="lead-sub">{_esc(row.get("company_name", ""))} | {_esc(row.get("role", ""))}</p>'
        f'<div class="chips">{"".join(meta_bits)}</div>'
        "</div>"
        '<div class="sequence-grid">'
        + "".join(_render_sequence(row, step) for step in range(1, 5))
        + "</div>"
        "</article>"
    )


def _render_html(*, title: str, rows: list[dict[str, str]]) -> str:
    segment_counts = Counter((row.get("source_role_segment") or "unknown") for row in rows)
    stats = "".join(
        f'<div class="stat"><span class="stat-label">{_esc(segment)}</span><span class="stat-value">{count}</span></div>'
        for segment, count in sorted(segment_counts.items())
    )
    cards = "".join(_render_card(row) for row in rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3eee5;
      --paper: #fffdf9;
      --ink: #1d1a16;
      --muted: #6a6358;
      --line: #d8cebf;
      --accent: #9a5618;
      --soft: #f8f2e8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #eee3d1 0%, #f8f5ef 100%);
      color: var(--ink);
      font-family: Georgia, "Iowan Old Style", serif;
    }}
    .shell {{ max-width: 1500px; margin: 0 auto; padding: 24px 18px 40px; }}
    .hero, .lead-card {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: 0 10px 28px rgba(0,0,0,.05);
    }}
    .hero {{ padding: 24px 28px; margin-bottom: 18px; }}
    .eyebrow {{
      margin: 0 0 10px;
      text-transform: uppercase;
      letter-spacing: .12em;
      font: 700 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: var(--accent);
    }}
    h1, h2, h3 {{ margin: 0 0 10px; }}
    h1 {{ font-size: 34px; line-height: 1.02; }}
    h3 {{ font-size: 22px; }}
    p, li {{ font-size: 15px; line-height: 1.55; }}
    .muted {{ color: var(--muted); }}
    .controls {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 12px;
    }}
    .input, .select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      background: #fffdfa;
      font: 14px/1.4 ui-sans-serif, system-ui, sans-serif;
    }}
    .stats {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
    }}
    .stat {{
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
    }}
    .stat-label {{
      display: block;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font: 700 11px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: var(--muted);
    }}
    .stat-value {{
      font: 700 24px/1 ui-sans-serif, system-ui, sans-serif;
      color: #243b53;
    }}
    .results {{
      margin: 0 0 16px;
      font: 600 13px/1.3 ui-sans-serif, system-ui, sans-serif;
      color: var(--muted);
    }}
    .lead-list {{
      display: grid;
      gap: 16px;
    }}
    .lead-card {{ padding: 20px 22px; }}
    .lead-head {{ margin-bottom: 14px; }}
    .lead-sub {{ margin: 0 0 10px; color: var(--muted); }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chip {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      background: #f3ebdd;
      border: 1px solid var(--line);
      font: 600 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: var(--muted);
      text-decoration: none;
    }}
    .chip.link {{ color: #24445c; }}
    .sequence-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .step {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: #fffaf2;
    }}
    .step-head {{
      margin-bottom: 10px;
      font: 700 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #243b53;
    }}
    .field + .field {{ margin-top: 10px; }}
    .label {{
      margin-bottom: 6px;
      font: 700 11px/1.2 ui-sans-serif, system-ui, sans-serif;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      border-radius: 12px;
      background: #fbf7ef;
      border: 1px solid var(--line);
      padding: 12px;
      font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .hidden {{ display: none !important; }}
    @media (max-width: 1100px) {{
      .controls, .stats, .sequence-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 28px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Lead-Level Final Copy Review</div>
      <h1>{_esc(title)}</h1>
      <p class="muted">This page is built directly from the actual send-ready CSV. Every card below is a real lead row with rendered sequence copy attached to that specific person, company, and role.</p>
      <div class="controls">
        <input id="search" class="input" type="search" placeholder="Search by name, company, role, subject, or copy...">
        <select id="segment" class="select">
          <option value="">All segments</option>
          <option value="leadership">leadership</option>
          <option value="it_ops">it_ops</option>
          <option value="security">security</option>
          <option value="commercial">commercial</option>
          <option value="admin">admin</option>
        </select>
      </div>
      <div class="stats">
        <div class="stat"><span class="stat-label">Lead Rows</span><span class="stat-value">{len(rows)}</span></div>
        {stats}
      </div>
    </section>
    <p id="results" class="results"></p>
    <section id="lead-list" class="lead-list">
      {cards}
    </section>
  </main>
  <script>
    const search = document.getElementById('search');
    const segment = document.getElementById('segment');
    const results = document.getElementById('results');
    const cards = Array.from(document.querySelectorAll('.lead-card'));

    function applyFilters() {{
      const query = (search.value || '').trim().toLowerCase();
      const seg = (segment.value || '').trim().toLowerCase();
      let visible = 0;
      for (const card of cards) {{
        const matchesQuery = !query || card.dataset.search.includes(query) || card.dataset.identity.includes(query);
        const matchesSegment = !seg || card.dataset.segment === seg;
        const show = matchesQuery && matchesSegment;
        card.classList.toggle('hidden', !show);
        if (show) visible += 1;
      }}
      results.textContent = `${{visible}} lead rows shown`;
    }}

    search.addEventListener('input', applyFilters);
    segment.addEventListener('change', applyFilters);
    applyFilters();
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-ready-csv", required=True)
    parser.add_argument("--output-html", required=True)
    parser.add_argument("--title", default="Alertica Actual Lead Copy Review")
    args = parser.parse_args()

    send_ready_csv = Path(args.send_ready_csv)
    output_html = Path(args.output_html)
    rows = _load_rows(send_ready_csv)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(_render_html(title=args.title, rows=rows), encoding="utf-8")


if __name__ == "__main__":
    main()
