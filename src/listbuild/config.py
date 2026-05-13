from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _find_dotenv(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_local_env(dotenv_path: Path | None = None) -> None:
    path = dotenv_path or _find_dotenv()
    if path is None:
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return value


def _get_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be a float") from exc


def _get_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer") from exc


def _read_key_group(prefix: str) -> tuple[str, ...]:
    entries: list[tuple[str, str]] = []
    direct = os.environ.get(prefix)
    if direct and direct.strip():
        entries.append((prefix, direct.strip()))

    for key, value in os.environ.items():
        if key.startswith(prefix) and key != prefix and value.strip():
            entries.append((key, value.strip()))

    entries.sort(key=lambda item: item[0])
    return tuple(value for _, value in entries)


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    name: str
    base_url: str
    api_keys: tuple[str, ...]
    timeout_seconds: float
    requests_per_second: float
    burst: int
    max_connections: int


@dataclass(frozen=True, slots=True)
class ProspeoSettings:
    base_url: str
    api_key: str
    timeout_seconds: float
    max_connections: int
    search_qps: float
    search_burst: int
    enrich_qps: float
    enrich_burst: int


@dataclass(frozen=True, slots=True)
class FirecrawlSettings:
    base_url: str
    api_key: str
    timeout_seconds: float
    max_connections: int
    scrape_qps: float
    scrape_burst: int
    map_qps: float
    map_burst: int
    search_qps: float
    search_burst: int


@dataclass(frozen=True, slots=True)
class BrightDataSettings:
    base_url: str
    api_key: str
    crawl_dataset_id: str
    timeout_seconds: float
    max_connections: int
    requests_per_second: float
    burst: int
    poll_interval_seconds: float
    poll_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class MiniMaxSettings:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_connections: int
    requests_per_second: float
    burst: int


@dataclass(frozen=True, slots=True)
class AppSettings:
    serper: ProviderSettings
    harvest: ProviderSettings
    million_verifier: ProviderSettings
    prospeo: ProspeoSettings
    firecrawl: FirecrawlSettings
    brightdata: BrightDataSettings | None
    minimax: MiniMaxSettings | None


def load_settings() -> AppSettings:
    load_local_env()

    timeout_seconds = _get_float("LISTBUILD_TIMEOUT_SECONDS", 30.0)

    serper_keys = _read_key_group("SERPER_API_KEY")
    if not serper_keys:
        raise ConfigError("At least one SERPER_API_KEY value is required")

    serper = ProviderSettings(
        name="serper",
        base_url="https://google.serper.dev",
        api_keys=serper_keys,
        timeout_seconds=_get_float("SERPER_TIMEOUT_SECONDS", timeout_seconds),
        requests_per_second=_get_float("SERPER_QPS", 50.0),
        burst=_get_int("SERPER_BURST", 50),
        max_connections=_get_int("SERPER_MAX_CONNECTIONS", 100),
    )

    harvest = ProviderSettings(
        name="harvest",
        base_url="https://api.harvest-api.com",
        api_keys=(_require_env("HARVEST_API_KEY"),),
        timeout_seconds=_get_float("HARVEST_TIMEOUT_SECONDS", timeout_seconds),
        requests_per_second=_get_float("HARVEST_QPS", 0.0),
        burst=_get_int("HARVEST_BURST", 5),
        max_connections=_get_int("HARVEST_MAX_CONNECTIONS", 5),
    )

    million_verifier = ProviderSettings(
        name="million_verifier",
        base_url="https://api.millionverifier.com",
        api_keys=(_require_env("MILLION_VERIFIER_API_KEY"),),
        timeout_seconds=_get_float("MILLION_VERIFIER_TIMEOUT_SECONDS", timeout_seconds),
        requests_per_second=_get_float("MILLION_VERIFIER_QPS", 5.0),
        burst=_get_int("MILLION_VERIFIER_BURST", 5),
        max_connections=_get_int("MILLION_VERIFIER_MAX_CONNECTIONS", 20),
    )

    prospeo = ProspeoSettings(
        base_url="https://api.prospeo.io",
        api_key=_require_env("PROSPEO_API_KEY"),
        timeout_seconds=_get_float("PROSPEO_TIMEOUT_SECONDS", timeout_seconds),
        max_connections=_get_int("PROSPEO_MAX_CONNECTIONS", 30),
        search_qps=_get_float("PROSPEO_SEARCH_QPS", 1.0),
        search_burst=_get_int("PROSPEO_SEARCH_BURST", 1),
        enrich_qps=_get_float("PROSPEO_ENRICH_QPS", 5.0),
        enrich_burst=_get_int("PROSPEO_ENRICH_BURST", 5),
    )

    firecrawl = FirecrawlSettings(
        base_url="https://api.firecrawl.dev",
        api_key=_require_env("FIRECRAWL_API_KEY"),
        timeout_seconds=_get_float("FIRECRAWL_TIMEOUT_SECONDS", timeout_seconds),
        max_connections=_get_int("FIRECRAWL_MAX_CONNECTIONS", 20),
        scrape_qps=_get_float("FIRECRAWL_SCRAPE_QPS", 10.0 / 60.0),
        scrape_burst=_get_int("FIRECRAWL_SCRAPE_BURST", 1),
        map_qps=_get_float("FIRECRAWL_MAP_QPS", 10.0 / 60.0),
        map_burst=_get_int("FIRECRAWL_MAP_BURST", 1),
        search_qps=_get_float("FIRECRAWL_SEARCH_QPS", 5.0 / 60.0),
        search_burst=_get_int("FIRECRAWL_SEARCH_BURST", 1),
    )

    brightdata_api_key = _optional_env("BRIGHTDATA_API_KEY")
    brightdata_dataset_id = _optional_env("BRIGHTDATA_CRAWL_DATASET_ID")
    brightdata: BrightDataSettings | None = None
    if brightdata_api_key is not None and brightdata_dataset_id is not None:
        brightdata = BrightDataSettings(
            base_url=os.environ.get("BRIGHTDATA_BASE_URL", "https://api.brightdata.com").strip(),
            api_key=brightdata_api_key,
            crawl_dataset_id=brightdata_dataset_id,
            timeout_seconds=_get_float("BRIGHTDATA_TIMEOUT_SECONDS", timeout_seconds),
            max_connections=_get_int("BRIGHTDATA_MAX_CONNECTIONS", 10),
            requests_per_second=_get_float("BRIGHTDATA_QPS", 1.0),
            burst=_get_int("BRIGHTDATA_BURST", 1),
            poll_interval_seconds=_get_float("BRIGHTDATA_POLL_INTERVAL_SECONDS", 2.0),
            poll_timeout_seconds=_get_float("BRIGHTDATA_POLL_TIMEOUT_SECONDS", 120.0),
        )

    minimax_api_key = _optional_env("MINIMAX_API_KEY")
    minimax: MiniMaxSettings | None = None
    if minimax_api_key is not None:
        minimax = MiniMaxSettings(
            base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io").strip(),
            api_key=minimax_api_key,
            model=os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5").strip(),
            timeout_seconds=_get_float("MINIMAX_TIMEOUT_SECONDS", timeout_seconds),
            max_connections=_get_int("MINIMAX_MAX_CONNECTIONS", 10),
            requests_per_second=_get_float("MINIMAX_QPS", 1.0),
            burst=_get_int("MINIMAX_BURST", 1),
        )

    return AppSettings(
        serper=serper,
        harvest=harvest,
        million_verifier=million_verifier,
        prospeo=prospeo,
        firecrawl=firecrawl,
        brightdata=brightdata,
        minimax=minimax,
    )
