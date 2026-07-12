from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urlparse


SITE_HOST = "www.pgsoft-game.net"

SITEMAP_URL = (
    "https://www.pgsoft-game.net/sitemap.xml"
)

KEY_LOCATION = (
    "https://www.pgsoft-game.net/"
    "b80d160a8383427f9955267e654eca74.txt"
)

INDEXNOW_ENDPOINT = (
    "https://api.indexnow.org/indexnow"
)

STATE_FILE = Path("data/indexnow_state.json")

USER_AGENT = (
    "GitHub-Actions-IndexNow/1.0 "
    "(+https://www.pgsoft-game.net)"
)

MAX_BATCH_SIZE = 10000
REQUEST_TIMEOUT = 45
MAX_RETRIES = 3


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Accept": (
                "application/xml,text/xml,"
                "text/plain,*/*"
            ),
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"无法读取 {url}，HTTP {response.status}"
            )

        return response.read()


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]

    return tag


def parse_sitemap(
    sitemap_url: str,
    visited: set[str] | None = None,
) -> Dict[str, str]:
    if visited is None:
        visited = set()

    if sitemap_url in visited:
        return {}

    visited.add(sitemap_url)

    print(f"读取 Sitemap：{sitemap_url}")

    xml_data = http_get(sitemap_url)

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as error:
        raise RuntimeError(
            f"Sitemap XML 格式错误：{error}"
        ) from error

    root_name = strip_namespace(root.tag)
    collected: Dict[str, str] = {}

    if root_name == "sitemapindex":
        for sitemap_node in root:
            if strip_namespace(
                sitemap_node.tag
            ) != "sitemap":
                continue

            child_location = ""

            for child in sitemap_node:
                if (
                    strip_namespace(child.tag) == "loc"
                    and child.text
                ):
                    child_location = child.text.strip()
                    break

            if child_location:
                child_urls = parse_sitemap(
                    child_location,
                    visited,
                )
                collected.update(child_urls)

        return collected

    if root_name != "urlset":
        raise RuntimeError(
            f"不支援的 Sitemap 类型：{root_name}"
        )

    for url_node in root:
        if strip_namespace(url_node.tag) != "url":
            continue

        location = ""
        last_modified = ""

        for child in url_node:
            child_name = strip_namespace(child.tag)

            if child_name == "loc" and child.text:
                location = child.text.strip()

            elif (
                child_name == "lastmod"
                and child.text
            ):
                last_modified = child.text.strip()

        if location:
            collected[location] = last_modified

    return collected


def validate_urls(
    sitemap_urls: Dict[str, str],
) -> Dict[str, str]:
    valid: Dict[str, str] = {}
    rejected: List[str] = []

    for url, last_modified in sitemap_urls.items():
        parsed = urlparse(url)

        if (
            parsed.scheme == "https"
            and parsed.hostname == SITE_HOST
        ):
            valid[url] = last_modified
        else:
            rejected.append(url)

    if rejected:
        print(
            f"忽略 {len(rejected)} 个主机不一致的网址："
        )

        for url in rejected[:10]:
            print(f"  - {url}")

    if not valid:
        raise RuntimeError(
            "没有找到属于 "
            f"{SITE_HOST} 的有效 HTTPS URL。"
            "请检查 Sitemap 是否使用 www。"
        )

    return valid


def load_state() -> Dict[str, str]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {}

        urls = data.get("urls", {})

        if not isinstance(urls, dict):
            return {}

        return {
            str(url): str(last_modified or "")
            for url, last_modified in urls.items()
        }

    except (
        json.JSONDecodeError,
        OSError,
        TypeError,
    ):
        return {}


def detect_changes(
    current: Dict[str, str],
    previous: Dict[str, str],
) -> Tuple[List[str], List[str], List[str]]:
    new_urls: List[str] = []
    updated_urls: List[str] = []

    for url, current_lastmod in current.items():
        if url not in previous:
            new_urls.append(url)
            continue

        previous_lastmod = previous.get(url, "")

        if (
            current_lastmod
            and current_lastmod != previous_lastmod
        ):
            updated_urls.append(url)

    removed_urls = sorted(
        set(previous) - set(current)
    )

    return (
        sorted(new_urls),
        sorted(updated_urls),
        removed_urls,
    )


def chunks(
    items: List[str],
    size: int,
) -> Iterable[List[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def submit_batch(
    api_key: str,
    url_list: List[str],
) -> int:
    payload = {
        "host": SITE_HOST,
        "key": api_key,
        "keyLocation": KEY_LOCATION,
        "urlList": url_list,
    }

    request_body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        INDEXNOW_ENDPOINT,
        data=request_body,
        headers={
            "Content-Type": (
                "application/json; charset=utf-8"
            ),
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                response_text = (
                    response.read()
                    .decode("utf-8", errors="replace")
                )

            print(
                f"IndexNow 回应：HTTP {status}；"
                f"本批 {len(url_list)} 个 URL"
            )

            if response_text:
                print(f"回应内容：{response_text}")

            if status not in (200, 202):
                raise RuntimeError(
                    f"IndexNow 返回 HTTP {status}"
                )

            return status

        except urllib.error.HTTPError as error:
            error_body = (
                error.read()
                .decode("utf-8", errors="replace")
            )

            last_error = RuntimeError(
                f"IndexNow HTTP {error.code}："
                f"{error_body or '无回应内容'}"
            )

            print(
                f"第 {attempt} 次提交失败："
                f"{last_error}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
        ) as error:
            last_error = error
            print(
                f"第 {attempt} 次提交失败：{error}"
            )

        if attempt < MAX_RETRIES:
            wait_seconds = attempt * 5
            print(
                f"{wait_seconds} 秒后重新尝试……"
            )
            time.sleep(wait_seconds)

    raise RuntimeError(
        f"IndexNow 提交失败：{last_error}"
    )


def save_state(
    current_urls: Dict[str, str],
    submitted_urls: List[str],
) -> None:
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    state_data = {
        "siteHost": SITE_HOST,
        "sitemap": SITEMAP_URL,
        "urls": current_urls,
        "lastSubmittedUrls": submitted_urls,
    }

    with STATE_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state_data,
            file,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

        file.write("\n")


def main() -> int:
    api_key = os.environ.get(
        "INDEXNOW_KEY",
        "",
    ).strip()

    if not api_key:
        print(
            "错误：找不到 INDEXNOW_KEY。"
            "请检查 GitHub Repository Secret。"
        )
        return 1

    current_urls = validate_urls(
        parse_sitemap(SITEMAP_URL)
    )

    previous_urls = load_state()

    (
        new_urls,
        updated_urls,
        removed_urls,
    ) = detect_changes(
        current_urls,
        previous_urls,
    )

    print(f"Sitemap 有效 URL：{len(current_urls)}")
    print(f"新增 URL：{len(new_urls)}")
    print(f"更新 URL：{len(updated_urls)}")
    print(f"移除 URL：{len(removed_urls)}")

    urls_to_submit = sorted(
        set(
            new_urls
            + updated_urls
            + removed_urls
        )
    )

    if not previous_urls:
        urls_to_submit = sorted(current_urls)

        print(
            "首次执行：提交 Sitemap 中全部 URL。"
        )

    if not urls_to_submit:
        print("没有发现新增、更新或删除的网址。")
        save_state(current_urls, [])
        return 0

    print(
        f"本次准备提交 {len(urls_to_submit)} 个 URL。"
    )

    for batch in chunks(
        urls_to_submit,
        MAX_BATCH_SIZE,
    ):
        submit_batch(api_key, batch)

    save_state(
        current_urls,
        urls_to_submit,
    )

    print("IndexNow 提交与状态保存完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
