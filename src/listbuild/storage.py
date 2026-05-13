from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

import httpx

from listbuild.datasets import PersonRow, load_harvest_profile_rows
from listbuild.normalize import normalize_url
from listbuild.runs import RunPaths
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Json
except ImportError:  # pragma: no cover - optional runtime dependency
    psycopg = None
    sql = None
    Json = None


class SupabaseStorageError(RuntimeError):
    """Raised when Supabase or Postgres storage is misconfigured or rejects a request."""


def _collapse_spaces(value: str) -> str:
    return " ".join(value.split())


def _normalize_identity_text(value: str) -> str:
    return _collapse_spaces(value).strip().lower()


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_linkedin_key(value: str) -> str:
    normalized = normalize_url(value)
    if normalized is None:
        return value.strip().rstrip("/").lower()
    return normalized.normalized_url.rstrip("/")


def _slug_text(value: str) -> str:
    lowered = _normalize_identity_text(value)
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _sanitize_postgres_dsn(dsn: str) -> str:
    parsed = urlsplit(dsn)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "pgbouncer"
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_pairs),
            parsed.fragment,
        )
    )


def build_company_record_key(
    *,
    company_linkedin_url: str = "",
    company_domain: str = "",
    company_name: str = "",
) -> str:
    if company_linkedin_url.strip():
        return f"li_company:{_normalize_linkedin_key(company_linkedin_url)}"
    if company_domain.strip():
        normalized = normalize_url(company_domain)
        if normalized is not None:
            return f"domain:{normalized.host}"
        return f"domain:{company_domain.strip().lower()}"
    if company_name.strip():
        return f"name:{_slug_text(company_name)}"
    return ""


def build_person_record_key(
    *,
    person_linkedin_url: str = "",
    person_email: str = "",
    company_domain: str = "",
    person_full_name: str = "",
) -> str:
    if person_linkedin_url.strip():
        return f"li_person:{_normalize_linkedin_key(person_linkedin_url)}"
    if person_email.strip():
        return f"email:{_normalize_email(person_email)}"
    if company_domain.strip() and person_full_name.strip():
        return (
            f"name_company:{build_company_record_key(company_domain=company_domain)}"
            f"::{_slug_text(person_full_name)}"
        )
    if person_full_name.strip():
        return f"name:{_slug_text(person_full_name)}"
    return ""


@dataclass(frozen=True, slots=True)
class SupabaseConfig:
    url: str
    service_role_key: str
    schema: str = "public"

    @classmethod
    def from_env(cls) -> "SupabaseConfig":
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = (
            os.environ.get("SUPABASE_SECRET_KEY", "").strip()
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        schema = os.environ.get("SUPABASE_SCHEMA", "public").strip() or "public"
        if not url or not key:
            raise SupabaseStorageError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY are required for Supabase REST storage"
            )
        return cls(url=url.rstrip("/"), service_role_key=key, schema=schema)

    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            os.environ.get("SUPABASE_URL", "").strip()
            and (
                os.environ.get("SUPABASE_SECRET_KEY", "").strip()
                or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            )
        )


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    dsn: str
    schema: str = "public"

    @classmethod
    def is_configured(cls) -> bool:
        if os.environ.get("SUPABASE_DB_URL", "").strip():
            return True
        required = [
            "SUPABASE_DB_HOST",
            "SUPABASE_DB_NAME",
            "SUPABASE_DB_USER",
            "SUPABASE_DB_PASSWORD",
        ]
        return all(os.environ.get(name, "").strip() for name in required)

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        schema = os.environ.get("SUPABASE_SCHEMA", "public").strip() or "public"
        dsn = os.environ.get("SUPABASE_DB_URL", "").strip()
        if dsn:
            return cls(dsn=_sanitize_postgres_dsn(dsn), schema=schema)

        host = os.environ.get("SUPABASE_DB_HOST", "").strip()
        port = os.environ.get("SUPABASE_DB_PORT", "6543").strip() or "6543"
        name = os.environ.get("SUPABASE_DB_NAME", "postgres").strip() or "postgres"
        user = os.environ.get("SUPABASE_DB_USER", "postgres").strip() or "postgres"
        password = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
        sslmode = os.environ.get("SUPABASE_DB_SSLMODE", "require").strip() or "require"
        if not host or not password:
            raise SupabaseStorageError(
                "SUPABASE_DB_URL or SUPABASE_DB_HOST/SUPABASE_DB_PASSWORD credentials are required for direct Postgres access"
            )
        built_dsn = (
            f"postgresql://{user}:{password}@{host}:{port}/{name}"
            f"?sslmode={sslmode}"
        )
        return cls(dsn=_sanitize_postgres_dsn(built_dsn), schema=schema)


def _require_psycopg() -> None:
    if psycopg is None or sql is None or Json is None:
        raise SupabaseStorageError(
            "Direct Postgres access requires psycopg. Reinstall with project dependencies."
        )


class SupabaseStorage:
    def __init__(self, config: SupabaseConfig) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=f"{config.url}/rest/v1",
            timeout=30.0,
            headers={
                "apikey": config.service_role_key,
                "Authorization": f"Bearer {config.service_role_key}",
                "Content-Type": "application/json",
                "Accept-Profile": config.schema,
                "Content-Profile": config.schema,
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SupabaseStorage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_rows(
        self,
        *,
        table: str,
        rows: list[dict[str, Any]],
        on_conflict: str,
        batch_size: int = 500,
    ) -> None:
        if not rows:
            return
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            response = self._client.post(
                f"/{table}",
                params={"on_conflict": on_conflict},
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                json=batch,
            )
            if response.status_code >= 400:
                raise SupabaseStorageError(
                    f"Supabase upsert failed for table {table}: {response.status_code} {response.text}"
                )


class PostgresStorage:
    def __init__(self, config: PostgresConfig) -> None:
        _require_psycopg()
        self._config = config
        self._conn = None

    def __enter__(self) -> "PostgresStorage":
        self._conn = psycopg.connect(self._config.dsn)
        try:
            self._conn.prepare_threshold = None
        except AttributeError:
            pass
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            self._conn.close()

    def ping(self) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            cursor.execute("select 1")
            cursor.fetchone()

    def apply_sql_file(self, path: str | Path) -> None:
        assert self._conn is not None
        payload = Path(path).read_text(encoding="utf-8")
        with self._conn.cursor() as cursor:
            statements = [
                statement.strip()
                for statement in payload.split(";")
                if statement.strip()
            ]
            for statement in statements:
                cursor.execute(statement)

    def fetch_existing_keys(self, *, table: str, keys: Iterable[str]) -> set[str]:
        assert self._conn is not None
        key_list = [key for key in keys if key]
        if not key_list:
            return set()
        query = sql.SQL(
            "select record_key from {}.{} where record_key = any(%s)"
        ).format(
            sql.Identifier(self._config.schema),
            sql.Identifier(table),
        )
        with self._conn.cursor() as cursor:
            cursor.execute(query, (key_list,))
            rows = cursor.fetchall()
        return {str(row[0]) for row in rows}

    def upsert_rows(
        self,
        *,
        table: str,
        rows: list[dict[str, Any]],
        conflict_columns: tuple[str, ...],
        batch_size: int = 500,
    ) -> None:
        assert self._conn is not None
        if not rows:
            return
        columns = list(rows[0].keys())
        conflict_set = set(conflict_columns)
        update_columns = [column for column in columns if column not in conflict_set]
        insert_sql = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
        conflict_sql = sql.SQL(", ").join(
            sql.Identifier(column) for column in conflict_columns
        )
        if update_columns:
            assignments = [
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(column),
                    sql.Identifier(column),
                )
                for column in update_columns
            ]
            if table != "raw_provider_events":
                assignments.append(sql.SQL("updated_at = now()"))
            update_sql = sql.SQL(", ").join(assignments)
            on_conflict = sql.SQL("do update set {}").format(update_sql)
        else:
            on_conflict = sql.SQL("do nothing")

        statement = sql.SQL(
            "insert into {}.{} ({}) values ({}) on conflict ({}) {}"
        ).format(
            sql.Identifier(self._config.schema),
            sql.Identifier(table),
            insert_sql,
            placeholders,
            conflict_sql,
            on_conflict,
        )

        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            payload = [
                [self._adapt_value(row.get(column)) for column in columns]
                for row in batch
            ]
            with self._conn.cursor() as cursor:
                cursor.executemany(statement, payload)

    @staticmethod
    def _adapt_value(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return Json(value)
        return value


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def _read_csv_rows(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    with target.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl_rows(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _normalize_company_name_key(value: str) -> str:
    return _collapse_spaces(str(value or "")).strip().casefold()


def _company_domain_lookup(company_rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in company_rows:
        company_name = _normalize_company_name_key(row.get("company_name") or "")
        company_domain = str(row.get("company_domain") or "").strip().lower()
        if company_name and company_domain:
            lookup[company_name] = company_domain
    return lookup


def _row_has_hidden_person_identity(row: dict[str, Any]) -> bool:
    return any(
        str(row.get(field) or "").strip()
        for field in (
            "person_linkedin_url",
            "person_email",
            "harvest_profile_id",
        )
    )


def _enrich_people_csv_rows_with_company_domain(
    rows: Iterable[dict[str, Any]],
    company_domains_by_name: dict[str, str],
) -> list[dict[str, Any]]:
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized = {**row}
        company_name = _normalize_company_name_key(row.get("company_name") or "")
        if not str(normalized.get("company_domain") or "").strip() and company_name:
            normalized["company_domain"] = company_domains_by_name.get(company_name, "")
        enriched_rows.append(normalized)
    return enriched_rows


def _merge_people_export_with_harvest_identity(
    *,
    export_rows: list[dict[str, Any]],
    raw_rows: list[PersonRow],
    company_domains_by_name: dict[str, str],
) -> list[dict[str, Any]]:
    merged_rows: list[dict[str, Any]] = []
    raw_by_record_key: dict[str, dict[str, Any]] = {}
    raw_by_name_company: dict[tuple[str, str], dict[str, Any]] = {}

    for raw_row in raw_rows:
        storage_row = raw_row.to_storage_dict()
        normalized = person_row_to_supabase(storage_row)
        record_key = str(normalized.get("record_key") or "").strip()
        if record_key:
            raw_by_record_key[record_key] = storage_row
        name_company_key = (
            _normalize_company_name_key(storage_row.get("company_name") or ""),
            _normalize_identity_text(storage_row.get("full_name") or ""),
        )
        if all(name_company_key):
            raw_by_name_company[name_company_key] = storage_row

    matched_record_keys: set[str] = set()
    for export_row in export_rows:
        enriched_export_row = _enrich_people_csv_rows_with_company_domain(
            [export_row],
            company_domains_by_name,
        )[0]
        normalized_export = person_row_to_supabase(enriched_export_row)
        record_key = str(normalized_export.get("record_key") or "").strip()
        matched_row = raw_by_record_key.get(record_key)

        if matched_row is None:
            name_company_key = (
                _normalize_company_name_key(export_row.get("company_name") or ""),
                _normalize_identity_text(export_row.get("full_name") or ""),
            )
            if all(name_company_key):
                matched_row = raw_by_name_company.get(name_company_key)

        if matched_row is None:
            merged_rows.append(normalized_export)
            continue

        merged = {**matched_row}
        for field in PersonRow.EXPORT_HEADERS:
            export_value = str(export_row.get(field) or "").strip()
            if export_value:
                merged[field] = export_value
        normalized_merged = person_row_to_supabase(merged)
        record_key = str(normalized_merged.get("record_key") or "").strip()
        if record_key:
            matched_record_keys.add(record_key)
        merged_rows.append(normalized_merged)

    for record_key, raw_row in raw_by_record_key.items():
        if record_key in matched_record_keys:
            continue
        merged_rows.append(person_row_to_supabase(raw_row))

    return merged_rows


def company_row_to_supabase(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {**row}
    normalized["company_quality_score"] = _as_int(row.get("company_quality_score"))
    normalized["company_needs_followup"] = _as_bool(
        row.get("company_needs_followup")
    )
    normalized["company_domain"] = str(row.get("company_domain") or "").strip().lower()
    normalized["company_linkedin_url"] = str(
        row.get("company_linkedin_url") or ""
    ).strip()
    normalized["record_key"] = build_company_record_key(
        company_linkedin_url=normalized["company_linkedin_url"],
        company_domain=normalized["company_domain"],
        company_name=str(row.get("company_name") or ""),
    )
    return normalized


def person_row_to_supabase(row: dict[str, Any]) -> dict[str, Any]:
    full_name = str(row.get("full_name") or row.get("person_full_name") or "").strip()
    first_name = str(
        row.get("first_name") or row.get("person_first_name") or ""
    ).strip()
    last_name = str(row.get("last_name") or row.get("person_last_name") or "").strip()
    company_name = str(row.get("company_name") or "").strip()
    company_domain = str(row.get("company_domain") or "").strip().lower()
    role = str(row.get("role") or row.get("person_title") or "").strip()
    role_description = str(
        row.get("role_description") or row.get("person_about") or ""
    ).strip()
    person_linkedin_url = str(row.get("person_linkedin_url") or "").strip()
    person_email = _normalize_email(str(row.get("person_email") or ""))
    source_role_segment = str(row.get("source_role_segment") or "").strip()

    normalized = {
        "run_slug": str(row.get("run_slug") or "").strip(),
        "seller_slug": str(row.get("seller_slug") or "").strip(),
        "segment_slug": str(row.get("segment_slug") or "").strip(),
        "source_role_segment": source_role_segment,
        "company_name": company_name,
        "company_domain": company_domain,
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "role": role,
        "role_description": role_description,
        "e1": str(row.get("e1") or "").strip(),
        "e2": str(row.get("e2") or "").strip(),
        "e3": str(row.get("e3") or "").strip(),
        "e4": str(row.get("e4") or "").strip(),
        "s1": str(row.get("s1") or "").strip(),
        "s2": str(row.get("s2") or "").strip(),
        "s3": str(row.get("s3") or "").strip(),
        "s4": str(row.get("s4") or "").strip(),
        "p1": str(row.get("p1") or "").strip(),
        "p2": str(row.get("p2") or "").strip(),
        "p3": str(row.get("p3") or "").strip(),
        "p4": str(row.get("p4") or "").strip(),
        "person_linkedin_url": person_linkedin_url,
        "person_email": person_email,
        "person_email_status": str(row.get("person_email_status") or "").strip(),
        "person_quality_score": _as_int(row.get("person_quality_score")),
        "harvest_profile_id": str(row.get("harvest_profile_id") or "").strip(),
        "harvest_status": str(row.get("harvest_status") or "").strip(),
        "person_full_name": full_name,
        "person_first_name": first_name,
        "person_last_name": last_name,
        "person_title": role,
        "person_about": role_description,
    }
    normalized["record_key"] = build_person_record_key(
        person_linkedin_url=person_linkedin_url,
        person_email=person_email,
        company_domain=company_domain,
        person_full_name=full_name,
    )
    return normalized


def run_payload_to_supabase(
    *,
    run_slug: str,
    seller_slug: str,
    segment_slug: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_slug": run_slug,
        "seller_slug": seller_slug,
        "segment_slug": segment_slug,
        "metadata": metadata,
    }


def _infer_event_entity(record: dict[str, Any]) -> tuple[str, str]:
    response = record.get("response")
    if not isinstance(response, dict):
        response = {}
    element = response.get("element")
    if not isinstance(element, dict):
        element = {}

    for key in ("company_url", "company_linkedin_url"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return "company", value.strip()
    for key in ("linkedin_url", "profile"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return "person", value.strip()
    for key in ("linkedinUrl", "website", "name"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            entity_type = "company" if "company" in value.lower() or key != "linkedinUrl" else "person"
            if key == "linkedinUrl":
                entity_type = "company" if "/company/" in value.lower() else "person"
            return entity_type, value.strip()
    query = record.get("query")
    if isinstance(query, str) and query.strip():
        return "search", query.strip()
    return "", ""


def raw_event_to_supabase(run_slug: str, record: dict[str, Any]) -> dict[str, Any]:
    provider = str(record.get("provider") or "").strip()
    stage = str(record.get("stage") or "").strip()
    entity_type, entity_key = _infer_event_entity(record)
    serialized = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(
        f"{run_slug}::{provider}::{stage}::{entity_type}::{entity_key}::{serialized}".encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "event_key": digest,
        "run_slug": run_slug,
        "provider": provider,
        "stage": stage,
        "entity_type": entity_type,
        "entity_key": entity_key,
        "payload": record,
    }


def direct_postgres_available() -> bool:
    return PostgresConfig.is_configured()


def fetch_existing_company_record_keys(keys: Iterable[str]) -> set[str]:
    if not PostgresConfig.is_configured():
        return set()
    with PostgresStorage(PostgresConfig.from_env()) as storage:
        return storage.fetch_existing_keys(table="company_records", keys=keys)


def fetch_existing_person_record_keys(keys: Iterable[str]) -> set[str]:
    if not PostgresConfig.is_configured():
        return set()
    with PostgresStorage(PostgresConfig.from_env()) as storage:
        return storage.fetch_existing_keys(table="person_records", keys=keys)


def ping_postgres() -> None:
    with PostgresStorage(PostgresConfig.from_env()) as storage:
        storage.ping()


def apply_schema_file(path: str | Path) -> None:
    with PostgresStorage(PostgresConfig.from_env()) as storage:
        storage.apply_sql_file(path)


def sync_run_to_supabase(
    *,
    paths: RunPaths,
    metadata: dict[str, Any] | None = None,
    config: SupabaseConfig | None = None,
    postgres_config: PostgresConfig | None = None,
    company_rows_override: list[dict[str, Any]] | None = None,
    people_rows_override: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    resolved_metadata = {
        **(metadata or {}),
        "paths": paths.to_dict(),
    }
    company_csv_rows = _read_csv_rows(paths.company_csv)
    company_rows = (
        [company_row_to_supabase(row) for row in company_rows_override]
        if company_rows_override is not None
        else [company_row_to_supabase(row) for row in company_csv_rows]
    )
    if people_rows_override is not None:
        people_rows = [person_row_to_supabase(row) for row in people_rows_override]
    else:
        people_csv_rows = _read_csv_rows(paths.people_csv)
        company_domains_by_name = _company_domain_lookup(company_csv_rows)
        enriched_people_csv_rows = _enrich_people_csv_rows_with_company_domain(
            people_csv_rows,
            company_domains_by_name,
        )
        if any(_row_has_hidden_person_identity(row) for row in enriched_people_csv_rows):
            people_rows = [
                person_row_to_supabase(row) for row in enriched_people_csv_rows
            ]
        elif paths.harvest_profile_jsonl.is_file():
            raw_people_rows = load_harvest_profile_rows(
                input_path=paths.harvest_profile_jsonl,
                naming=paths.naming,
                company_domains_by_name=company_domains_by_name,
            )
            people_rows = _merge_people_export_with_harvest_identity(
                export_rows=people_csv_rows,
                raw_rows=raw_people_rows,
                company_domains_by_name=company_domains_by_name,
            )
        else:
            people_rows = [
                person_row_to_supabase(row) for row in enriched_people_csv_rows
            ]
    raw_records: list[dict[str, Any]] = []
    for raw_path in (
        paths.serper_jsonl,
        paths.harvest_company_jsonl,
        paths.harvest_profile_jsonl,
        paths.firecrawl_jsonl,
    ):
        raw_records.extend(_read_jsonl_rows(raw_path))
    raw_rows = [
        raw_event_to_supabase(paths.naming.run_slug, record) for record in raw_records
    ]

    if PostgresConfig.is_configured():
        with PostgresStorage(postgres_config or PostgresConfig.from_env()) as storage:
            storage.upsert_rows(
                table="scrape_runs",
                rows=[
                    run_payload_to_supabase(
                        run_slug=paths.naming.run_slug,
                        seller_slug=paths.naming.seller_slug,
                        segment_slug=paths.naming.segment_slug,
                        metadata=resolved_metadata,
                    )
                ],
                conflict_columns=("run_slug",),
            )
            storage.upsert_rows(
                table="company_records",
                rows=[row for row in company_rows if row.get("record_key")],
                conflict_columns=("record_key",),
            )
            storage.upsert_rows(
                table="person_records",
                rows=[row for row in people_rows if row.get("record_key")],
                conflict_columns=("record_key",),
            )
            storage.upsert_rows(
                table="raw_provider_events",
                rows=raw_rows,
                conflict_columns=("event_key",),
            )
    else:
        with SupabaseStorage(config or SupabaseConfig.from_env()) as storage:
            storage.upsert_rows(
                table="scrape_runs",
                rows=[
                    run_payload_to_supabase(
                        run_slug=paths.naming.run_slug,
                        seller_slug=paths.naming.seller_slug,
                        segment_slug=paths.naming.segment_slug,
                        metadata=resolved_metadata,
                    )
                ],
                on_conflict="run_slug",
            )
            storage.upsert_rows(
                table="company_records",
                rows=[row for row in company_rows if row.get("record_key")],
                on_conflict="record_key",
            )
            storage.upsert_rows(
                table="person_records",
                rows=[row for row in people_rows if row.get("record_key")],
                on_conflict="record_key",
            )
            storage.upsert_rows(
                table="raw_provider_events",
                rows=raw_rows,
                on_conflict="event_key",
            )

    return {
        "company_rows": len(company_rows),
        "person_rows": len(people_rows),
        "raw_events": len(raw_rows),
    }
