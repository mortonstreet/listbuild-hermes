#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import defaultdict
from pathlib import Path


SEGMENT_ORDER = ["leadership", "it_ops", "security", "commercial", "admin"]
SEGMENT_LABELS = {
    "leadership": "Leadership",
    "it_ops": "IT Ops",
    "security": "Security",
    "commercial": "Commercial",
    "admin": "Admin",
}


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


def _paired_rows(
    template_rows: list[dict[str, str]],
    rendered_rows: list[dict[str, str]],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    pair_count = min(len(template_rows), len(rendered_rows))
    return [(template_rows[index], rendered_rows[index]) for index in range(pair_count)]


def _samples_by_segment(
    pairs: list[tuple[dict[str, str], dict[str, str]]],
    *,
    samples_per_segment: int,
) -> dict[str, list[tuple[dict[str, str], dict[str, str]]]]:
    grouped: dict[str, list[tuple[dict[str, str], dict[str, str]]]] = defaultdict(list)
    for template_row, rendered_row in pairs:
        segment = (template_row.get("source_role_segment") or rendered_row.get("source_role_segment") or "").strip()
        if not segment:
            continue
        if len(grouped[segment]) >= samples_per_segment:
            continue
        grouped[segment].append((template_row, rendered_row))
    return grouped


def _render_step(step_number: int, template_row: dict[str, str], rendered_row: dict[str, str]) -> str:
    subject_key = f"s{step_number}"
    body_key = f"e{step_number}"
    personalization_key = f"p{step_number}"
    return (
        '<section class="step-card">'
        f'<div class="step-head"><span class="step-index">Step {step_number}</span></div>'
        '<div class="step-grid">'
        '<div class="pane">'
        '<h5>Spintax</h5>'
        '<div class="label">Subject</div>'
        f'<pre>{_esc(template_row.get(subject_key, ""))}</pre>'
        '<div class="label">Body Template</div>'
        f'<pre>{_esc(template_row.get(body_key, ""))}</pre>'
        '<div class="label">Personalization</div>'
        f'<pre>{_esc(template_row.get(personalization_key, ""))}</pre>'
        "</div>"
        '<div class="pane">'
        '<h5>Rendered</h5>'
        '<div class="label">Subject</div>'
        f'<pre>{_esc(rendered_row.get(subject_key, ""))}</pre>'
        '<div class="label">Body</div>'
        f'<pre>{_esc(rendered_row.get(body_key, ""))}</pre>'
        "</div>"
        "</div>"
        "</section>"
    )


def _render_segment_card(
    segment: str,
    pairs: list[tuple[dict[str, str], dict[str, str]]],
) -> str:
    items: list[str] = []
    for index, (template_row, rendered_row) in enumerate(pairs, start=1):
        company_name = rendered_row.get("company_name", "")
        role = rendered_row.get("role", "")
        first_name = rendered_row.get("first_name", "")
        header = (
            '<article class="sample-card">'
            f'<div class="sample-meta"><span class="sample-badge">Sample {index}</span>'
            f'<span>{_esc(company_name)}</span>'
            f'<span>{_esc(role)}</span>'
            f'<span>{_esc(first_name)}</span></div>'
        )
        steps = "".join(_render_step(step_number, template_row, rendered_row) for step_number in range(1, 5))
        items.append(header + steps + "</article>")
    return (
        '<section class="segment-card">'
        f'<h3>{_esc(SEGMENT_LABELS.get(segment, segment.title()))}</h3>'
        f'<p class="muted">Actual examples pulled from the refreshed conversational MSP/MSSP run for <code>{_esc(segment)}</code>.</p>'
        + "".join(items)
        + "</section>"
    )


def _render_html(
    *,
    title: str,
    run_slug: str,
    summary: dict[str, object],
    grouped_pairs: dict[str, list[tuple[dict[str, str], dict[str, str]]]],
) -> str:
    row_count = summary.get("row_count") or summary.get("people_rows") or 0
    company_count = summary.get("company_count") or summary.get("company_rows") or 0
    bucket_counts = summary.get("bucket_counts") or {}
    bucket_line = ", ".join(f"{key}={value}" for key, value in bucket_counts.items()) if isinstance(bucket_counts, dict) else ""

    segments_html = "".join(
        _render_segment_card(segment, grouped_pairs[segment])
        for segment in SEGMENT_ORDER
        if grouped_pairs.get(segment)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe6;
      --paper: #fffdf9;
      --ink: #1f1d19;
      --muted: #655f53;
      --line: #d9cfbf;
      --accent: #a55b18;
      --accent-2: #24445c;
      --soft: #f7f1e6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", serif;
      background: linear-gradient(180deg, #ece2cf 0%, #f8f5ef 100%);
      color: var(--ink);
    }}
    .shell {{ max-width: 1320px; margin: 0 auto; padding: 28px 18px 48px; }}
    .hero, .segment-card {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: 0 10px 28px rgba(0,0,0,.05);
    }}
    .hero {{ padding: 28px 30px; }}
    .eyebrow {{
      margin: 0 0 10px;
      text-transform: uppercase;
      letter-spacing: .12em;
      font: 700 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: var(--accent);
    }}
    h1, h2, h3, h4, h5 {{ margin: 0 0 10px; }}
    h1 {{ font-size: 38px; line-height: 1.02; }}
    h3 {{ font-size: 24px; }}
    h5 {{ font: 700 14px/1.3 ui-sans-serif, system-ui, sans-serif; text-transform: uppercase; letter-spacing: .04em; }}
    p, li {{ font-size: 15px; line-height: 1.55; }}
    code {{ background: #f4ecde; padding: 2px 6px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #fbf7ef;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .muted {{ color: var(--muted); }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 20px;
    }}
    .stat {{
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
    }}
    .stat-label {{
      display: block;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font: 700 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: var(--muted);
    }}
    .stat-value {{
      font: 700 28px/1 ui-sans-serif, system-ui, sans-serif;
      color: var(--accent-2);
    }}
    .segment-list {{ margin-top: 24px; display: grid; gap: 18px; }}
    .segment-card {{ padding: 20px 22px; }}
    .sample-card {{
      margin-top: 18px;
      border-top: 1px solid var(--line);
      padding-top: 18px;
    }}
    .sample-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
      align-items: center;
      font: 600 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: var(--muted);
    }}
    .sample-badge, .step-index {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      background: #f3ead8;
      border: 1px solid var(--line);
      color: var(--accent-2);
    }}
    .step-card {{
      margin-top: 16px;
      background: #fffdfa;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
    }}
    .step-head {{ margin-bottom: 12px; }}
    .step-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .pane {{
      background: #fdf8ef;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
    }}
    .label {{
      margin: 10px 0 8px;
      font: 700 11px/1.2 ui-sans-serif, system-ui, sans-serif;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
    }}
    @media (max-width: 980px) {{
      .stats, .step-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 30px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Final Campaign Snapshot</div>
      <h1>{_esc(title)}</h1>
      <p class="muted">This HTML preview is pulled from the refreshed conversational MSP/MSSP run. Each segment below shows the underlying spintax from <code>people.csv</code> and the fully rendered copy from <code>people-send-ready.csv</code>.</p>
      <div class="stats">
        <div class="stat"><span class="stat-label">Run</span><span class="stat-value">{_esc(run_slug)}</span></div>
        <div class="stat"><span class="stat-label">People</span><span class="stat-value">{_esc(str(row_count))}</span></div>
        <div class="stat"><span class="stat-label">Companies</span><span class="stat-value">{_esc(str(company_count))}</span></div>
        <div class="stat"><span class="stat-label">Mix</span><span class="stat-value">{_esc(bucket_line or "n/a")}</span></div>
      </div>
    </section>
    <section class="segment-list">
      {segments_html}
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--people-csv", required=True)
    parser.add_argument("--send-ready-csv", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--output-html", required=True)
    parser.add_argument("--samples-per-segment", type=int, default=1)
    parser.add_argument("--title", default="Alertica MSP/MSSP Final Copy Preview")
    args = parser.parse_args()

    people_csv = Path(args.people_csv)
    send_ready_csv = Path(args.send_ready_csv)
    summary_json = Path(args.summary_json)
    output_html = Path(args.output_html)

    template_rows = _load_rows(people_csv)
    rendered_rows = _load_rows(send_ready_csv)
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    grouped_pairs = _samples_by_segment(
        _paired_rows(template_rows, rendered_rows),
        samples_per_segment=max(1, args.samples_per_segment),
    )

    html_text = _render_html(
        title=args.title,
        run_slug=str(summary.get("run_slug") or output_html.stem),
        summary=summary,
        grouped_pairs=grouped_pairs,
    )
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(html_text, encoding="utf-8")


if __name__ == "__main__":
    main()
