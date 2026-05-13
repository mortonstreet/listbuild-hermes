#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_CANDIDATES_CSV = "/Users/mortonstreet/alertica/alertica-tiers/alertica_msp_mssp_51-200_201-500_people_serper_candidates.csv"
DEFAULT_PROFILES_JSONL = "/Users/mortonstreet/alertica/alertica-tiers/alertica_msp_mssp_51-200_201-500_people_harvest_qualifier_profiles.jsonl"
DEFAULT_OUTPUT_DIR = "/Users/mortonstreet/alertica/alertica-tiers"
DEFAULT_BASE_NAME = "alertica_msp_mssp_51-200_201-500_people_harvest_qualifier_snapshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build snapshot qualification CSVs from the current Harvest profile cache."
    )
    parser.add_argument("--candidates-csv", default=DEFAULT_CANDIDATES_CSV)
    parser.add_argument("--profiles-jsonl", default=DEFAULT_PROFILES_JSONL)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-name", default=DEFAULT_BASE_NAME)
    return parser.parse_args()


def load_qual_module() -> Any:
    module_path = REPO_ROOT / "scripts" / "alertica_msp_mssp_people_harvest_qualify.py"
    spec = importlib.util.spec_from_file_location("alertica_harvest_qual", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load harvest qualifier module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    args = parse_args()
    qual = load_qual_module()

    candidates_csv = Path(args.candidates_csv).expanduser().resolve()
    profiles_jsonl = Path(args.profiles_jsonl).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_csv = output_dir / f"{args.base_name}_all.csv"
    final_csv = output_dir / f"{args.base_name}_final.csv"
    summary_json = output_dir / f"{args.base_name}_summary.json"

    candidate_rows = read_csv(candidates_csv)
    profile_lookup = qual.load_existing_profile_results(profiles_jsonl)
    scored_rows = qual.score_rows(candidate_rows, profile_lookup)
    final_rows = qual.select_final_people(scored_rows)

    all_fieldnames = [
        "company_key",
        "company_name",
        "company_domain",
        "company_linkedin_url",
        "company_size",
        "headcount_exact",
        "headcount_range",
        "target_people_cap",
        "candidate_pool_target",
        "person_name_guess",
        "role_guess",
        "linkedin_profile_url",
        "source_role_segments",
        "requested_titles",
        "matched_queries",
        "serper_hit_count",
        "min_serper_rank",
        "first_seen_at",
        "best_serper_title",
        "best_serper_snippet",
        "company_description",
        "company_offer",
        "company_icp",
        "company_painpoint",
        "company_signals",
        "company_signal_sources",
        "harvest_status",
        "harvest_profile_id",
        "harvest_full_name",
        "harvest_headline",
        "harvest_about",
        "harvest_current_company",
        "harvest_location",
        "current_employer_match",
        "title_match",
        "matched_titles",
        "matched_role_segments",
        "primary_matched_title",
        "primary_matched_role_segment",
        "title_priority",
        "person_soft_qualifies",
        "person_soft_qualifier_reason",
        "serper_hits_json",
        "harvest_json",
    ]
    final_fieldnames = [*all_fieldnames, "selection_rank_within_company", "selected_for_final_list"]

    write_csv(all_csv, scored_rows, all_fieldnames)
    write_csv(final_csv, final_rows, final_fieldnames)

    successes = sum(1 for result in profile_lookup.values() if not qual.clean_text(result.get("error")))
    errors = sum(1 for result in profile_lookup.values() if qual.clean_text(result.get("error")))
    summary = {
        "snapshot_profile_successes": successes,
        "snapshot_profile_errors": errors,
        "snapshot_candidate_rows": len(candidate_rows),
        "snapshot_qualified_rows": sum(
            1 for row in scored_rows if qual.clean_text(row.get("person_soft_qualifies")).lower() == "yes"
        ),
        "snapshot_final_selected_rows": len(final_rows),
        "output_files": {
            "all_csv": str(all_csv),
            "final_csv": str(final_csv),
            "summary_json": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
