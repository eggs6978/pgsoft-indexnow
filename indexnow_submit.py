from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


CONFIG_FILE = Path("sites.json")
STATE_FILE = Path("data/indexnow_state.json")

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

USER_AGENT = (
    "GitHub-Actions-MultiSite-IndexNow/2.0"
)

REQUEST_TIMEOUT = 45
MAX_RETRIES = 4
MAX_BATCH_SIZE = 10000
MAX_SITEMAP_DEPTH = 10


@dataclass(frozen=True)
class SiteConfig:
    name: str
    enabled: bool
    host: str
    sitemap: str
    key_location: str
    key_env: str


def log(message: str) -> None:
    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"[{timestamp}] {message}", flush=True)


def load_json_file(
    path: Path,
    default: Any,
) -> Any:
    if not path.exists():
        return default

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        raise RuntimeError(
            f"无法读取 JSON 文件 {path}：{error}"
        ) from error


def load_site_configs() -> list[SiteConfig]:
    raw_config = load_json_file(
        CONFIG_FILE,
        {},
    )

    raw_sites = raw_config.get("sites", [])

    if not isinstance(raw_sites, list):
        raise RuntimeError(
            "sites.json 的 sites 必须是数组"
        )

    sites: list[SiteConfig] = []

    for index, item in enumerate(raw_sites):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"sites[{index}] 格式错误"
            )

        site = SiteConfig(
            name=str(item.get("name", "")).strip(),
            enabled=bool(
                item.get("enabled", True)
            ),
            host=str(item.get("host", "")).strip(),
            sitemap=str(
                item.get("sitemap", "")
            ).strip(),
            key_location=str(
                item.get("key_location", "")
            ).strip(),
            key_env=str(
                item.get("key_env", "")
            ).strip(),
        )

        validate_site_config(site)
        sites.append(site)

    if not sites:
        raise RuntimeError(
            "sites.json 没有设置任何网站"
        )

    return sites


def validate_site_config(
    site: SiteConfig,
) -> None:
    required = {
        "name": site.name,
        "host": site.host,
        "sitemap": site.sitemap,
        "key_location": site.key_location,
        "key_env": site.key_env,
    }

    missing = [
        key
        for key, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            f"网站设置缺少栏位："
            f"{', '.join(missing)}"
        )

    sitemap_host = urlparse(
        site.sitemap
    ).hostname

    key_host = urlparse(
        site.key_location
    ).hostname

    if sitemap_host != site.host:
        raise RuntimeError(
            f"{site.name} 的 sitemap 主机 "
            f"{sitemap_host} 与 host "
            f"{site.host} 不一致"
        )

    if key_host != site.host:
        raise RuntimeError(
            f"{site.name} 的 key_location 主机 "
            f"{key_host} 与 host "
            f"{site.host} 不一致"
        )


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/xml,text/xml,"
                "text/plain,*/*"
            ),
            "Cache-Control": "no-cache",
        },
        method="GET",
    )

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                return response.read()

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
        ) as error:
            last_error = error

            log(
                f"读取失败，第 {attempt} 次："
                f"{url}；{error}"
            )

            if attempt < MAX_RETRIES:
                wait_seconds = min(
                    5 * (2 ** (attempt - 1)),
                    30,
                )

                time.sleep(wait_seconds)

    raise RuntimeError(
        f"无法读取 {url}：{last_error}"
    )


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def collect_sitemap_urls(
    sitemap_url: str,
    *,
    visited: set[str] | None = None,
    depth: int = 0,
) -> dict[str, str]:
    if depth > MAX_SITEMAP_DEPTH:
        raise RuntimeError(
            "Sitemap 嵌套层级过深"
        )

    if visited is None:
        visited = set()

    if sitemap_url in visited:
        return {}

    visited.add(sitemap_url)

    log(f"读取 Sitemap：{sitemap_url}")

    xml_data = http_get(sitemap_url)

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as error:
        raise RuntimeError(
            f"Sitemap XML 格式错误：{error}"
        ) from error

    root_type = strip_namespace(root.tag)
    collected: dict[str, str] = {}

    if root_type == "sitemapindex":
        for sitemap_node in root:
            if (
                strip_namespace(
                    sitemap_node.tag
                )
                != "sitemap"
            ):
                continue

            child_url = ""

            for child in sitemap_node:
                if (
                    strip_namespace(child.tag)
                    == "loc"
                    and child.text
                ):
                    child_url = child.text.strip()
                    break

            if child_url:
                child_results = (
                    collect_sitemap_urls(
                        child_url,
                        visited=visited,
                        depth=depth + 1,
                    )
                )

                collected.update(
                    child_results
                )

        return collected

    if root_type != "urlset":
        raise RuntimeError(
            f"不支持的 Sitemap 类型："
            f"{root_type}"
        )

    for url_node in root:
        if (
            strip_namespace(url_node.tag)
            != "url"
        ):
            continue

        location = ""
        last_modified = ""

        for child in url_node:
            child_type = strip_namespace(
                child.tag
            )

            if (
                child_type == "loc"
                and child.text
            ):
                location = child.text.strip()

            elif (
                child_type == "lastmod"
                and child.text
            ):
                last_modified = (
                    child.text.strip()
                )

        if location:
            collected[location] = last_modified

    return collected


def filter_site_urls(
    site: SiteConfig,
    urls: dict[str, str],
) -> dict[str, str]:
    valid: dict[str, str] = {}
    rejected: list[str] = []

    for url, lastmod in urls.items():
        parsed = urlparse(url)

        if (
            parsed.scheme == "https"
            and parsed.hostname == site.host
        ):
            valid[url] = lastmod
        else:
            rejected.append(url)

    if rejected:
        log(
            f"{site.name} 忽略 "
            f"{len(rejected)} 个主机不一致网址"
        )

        for url in rejected[:5]:
            log(f"忽略：{url}")

    if not valid:
        raise RuntimeError(
            f"{site.name} 没有找到属于 "
            f"{site.host} 的有效 URL"
        )

    return dict(sorted(valid.items()))


def load_state() -> dict[str, Any]:
    state = load_json_file(
        STATE_FILE,
        {
            "version": 2,
            "sites": {},
        },
    )

    if not isinstance(state, dict):
        state = {}

    state.setdefault("version", 2)
    state.setdefault("sites", {})

    if not isinstance(state["sites"], dict):
        state["sites"] = {}

    return state


def detect_changes(
    current: dict[str, str],
    previous: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    new_urls: list[str] = []
    updated_urls: list[str] = []

    for url, current_lastmod in current.items():
        if url not in previous:
            new_urls.append(url)
            continue

        previous_lastmod = str(
            previous.get(url, "")
        )

        if (
            current_lastmod
            and current_lastmod
            != previous_lastmod
        ):
            updated_urls.append(url)

    removed_urls = list(
        set(previous) - set(current)
    )

    return (
        sorted(new_urls),
        sorted(updated_urls),
        sorted(removed_urls),
    )


def chunks(
    items: list[str],
    size: int,
) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def verify_key_file(
    site: SiteConfig,
    api_key: str,
) -> None:
    content = (
        http_get(site.key_location)
        .decode("utf-8", errors="replace")
        .strip()
    )

    if content != api_key:
        raise RuntimeError(
            f"{site.name} Key 文件内容与 "
            f"GitHub Secret 不一致"
        )

    log(f"{site.name} Key 文件验证正常")


def submit_batch(
    site: SiteConfig,
    api_key: str,
    url_list: list[str],
) -> int:
    payload = {
        "host": site.host,
        "key": api_key,
        "keyLocation": site.key_location,
        "urlList": url_list,
    }

    request_data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            INDEXNOW_ENDPOINT,
            data=request_data,
            headers={
                "Content-Type": (
                    "application/json; "
                    "charset=utf-8"
                ),
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status

                response_text = (
                    response.read()
                    .decode(
                        "utf-8",
                        errors="replace",
                    )
                    .strip()
                )

            if status not in (200, 202):
                raise RuntimeError(
                    f"IndexNow HTTP {status}："
                    f"{response_text}"
                )

            log(
                f"{site.name} 提交成功："
                f"HTTP {status}，"
                f"{len(url_list)} 个 URL"
            )

            return status

        except urllib.error.HTTPError as error:
            error_body = (
                error.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
                .strip()
            )

            last_error = RuntimeError(
                f"HTTP {error.code}："
                f"{error_body or '无回应内容'}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
        ) as error:
            last_error = error

        log(
            f"{site.name} 第 {attempt} 次"
            f"提交失败：{last_error}"
        )

        retryable = True

        if isinstance(
            last_error,
            RuntimeError,
        ):
            text = str(last_error)

            if (
                "HTTP 400" in text
                or "HTTP 403" in text
                or "HTTP 422" in text
            ):
                retryable = False

        if (
            not retryable
            or attempt >= MAX_RETRIES
        ):
            break

        wait_seconds = min(
            10 * (2 ** (attempt - 1)),
            60,
        )

        log(
            f"{wait_seconds} 秒后重新提交"
        )
        time.sleep(wait_seconds)

    raise RuntimeError(
        f"{site.name} IndexNow 提交失败："
        f"{last_error}"
    )


def create_url_digest(
    urls: dict[str, str],
) -> str:
    payload = json.dumps(
        urls,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(
        payload
    ).hexdigest()


def process_site(
    site: SiteConfig,
    state: dict[str, Any],
    force_all: bool,
) -> dict[str, Any]:
    log("=" * 60)
    log(f"开始处理：{site.name}")

    api_key = os.environ.get(
        site.key_env,
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            f"找不到 GitHub Secret："
            f"{site.key_env}"
        )

    verify_key_file(site, api_key)

    discovered_urls = (
        collect_sitemap_urls(
            site.sitemap
        )
    )

    current_urls = filter_site_urls(
        site,
        discovered_urls,
    )

    site_states = state["sites"]
    previous_site_state = site_states.get(
        site.name,
        {},
    )

    previous_urls = previous_site_state.get(
        "urls",
        {},
    )

    if not isinstance(previous_urls, dict):
        previous_urls = {}

    new_urls, updated_urls, removed_urls = (
        detect_changes(
            current_urls,
            previous_urls,
        )
    )

    first_run = not bool(previous_urls)

    if force_all or first_run:
        urls_to_submit = sorted(
            current_urls
        )
    else:
        urls_to_submit = sorted(
            set(
                new_urls
                + updated_urls
                + removed_urls
            )
        )

    log(
        f"{site.name} 有效 URL："
        f"{len(current_urls)}"
    )
    log(
        f"新增：{len(new_urls)}；"
        f"更新：{len(updated_urls)}；"
        f"删除：{len(removed_urls)}"
    )

    if force_all:
        log("本次使用强制全站提交模式")
    elif first_run:
        log("首次执行，提交全部 URL")

    statuses: list[int] = []

    for batch in chunks(
        urls_to_submit,
        MAX_BATCH_SIZE,
    ):
        statuses.append(
            submit_batch(
                site,
                api_key,
                batch,
            )
        )

    if not urls_to_submit:
        log(
            f"{site.name} 没有发现变化，"
            "不提交 IndexNow"
        )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    site_states[site.name] = {
        "host": site.host,
        "sitemap": site.sitemap,
        "keyLocation": site.key_location,
        "checkedAt": now,
        "lastSuccessfulRun": now,
        "urlCount": len(current_urls),
        "urlDigest": create_url_digest(
            current_urls
        ),
        "submittedCount": len(
            urls_to_submit
        ),
        "submittedUrls": urls_to_submit,
        "newCount": len(new_urls),
        "updatedCount": len(updated_urls),
        "removedCount": len(removed_urls),
        "responseStatuses": statuses,
        "urls": current_urls,
    }

    return {
        "site": site.name,
        "success": True,
        "submitted": len(urls_to_submit),
        "new": len(new_urls),
        "updated": len(updated_urls),
        "removed": len(removed_urls),
    }


def save_state(
    state: dict[str, Any],
) -> None:
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    state["version"] = 2
    state["updatedAt"] = datetime.now(
        timezone.utc
    ).isoformat()

    temporary_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    with temporary_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")

    temporary_file.replace(STATE_FILE)


def env_is_true(name: str) -> bool:
    return os.environ.get(
        name,
        "",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def main() -> int:
    force_all = env_is_true(
        "FORCE_ALL"
    )

    sites = load_site_configs()
    state = load_state()

    results: list[dict[str, Any]] = []
    failures: list[str] = []

    for site in sites:
        if not site.enabled:
            log(f"跳过已停用网站：{site.name}")
            continue

        try:
            result = process_site(
                site,
                state,
                force_all,
            )

            results.append(result)

        except Exception as error:
            message = (
                f"{site.name} 处理失败：{error}"
            )

            log(message)
            failures.append(message)

    save_state(state)

    log("=" * 60)
    log("执行结果：")

    for result in results:
        log(
            f"{result['site']}：成功；"
            f"提交 {result['submitted']} 个 URL"
        )

    if failures:
        log("以下网站执行失败：")

        for failure in failures:
            log(failure)

        return 1

    log("所有网站处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
