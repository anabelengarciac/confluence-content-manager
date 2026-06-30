#!/usr/bin/env python3
"""Confluence helper for auditing and standardizing documentation pages."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import escape, unescape
from pathlib import Path
from typing import Any, Iterable
from urllib import error, parse, request


DEFAULT_TOKEN_FILE = Path("~/.config/confluence/api.token")
DEFAULT_ENV_FILE = Path("~/.config/confluence/.env")
DEFAULT_SPACE_NAME = "Analytics Documentation Team"
DEFAULT_HOME_TITLE = "Analytics Home"
MAX_PAGE_FETCH = 250
FULL_SPACE_FETCH_LIMIT = 2000

REQUIRED_SECTIONS = (
    "overview",
    "purpose",
    "details / analysis",
    "related links",
)

DATA_DOMAIN_PATH_HINTS = (
    "Analytics Portal",
    "Execution Phase",
    "Data Documentation",
    "Data Documentation",
)
DATA_DOMAIN_INCLUDED_NODE = "data documentation"
DATA_DOMAIN_EXCLUDED_NODE = "integration documentation"

DATA_DOMAIN_REQUIRED_HEADINGS = (
    "1. overview",
    "2. scope",
    "3. business use",
    "4. kpis",
    "5. grain & keys",
    "6. joins",
    "7. important fields",
    "8. data logic (raw + silver + gold)",
    "9. refresh",
    "10. notes / limitations",
)

INTENTIONAL_DUPLICATE_TITLE_PAIRS = {
    frozenset(("Discovery Phase - Project Charter", "Analytics Portal - Project Charter")),
    frozenset((
        "Discovery Phase - User Requirement Specifications (URS)",
        "Analytics Portal - User Requirement Specifications (URS)",
    )),
    frozenset((
        "Discovery Phase - Functional Requirement Specifications (FRS)",
        "Analytics Portal - Functional Requirement Specifications (FRS)",
    )),
    frozenset(("Discovery Phase - Architectural Design", "Analytics Portal - Architectural Design")),
    frozenset(("Discovery Phase - Minimum Viable Product (MVP)", "Analytics Portal - Minimum Viable Product (MVP)")),
    frozenset(("Discovery Phase - User Acceptance Test (UAT)", "Analytics Portal - User Acceptance Test (UAT)")),
    frozenset(("Data Framework", "L0 to L1 Data Framework")),
    frozenset(("Data Layers", "Data Layers Overview")),
    frozenset(("Starcom Data", "Starcom Data Raw")),
    frozenset(("Search Interest Data", "Search Interest Data Raw")),
    frozenset(("Web - AI Data", "Profound Web Performance AI Raw")),
    frozenset(("CI - Consumer Segmentation POC", "Consumer Insights")),
}

INTENTIONAL_DETAIL_SUFFIXES = (" raw", " overview")
INTENTIONAL_VARIANT_CODES = {"ci", "mi", "bi", "se", "de", "atl", "btl"}


class ConfluenceError(RuntimeError):
    """Raised when the Confluence API returns an error or invalid data."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit, create, and update Confluence pages for documentation hygiene."
    )
    parser.add_argument(
        "--base-url",
        help="Tenant URL, for example https://company.atlassian.net. Defaults to CONFLUENCE_BASE_URL from the Confluence .env file.",
    )
    parser.add_argument("--cloud-id", help="Atlassian cloud ID for gateway API usage")
    parser.add_argument(
        "--auth-mode",
        choices=("auto", "bearer", "basic"),
        default="auto",
        help="Authentication mode. Auto uses basic when --email is provided; otherwise bearer.",
    )
    parser.add_argument(
        "--email",
        help="Email for basic auth. Defaults to CONFLUENCE_EMAIL from the Confluence .env file.",
    )
    parser.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help="Path to file containing a Confluence token or API token",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format for list and audit operations",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_spaces = subparsers.add_parser("list-spaces", help="List available spaces")
    list_spaces.add_argument("--limit", type=int, default=100)

    audit = subparsers.add_parser("audit-space", help="Audit pages in a Confluence space")
    audit.add_argument("--space", default=DEFAULT_SPACE_NAME, help="Space name or key")
    audit.add_argument("--limit", type=int, default=MAX_PAGE_FETCH)
    audit.add_argument("--stale-days", type=int, default=120)
    audit.add_argument("--min-words", type=int, default=80)
    audit.add_argument("--max-paragraph-words", type=int, default=120)
    audit.add_argument(
        "--verbose",
        action="store_true",
        help="Include the full issue-by-issue detail instead of only the default executive summary.",
    )

    render = subparsers.add_parser("render-template", help="Render the standard page body")
    add_page_content_arguments(render)

    create_page = subparsers.add_parser("create-page", help="Create a new Confluence page")
    create_page.add_argument("--space", default=DEFAULT_SPACE_NAME, help="Space name or key")
    create_page.add_argument("--parent-id", required=True, help="Parent page ID")
    add_page_content_arguments(create_page)

    update_page = subparsers.add_parser("update-page", help="Update an existing Confluence page")
    update_page.add_argument("--page-id", required=True, help="Page ID")
    add_page_content_arguments(update_page)

    fix_docs = subparsers.add_parser(
        "fix-docs",
        aliases=["standardize-data-domains"],
        help="Fix and standardize Data Documentation documentation under Analytics Portal > Execution Phase > Data Documentation > Data Documentation",
    )
    fix_docs.set_defaults(command="fix-docs")
    fix_docs.add_argument("--space", default=DEFAULT_SPACE_NAME, help="Space name or key")
    fix_docs.add_argument(
        "--page",
        help="Optional page title filter inside Data Documentation. Useful for previewing or fixing a single page.",
    )
    fix_docs.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the changes without updating Confluence pages.",
    )

    return parser.parse_args()


def add_page_content_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", required=True, help="Page title")
    parser.add_argument("--overview", required=True, help="Short overview section")
    parser.add_argument("--purpose", required=True, help="Purpose section")
    parser.add_argument(
        "--details",
        help="Main details section body, in markdown-ish plain text",
    )
    parser.add_argument(
        "--details-file",
        help="Path to a markdown/plaintext file used for the Details / Analysis section",
    )
    parser.add_argument(
        "--insight",
        action="append",
        default=[],
        help="Add a bullet to Key Insights. Repeat as needed.",
    )
    parser.add_argument(
        "--related-link",
        action="append",
        default=[],
        help="Add a related link as Label|URL or URL",
    )


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def resolved_setting(cli_value: str | None, env_key: str, file_values: dict[str, str]) -> str | None:
    if cli_value:
        return cli_value
    if env_key in os.environ and os.environ[env_key]:
        return os.environ[env_key]
    return file_values.get(env_key)


def read_token(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ConfluenceError(f"Token file is empty: {path}")
    if "\n" not in raw:
        match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=(.+)$", raw)
        if match:
            raw = match.group(1).strip()
    return raw


def normalize_base_url(base_url: str | None) -> str:
    if not base_url:
        raise ConfluenceError("Missing --base-url. Provide the tenant URL explicitly.")
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/wiki"):
        return trimmed
    return f"{trimmed}/wiki"


def auth_headers(token: str, auth_mode: str, email: str | None) -> dict[str, str]:
    resolved = auth_mode
    if resolved == "auto":
        resolved = "basic" if email else "bearer"
    if resolved == "basic":
        if not email:
            raise ConfluenceError("Basic auth requires --email.")
        creds = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {creds}"}
    return {"Authorization": f"Bearer {token}"}


class ConfluenceClient:
    def __init__(
        self,
        *,
        base_url: str | None,
        cloud_id: str | None,
        token: str,
        auth_mode: str,
        email: str | None,
    ) -> None:
        self.base_url = normalize_base_url(base_url) if base_url else None
        self.cloud_id = cloud_id
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **auth_headers(token, auth_mode, email),
        }

    def _url(self, path: str, query: dict[str, Any] | None = None, api: str = "v2") -> str:
        if self.cloud_id:
            if api == "v2":
                root = f"https://api.atlassian.com/ex/confluence/{self.cloud_id}/wiki/api/v2"
            elif api == "v1":
                root = f"https://api.atlassian.com/ex/confluence/{self.cloud_id}/wiki/rest/api"
            else:
                raise ConfluenceError(f"Unsupported API family: {api}")
        else:
            if not self.base_url:
                raise ConfluenceError("Missing --base-url or --cloud-id.")
            if api == "v2":
                root = f"{self.base_url}/api/v2"
            elif api == "v1":
                root = f"{self.base_url}/rest/api"
            else:
                raise ConfluenceError(f"Unsupported API family: {api}")

        url = f"{root}{path}"
        if query:
            query_pairs: dict[str, str] = {}
            for key, value in query.items():
                if value is None:
                    continue
                query_pairs[key] = str(value)
            if query_pairs:
                url = f"{url}?{parse.urlencode(query_pairs)}"
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        api: str = "v2",
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._url(path, query=query, api=api),
            data=body,
            method=method,
            headers=self.headers,
        )
        try:
            with request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise ConfluenceError(f"{method} {path} failed ({exc.code}): {raw}") from exc
        except error.URLError as exc:
            raise ConfluenceError(f"Unable to reach Confluence: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfluenceError(f"Invalid JSON response from {path}") from exc

    def request_url(self, method: str, url: str) -> dict[str, Any]:
        req = request.Request(url, method=method, headers=self.headers)
        try:
            with request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise ConfluenceError(f"{method} {url} failed ({exc.code}): {raw}") from exc
        except error.URLError as exc:
            raise ConfluenceError(f"Unable to reach Confluence: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfluenceError(f"Invalid JSON response from {url}") from exc

    def _absolute_next_url(self, next_link: str) -> str:
        if next_link.startswith("http://") or next_link.startswith("https://"):
            return next_link
        if self.cloud_id:
            return f"https://api.atlassian.com{next_link}"
        if not self.base_url:
            raise ConfluenceError("Missing base URL for pagination.")
        base_root = self.base_url.removesuffix("/wiki")
        return f"{base_root}{next_link}"

    def paginated_v2(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        data = self.request("GET", path, query=query, api="v2")
        results = list(data.get("results", []))

        while True:
            if max_items is not None and len(results) >= max_items:
                return results[:max_items]
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                return results[:max_items] if max_items is not None else results
            data = self.request_url("GET", self._absolute_next_url(next_link))
            results.extend(data.get("results", []))

    def list_spaces(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.paginated_v2("/spaces", query={"limit": min(limit, 250)}, max_items=limit)

    def find_space(self, identifier: str) -> dict[str, Any]:
        spaces = self.paginated_v2("/spaces", query={"limit": 250})
        normalized = identifier.casefold().strip()
        for space in spaces:
            if str(space.get("id", "")).strip() == identifier:
                return space
            if str(space.get("key", "")).casefold() == normalized:
                return space
            if str(space.get("name", "")).casefold() == normalized:
                return space
        for space in spaces:
            name = str(space.get("name", "")).casefold()
            key = str(space.get("key", "")).casefold()
            if normalized in name or normalized in key:
                return space
        raise ConfluenceError(f"Space not found: {identifier}")

    def list_pages(self, space_id: str, limit: int) -> list[dict[str, Any]]:
        return self.paginated_v2(
            "/pages",
            query={
                "space-id": space_id,
                "limit": min(limit, 250),
                "sort": "modified-date",
                "body-format": "storage",
            },
            max_items=limit,
        )

    def search_content(
        self,
        cql: str,
        *,
        limit: int = 100,
        expand: str = "body.storage,version,ancestors",
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self.request(
                "GET",
                "/content/search",
                api="v1",
                query={
                    "cql": cql,
                    "limit": min(limit, 100),
                    "start": start,
                    "expand": expand,
                },
            )
            chunk = list(data.get("results", []))
            results.extend(chunk)
            if len(chunk) < min(limit, 100):
                return results
            start += len(chunk)
            if len(results) >= limit:
                return results[:limit]

    def get_page(self, page_id: str, *, expand: str = "version,space,body.storage") -> dict[str, Any]:
        return self.request(
            "GET",
            f"/content/{page_id}",
            api="v1",
            query={"expand": expand},
        )

    def create_page(self, *, space_key: str, parent_id: str, title: str, body: str) -> dict[str, Any]:
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": str(parent_id)}],
            "body": {
                "storage": {
                    "value": body,
                    "representation": "storage",
                }
            },
        }
        return self.request("POST", "/content", api="v1", payload=payload)

    def update_page(self, *, page_id: str, title: str, body: str) -> dict[str, Any]:
        current = self.get_page(page_id)
        # Business rule: Confluence updates must preserve version history so owners can audit
        # what changed and roll back documentation if a standardization pass goes too far.
        version_number = int(current["version"]["number"]) + 1
        payload = {
            "id": str(page_id),
            "type": "page",
            "title": title,
            "version": {"number": version_number},
            "body": {
                "storage": {
                    "value": body,
                    "representation": "storage",
                }
            },
        }
        return self.request("PUT", f"/content/{page_id}", api="v1", payload=payload)


def parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?>.*?</\1>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h1|h2|h3|h4|h5|h6)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_headings(raw_html: str) -> list[str]:
    headings = re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", raw_html, flags=re.I | re.S)
    return [strip_tags(item).casefold() for item in headings]


def markdown_to_storage(text: str) -> str:
    lines = text.splitlines()
    blocks: list[str] = []
    bullets: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{escape(' '.join(part.strip() for part in paragraph if part.strip()))}</p>")
            paragraph = []

    def flush_bullets() -> None:
        nonlocal bullets
        if bullets:
            items = "".join(f"<li>{escape(item)}</li>" for item in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            flush_bullets()
            blocks.append(f"<h3>{escape(stripped[4:].strip())}</h3>")
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            flush_bullets()
            blocks.append(f"<h2>{escape(stripped[3:].strip())}</h2>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            bullets.append(stripped[2:].strip())
            continue
        flush_bullets()
        paragraph.append(stripped)

    flush_paragraph()
    flush_bullets()
    return "\n".join(blocks)


def details_content(args: argparse.Namespace) -> str:
    if args.details_file:
        return Path(args.details_file).read_text(encoding="utf-8").strip()
    if args.details:
        return args.details.strip()
    return ""


def parse_related_links(entries: Iterable[str]) -> list[tuple[str, str]]:
    parsed_links: list[tuple[str, str]] = []
    for entry in entries:
        if "|" in entry:
            label, url = entry.split("|", 1)
            parsed_links.append((label.strip(), url.strip()))
        else:
            parsed_links.append((entry.strip(), entry.strip()))
    return parsed_links


def render_page_body(args: argparse.Namespace) -> str:
    details = details_content(args)
    sections = [
        "<h1>Overview</h1>",
        f"<p>{escape(args.overview.strip())}</p>",
        "<h1>Purpose</h1>",
        f"<p>{escape(args.purpose.strip())}</p>",
        "<h1>Details / Analysis</h1>",
        markdown_to_storage(details) if details else "<p>TBD</p>",
    ]
    if args.insight:
        insight_items = "".join(f"<li>{escape(item.strip())}</li>" for item in args.insight if item.strip())
        if insight_items:
            sections.extend(["<h1>Key Insights</h1>", f"<ul>{insight_items}</ul>"])
    links = parse_related_links(args.related_link)
    sections.append("<h1>Related Links</h1>")
    if links:
        link_items = "".join(
            f'<li><a href="{escape(url, quote=True)}">{escape(label)}</a></li>'
            for label, url in links
        )
        sections.append(f"<ul>{link_items}</ul>")
    else:
        sections.append("<p>No related links yet.</p>")
    return "\n".join(sections)


def html_to_lines(raw_html: str) -> list[str]:
    text = raw_html
    replacements = (
        ("<br/>", "\n"),
        ("<br />", "\n"),
        ("<br>", "\n"),
        ("</p>", "\n"),
        ("</li>", "\n"),
        ("</tr>", "\n"),
        ("</h1>", "\n"),
        ("</h2>", "\n"),
        ("</h3>", "\n"),
        ("</h4>", "\n"),
        ("</h5>", "\n"),
        ("</h6>", "\n"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t-*•")
        if line:
            lines.append(line)
    return lines


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.casefold()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def ancestor_titles(page: dict[str, Any]) -> list[str]:
    return [str(item.get("title", "")).strip() for item in page.get("ancestors", []) if str(item.get("title", "")).strip()]


def path_contains(page: dict[str, Any], title: str) -> bool:
    target = title.casefold()
    return any(item.casefold() == target for item in ancestor_titles(page))


def best_effort_value(value: str | None, fallback: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if cleaned else fallback


def list_to_bullets(items: list[str], fallback: str, limit: int = 3) -> str:
    selected = dedupe_preserve_order([item.strip() for item in items if item.strip()])[:limit]
    if not selected:
        selected = [fallback]
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in selected) + "</ul>"


def render_html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        padded = row + [""] * max(0, len(headers) - len(row))
        body_rows.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in padded[: len(headers)]) + "</tr>")
    return "<table><tbody><tr>" + head + "</tr>" + "".join(body_rows) + "</tbody></table>"


@dataclass
class SchemaField:
    name: str
    type_name: str = ""
    description: str = ""


@dataclass
class DataDomainSignals:
    profile: str
    source_label: str
    model_kind: str = "dataset"
    source_tables: list[str] = field(default_factory=list)
    schema_fields: list[SchemaField] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    key_fields: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    business_uses: list[str] = field(default_factory=list)
    kpis: list[list[str]] = field(default_factory=list)
    grain: str = ""
    grain_notes: list[str] = field(default_factory=list)
    joins: dict[str, str] = field(default_factory=dict)
    important_fields: list[list[str]] = field(default_factory=list)
    logic_points: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    domain: str = ""
    geography: str = ""
    time_range: str = ""
    refresh_frequency: str = ""
    refresh_type: str = ""
    model_complexity_reasons: list[str] = field(default_factory=list)
    preserved_context_html: str = ""


METRIC_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("Impressions", ("impression",)),
    ("Clicks", ("click",)),
    ("Spend / Cost", ("cost", "spend")),
    ("Engagements", ("engagement",)),
    ("Video Starts", ("video_start", "video start")),
    ("Video Completes", ("video_complete", "video complete")),
    ("Add to Carts", ("add_to_cart", "add to cart")),
    ("Search Volume", ("search volume", "volume")),
    ("Share of Search", ("share of search", "percentage")),
    ("Cases", ("case",)),
    ("Response Time", ("response", "hours_to_first_respond", "response time")),
    ("Sentiment", ("sentiment",)),
    ("Satisfaction", ("satisfaction",)),
    ("Visibility Score", ("visibility",)),
    ("Sessions", ("session",)),
    ("Users", ("user", "active user")),
    ("Conversions", ("conversion",)),
]


DIMENSION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("Date", ("date", "month", "week", "year")),
    ("Brand", ("brand",)),
    ("Product", ("product", "sku", "item", "family")),
    ("Campaign", ("campaign",)),
    ("Channel", ("channel", "medium")),
    ("Placement", ("placement", "site", "creative")),
    ("Audience", ("audience", "segment")),
    ("Country", ("country", "iso", "geograph", "market")),
    ("Category", ("category", "tracker")),
    ("Topic", ("topic",)),
    ("Case", ("case number", "case_number", "case id")),
]


PROFILE_ALLOWED_METRICS: dict[str, set[str]] = {
    "paid_media": {"Impressions", "Clicks", "Spend / Cost", "Engagements", "Video Starts", "Video Completes", "Add to Carts", "Conversions"},
    "owned_crm": {"Cases", "Response Time", "Sentiment", "Satisfaction", "Users"},
    "search_trends": {"Search Volume", "Share of Search", "Visibility Score"},
    "web_behavior": {"Sessions", "Users", "Conversions", "Engagements"},
    "other": set(label for label, _ in METRIC_PATTERNS),
}


PROFILE_ALLOWED_DIMENSIONS: dict[str, set[str]] = {
    "paid_media": {"Date", "Brand", "Product", "Campaign", "Channel", "Placement", "Audience", "Country"},
    "owned_crm": {"Date", "Brand", "Product", "Country", "Topic", "Case"},
    "search_trends": {"Date", "Brand", "Product", "Country", "Category"},
    "web_behavior": {"Date", "Brand", "Product", "Country", "Channel"},
    "other": set(label for label, _ in DIMENSION_PATTERNS),
}


PROFILE_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "paid_media": {
        "strong": ("paid media", "campaign", "impressions", "clicks", "media_cost", "media cost", "placement", "starcom", "google ads", "meta", "skai", "redmill"),
        "schema": ("impression", "click", "cost", "spend", "placement", "campaign", "audience", "creative", "channel", "video"),
    },
    "owned_crm": {
        "strong": ("salesforce service cloud", "consumer response", "case", "topic assignment", "consumer interaction", "service cloud", "response time"),
        "schema": ("case", "topic", "response", "sentiment", "satisfaction", "consumer", "account", "contact"),
    },
    "search_trends": {
        "strong": ("search interest", "share of search", "search volume", "competitor", "visibility", "trend", "search demand"),
        "schema": ("tracker", "volume", "percentage", "competitor", "search", "visibility", "category"),
    },
    "web_behavior": {
        "strong": ("ga4", "pageview", "page view", "session", "user", "conversion", "web traffic", "website"),
        "schema": ("session", "user", "page", "conversion", "traffic", "visit", "source", "medium"),
    },
}


JOIN_PRIORITY_BY_PROFILE: dict[str, dict[str, tuple[str, ...]]] = {
    "paid_media": {
        "Date": ("performance_date", "date", "reporting_date", "placement_start_date", "placement_end_date", "month"),
        "Brand": ("brand_family", "brand", "product_code", "product_group", "product"),
        "Product": ("product_code", "product_group", "product", "sku", "item"),
        "Geography": ("country_code", "country", "market", "region", "geo"),
    },
    "owned_crm": {
        "Date": ("performance_date", "case_created_date", "case_date", "date"),
        "Brand": ("brand_family", "short_brand", "brand"),
        "Product": ("product_group", "product", "sku"),
        "Geography": ("country_code", "country", "iso"),
    },
    "search_trends": {
        "Date": ("performance_date", "date", "month"),
        "Brand": ("brand_family", "brand", "tracker"),
        "Product": ("product_group", "product", "category"),
        "Geography": ("country_code", "country", "iso", "market"),
    },
    "web_behavior": {
        "Date": ("performance_date", "date", "session_date", "event_date"),
        "Brand": ("brand_family", "brand"),
        "Product": ("product_group", "product", "sku"),
        "Geography": ("country_code", "country", "market", "region"),
    },
    "other": {
        "Date": ("performance_date", "date", "month", "week"),
        "Brand": ("brand_family", "short_brand", "brand"),
        "Product": ("product_group", "product_code", "product", "sku"),
        "Geography": ("country_code", "iso", "country", "market", "region"),
    },
}


COMPLEX_MODEL_KPI_HINTS: dict[str, tuple[str, ...]] = {
    "ROAS": ("roas", "return_on_ad_spend", "return on ad spend"),
    "CPA": ("cpa", "cost_per_acquisition", "cost per acquisition"),
    "CPC": ("cpc", "cost_per_click", "cost per click"),
    "CPM": ("cpm", "cost_per_mille", "cost per mille"),
    "Cost Reach": ("cost_reach", "cost reach", "cost_per_reach"),
    "Normalized Spend": ("normalized_spend", "inflation_adjusted_spend", "fx_adjusted_spend"),
}


ANALYTICAL_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "FX-adjusted spend": ("fx", "exchange_rate", "exchange rate", "local_currency", "usd"),
    "Inflation-adjusted spend": ("inflation", "inflation_multiplier", "real_terms"),
    "Normalized spend output": ("normalized_spend", "adj_spend", "final_spend", "spend_normalized"),
    "Product hierarchy": ("brand_family", "product_group", "hierarchy", "category", "sub_category"),
    "Derived efficiency output": ("roas", "cpa", "cpc", "cpm", "cost_reach"),
}


BLOCKED_SCHEMA_FIELD_NAMES = {
    "page", "field", "dimension", "kpi", "logic", "description", "overview",
    "notes", "grain", "refresh", "purpose", "details",
}


def infer_data_domain(title: str, combined_text: str) -> str:
    checks = (
        ("marketing", ("marketing", "media", "starcom", "skai", "search", "paid", "havas", "profitero")),
        ("consumer", ("consumer", "crm", "mfour", "answer rocket", "groundsignal", "consumer response")),
        ("sales", ("sales", "sell in", "deplet", "nabca")),
        ("finance", ("finance", "p&l", "pricing")),
        ("web", ("web", "ga4", "google analytics", "site", "traffic")),
    )
    haystack = f"{title} {combined_text}".casefold()
    for label, keywords in checks:
        if any(keyword in haystack for keyword in keywords):
            return label.capitalize()
    return "Not specified in current page"


def infer_geography(lines: list[str]) -> str:
    text = " ".join(lines).casefold()
    for label, keywords in (
        ("Global", ("global", "worldwide")),
        ("US", ("us ", "usa", "united states", "open states", "control states")),
        ("Region", ("region", "regional", "emea", "apac", "latam")),
    ):
        if any(keyword in text for keyword in keywords):
            return label
    return "Not specified in current page"


def infer_frequency(lines: list[str]) -> str:
    text = " ".join(lines).casefold()
    for label in ("daily", "weekly", "monthly", "hourly"):
        if label in text:
            return label.capitalize()
    return "Not specified in current page"


def infer_refresh_type(lines: list[str]) -> str:
    text = " ".join(lines).casefold()
    if "incremental" in text:
        return "Incremental"
    if "full" in text:
        return "Full"
    return "Not specified in current page"


def infer_time_range(lines: list[str], frequency: str) -> str:
    text = " ".join(lines)
    date_matches = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if len(date_matches) >= 2:
        return f"{date_matches[0]} onward, refresh {frequency.lower()}"
    if frequency != "Not specified in current page":
        return f"Not specified in current page, refresh {frequency.lower()}"
    return "Not specified in current page"


def infer_source_label(title: str, lines: list[str] | None = None) -> str:
    known_sources = (
        ("salesforce service cloud", "Salesforce Service Cloud"),
        ("google ads", "Google Ads"),
        ("starcom", "Starcom"),
        ("redmill", "Redmill"),
        ("profound", "Profound"),
        ("search interest", "Search Interest"),
        ("ga4", "GA4"),
    )
    title_cleaned = re.sub(r"\b(data|raw|overview)\b", "", title, flags=re.I)
    title_cleaned = re.sub(r"\s+", " ", title_cleaned).strip(" -")

    title_haystack = title.casefold()
    for needle, label in known_sources:
        if needle in title_haystack:
            return label

    if lines:
        joined = " ".join(lines)
        joined_lower = joined.casefold()
        for needle, label in known_sources:
            if needle in joined_lower:
                return label
        patterns = (
            r"data from ([A-Z][A-Za-z0-9 &/_-]{1,80}?)(?:,|\.| and | used for )",
            r"source(?: system| platform)?[: ]+([A-Z][A-Za-z0-9 &/_-]{1,80}?)(?:,|\.| and | table|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, joined, flags=re.I)
            if match:
                return match.group(1).strip(" .:-")

    return title_cleaned or title


def extract_source_table(lines: list[str]) -> str:
    pattern = re.compile(r"\b[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(0)
    return "Not specified in current page"


def extract_main_questions(title: str, lines: list[str]) -> list[str]:
    explicit = [line for line in lines if "?" in line]
    if explicit:
        return explicit[:3]
    return [
        f"What does {title} show?",
        f"How is {title} used in analytics and reporting?",
        f"What business decisions depend on {title}?",
    ]


def extract_business_use(title: str, lines: list[str]) -> list[str]:
    candidates = [
        line for line in lines
        if any(token in line.casefold() for token in ("used", "use case", "supports", "helps", "report", "dashboard"))
    ]
    if candidates:
        return candidates[:3]
    return [
        f"Analytics and reporting for {title}",
        f"Business review and decision support for {title}",
        "Ad hoc investigation and documentation reference",
    ]


def extract_logic_lines(lines: list[str]) -> list[str]:
    candidates = [
        line for line in lines
        if any(token in line.casefold() for token in ("logic", "clean", "standard", "upper", "null", "raw", "silver", "gold"))
    ]
    return candidates[:4]


def extract_notes(lines: list[str]) -> list[str]:
    candidates = [
        line for line in lines
        if any(token in line.casefold() for token in ("note", "limit", "limitation", "warning", "pending", "wip", "not ready"))
    ]
    return candidates[:5]


def extract_field_names(raw_html: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", raw_html)
    blocked = {
        "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "DATE", "TABLE", "COUNT",
        "DISTINCT", "NULL", "TRUE", "FALSE", "WITH", "JOIN", "LEFT", "RIGHT",
    }
    result = [candidate for candidate in candidates if candidate not in blocked]
    return dedupe_preserve_order(result)[:8]


def extract_join_fields(lines: list[str]) -> dict[str, str]:
    lookup = {
        "Date": ("date", "day", "week", "month"),
        "Brand": ("brand",),
        "Product": ("product", "sku", "item"),
        "Geography": ("country", "market", "region", "geograph"),
    }
    joins: dict[str, str] = {}
    for dimension, keywords in lookup.items():
        found = "Not clearly documented in current page"
        for line in lines:
            lowered = line.casefold()
            if any(keyword in lowered for keyword in keywords):
                found = line
                break
        joins[dimension] = found
    return joins


def humanize_token(value: str) -> str:
    cleaned = re.sub(r"[_\-]+", " ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned if cleaned.isupper() and len(cleaned) <= 6 else cleaned.title()


def sentence_list(values: list[str], *, fallback: str = "the documented business dimensions", limit: int = 4) -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    selected = dedupe_preserve_order(cleaned)[:limit]
    if not selected:
        return fallback
    if len(selected) == 1:
        return selected[0]
    if len(selected) == 2:
        return f"{selected[0]} and {selected[1]}"
    return ", ".join(selected[:-1]) + f", and {selected[-1]}"


def clean_unspecified(value: str, fallback: str = "Not specified") -> str:
    cleaned = value.strip()
    if not cleaned:
        return fallback
    normalized = cleaned.casefold()
    if normalized in {
        "not specified in current page",
        "not specified",
        "not clearly documented in current page",
        "not clearly documented",
    }:
        return fallback
    return cleaned


def collect_section_lines(
    lines: list[str],
    *,
    start_markers: tuple[str, ...],
    stop_markers: tuple[str, ...] = (),
    limit: int = 12,
) -> list[str]:
    captured: list[str] = []
    active = False
    for line in lines:
        lowered = compact_title(line)
        if any(marker in lowered for marker in start_markers):
            active = True
            continue
        if not active:
            continue
        if re.match(r"^\d+\.", line):
            break
        if stop_markers and any(marker in lowered for marker in stop_markers):
            break
        if len(line.split()) < 3:
            continue
        captured.append(line)
        if len(captured) >= limit:
            break
    return captured


def field_name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", name.casefold()) if token]


def has_field_token(name: str, *tokens: str) -> bool:
    parts = set(field_name_tokens(name))
    return any(token.casefold() in parts for token in tokens)


def field_matches_patterns(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.casefold()
    tokens = set(field_name_tokens(name))
    for pattern in patterns:
        pattern_lower = pattern.casefold()
        pattern_tokens = tuple(token for token in re.split(r"[^a-z0-9]+", pattern_lower) if token)
        if not pattern_tokens:
            continue
        if len(pattern_tokens) == 1:
            token = pattern_tokens[0]
            if token in tokens or any(part.startswith(token) for part in tokens):
                return True
        elif all(token in lowered for token in pattern_tokens):
            return True
    return False


def score_join_candidate(field_name: str, patterns: tuple[str, ...]) -> int:
    parts = set(field_name_tokens(field_name))
    score = 0
    for pattern in patterns:
        normalized = pattern.casefold()
        pattern_tokens = tuple(token for token in re.split(r"[^a-z0-9]+", normalized) if token)
        if field_name.casefold() == normalized:
            score += 16
        elif len(pattern_tokens) == 1 and pattern_tokens[0] in parts:
            score += 10
        elif len(pattern_tokens) > 1 and all(token in parts for token in pattern_tokens):
            score += 12
        elif any(part.startswith(normalized) for part in parts):
            score += 4
    penalty_tokens = {
        "ads", "ad", "impressions", "impression", "clicks", "click", "cost", "video",
        "engagements", "engagement", "visitors", "visitor", "fraudulent", "eligible",
        "measured", "monitored", "viewable", "out", "geo",
    }
    score -= sum(3 for token in penalty_tokens if token in parts)
    score -= sum(2 for token in ("start", "end", "placement", "suitable", "site") if token in parts)
    return score


def extract_html_tables(raw_html: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table_html in re.findall(r"<table\b[^>]*>(.*?)</table>", raw_html, flags=re.I | re.S):
        rows: list[list[str]] = []
        for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S):
            cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
            cleaned = [strip_tags(unescape(cell)).strip() for cell in cells]
            cleaned = [cell for cell in cleaned if cell]
            if cleaned:
                rows.append(cleaned)
        if rows:
            tables.append(rows)
    return tables


def parse_schema_fields(raw_html: str, lines: list[str]) -> list[SchemaField]:
    schema_fields: list[SchemaField] = []
    for table in extract_html_tables(raw_html):
        header = [compact_title(cell) for cell in table[0]]
        if not any("field" in cell or "column" in cell for cell in header):
            continue
        name_idx = next((idx for idx, cell in enumerate(header) if "field" in cell or "column" in cell), 0)
        type_idx = next((idx for idx, cell in enumerate(header) if "type" in cell), None)
        desc_idx = next(
            (
                idx for idx, cell in enumerate(header)
                if "description" in cell or "comment" in cell or "logic" in cell
            ),
            None,
        )
        for row in table[1:]:
            if name_idx >= len(row):
                continue
            name = row[name_idx].strip()
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]{1,}$", name):
                continue
            if name.casefold() in BLOCKED_SCHEMA_FIELD_NAMES:
                continue
            type_name = row[type_idx].strip() if type_idx is not None and type_idx < len(row) else ""
            description = row[desc_idx].strip() if desc_idx is not None and desc_idx < len(row) else ""
            schema_fields.append(SchemaField(name=name, type_name=type_name, description=description))

    if not schema_fields:
        for index, line in enumerate(lines):
            if re.match(r"^[A-Za-z][A-Za-z0-9_]{1,}$", line):
                if line.casefold() in BLOCKED_SCHEMA_FIELD_NAMES:
                    continue
                next_line = lines[index + 1] if index + 1 < len(lines) else ""
                if next_line.upper() in {"STRING", "INTEGER", "INT64", "FLOAT", "NUMERIC", "BOOLEAN", "DATE", "TIMESTAMP"}:
                    description = lines[index + 2] if index + 2 < len(lines) else ""
                    schema_fields.append(SchemaField(name=line, type_name=next_line, description=description))

    unique: dict[str, SchemaField] = {}
    for field_info in schema_fields:
        unique.setdefault(field_info.name.casefold(), field_info)
    return list(unique.values())


def parse_explicit_kpis(raw_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for table in extract_html_tables(raw_html):
        header = [compact_title(cell) for cell in table[0]]
        if not header or "kpi" not in " ".join(header):
            continue
        name_idx = next((idx for idx, cell in enumerate(header) if "kpi" in cell), 0)
        logic_idx = next((idx for idx, cell in enumerate(header) if "logic" in cell or "calculation" in cell), None)
        for row in table[1:]:
            if name_idx >= len(row):
                continue
            name = row[name_idx].strip()
            if not name:
                continue
            logic = row[logic_idx].strip() if logic_idx is not None and logic_idx < len(row) else ""
            rows.append([name, logic or "Logic documented in source page"])
    return rows[:8]


def normalize_join_dimension(value: str) -> str:
    lowered = compact_title(value)
    if any(token in lowered for token in ("date", "calendar", "time", "month", "week")):
        return "Date"
    if any(token in lowered for token in ("brand",)):
        return "Brand"
    if any(token in lowered for token in ("product", "sku", "item", "family")):
        return "Product"
    if any(token in lowered for token in ("country", "geography", "geo", "market", "region")):
        return "Geography"
    return ""


def clean_join_field_value(value: str, dimension: str, schema_fields: list[SchemaField]) -> str:
    schema_names = [field_info.name for field_info in schema_fields]
    exact_matches = [name for name in schema_names if name.casefold() in value.casefold()]
    if exact_matches:
        return dedupe_preserve_order(exact_matches)[-1]

    candidates = re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", value)
    schema_lookup = {name.casefold(): name for name in schema_names}
    schema_hits = [schema_lookup[token.casefold()] for token in candidates if token.casefold() in schema_lookup]
    if schema_hits:
        return dedupe_preserve_order(schema_hits)[-1]
    rich_tokens = [token for token in candidates if "_" in token or sum(1 for part in token.split("_") if part) >= 2]
    if rich_tokens:
        return rich_tokens[-1]

    cleaned = re.sub(r"\s+", " ", value).strip(" :-")
    if cleaned.casefold() in {"page", "field", "dimension", "not specified"}:
        return ""
    return cleaned


def is_placeholder_join_value(value: str) -> bool:
    normalized = clean_unspecified(value, "").casefold()
    return not normalized or normalized in {"page", "field", "dimension", "not specified"}


def parse_explicit_joins(raw_html: str, schema_fields: list[SchemaField]) -> dict[str, str]:
    for table in extract_html_tables(raw_html):
        header = [compact_title(cell) for cell in table[0]]
        if "dimension" not in " ".join(header) or "field" not in " ".join(header):
            continue
        dimension_idx = next((idx for idx, cell in enumerate(header) if "dimension" in cell), 0)
        field_idx = next((idx for idx, cell in enumerate(header) if "field" in cell), None)
        if field_idx is None:
            continue
        joins: dict[str, str] = {}
        for row in table[1:]:
            if dimension_idx >= len(row) or field_idx >= len(row):
                continue
            dimension = normalize_join_dimension(row[dimension_idx].strip())
            field_name = clean_join_field_value(row[field_idx].strip(), dimension, schema_fields)
            if not dimension or not field_name:
                continue
            joins[dimension] = field_name
        if joins and not all(is_placeholder_join_value(value) for value in joins.values()):
            return joins
    return {}


def parse_analytical_join_summary(lines: list[str], schema_fields: list[SchemaField]) -> dict[str, str]:
    start_index = next(
        (index for index, line in enumerate(lines) if "join logic summary" in line.casefold() or "joined table / cte" in line.casefold()),
        -1,
    )
    if start_index < 0:
        return {}

    joins: dict[str, str] = {}
    window = lines[start_index + 1 : start_index + 20]
    for line in window:
        lowered = line.casefold()
        if "iso2" in lowered or "country" in lowered or "market" in lowered:
            joins.setdefault("Geography", clean_join_field_value(line, "Geography", schema_fields) or line)
        if "performance year" in lowered or "rate year" in lowered or "date" in lowered or "month-start" in lowered:
            joins.setdefault("Date", clean_join_field_value(line, "Date", schema_fields) or line)
        if "product" in lowered or "hierarchy" in lowered or "product group" in lowered:
            joins.setdefault("Product", clean_join_field_value(line, "Product", schema_fields) or line)
        if "brand" in lowered:
            joins.setdefault("Brand", clean_join_field_value(line, "Brand", schema_fields) or line)
    return joins


def extract_explicit_questions(lines: list[str]) -> list[str]:
    section_items = collect_section_lines(
        lines,
        start_markers=("main questions it helps answer", "questions it helps answer", "main questions"),
        stop_markers=("scope", "business use", "business purpose", "kpis"),
        limit=5,
    )
    questions = [item for item in section_items if "?" in item]
    if questions:
        return dedupe_preserve_order(questions)[:3]

    fallback: list[str] = []
    blocked = ("what is this dataset", "how is this dataset used", "what decisions depend")
    for line in lines:
        if "?" not in line:
            continue
        cleaned = re.sub(r"^[0-9]+[.)]?\s*", "", line).strip()
        if len(cleaned.split()) < 5:
            continue
        if any(token in cleaned.casefold() for token in blocked):
            continue
        fallback.append(cleaned)
    return dedupe_preserve_order(fallback)[:3]


def extract_explicit_business_uses(lines: list[str]) -> list[str]:
    section_items = collect_section_lines(
        lines,
        start_markers=("business use", "business purpose", "used for"),
        stop_markers=("kpis", "grain", "joins", "important fields"),
        limit=6,
    )
    cleaned_section = [
        item for item in section_items
        if "dataset is mainly used for" not in item.casefold()
    ]
    if cleaned_section:
        return dedupe_preserve_order(cleaned_section)[:3]

    candidates: list[str] = []
    meaningful_tokens = (
        "tracking", "analysis", "monitor", "monitoring", "benchmark", "benchmarking",
        "trend", "optimization", "reporting", "attribution", "funnel", "segmentation",
        "resolution", "performance", "efficiency", "comparison", "visibility",
    )
    for line in lines:
        lowered = line.casefold()
        if "used for" in lowered or "supports" in lowered or any(token in lowered for token in meaningful_tokens):
            if len(line.split()) < 4:
                continue
            candidates.append(line)
    return dedupe_preserve_order(candidates)[:3]


def infer_model_kind(raw_html: str, lines: list[str], schema_fields: list[SchemaField]) -> tuple[str, list[str]]:
    text = " ".join(lines).casefold()
    schema_blob = " ".join(field_info.name for field_info in schema_fields).casefold()
    cte_count = len(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\s+as\s*\(", raw_html, flags=re.I))
    join_count = len(re.findall(r"\bjoin\b", raw_html, flags=re.I))
    cast_count = len(re.findall(r"\bcast\s*\(", raw_html, flags=re.I))
    sql_keywords = (
        "with ", "select ", "group by", "left join", "inner join", "union all",
        "inflation", "fx", "exchange rate", "currency", "roas", "cpa", "cost reach",
    )

    reasons: list[str] = []
    if cte_count >= 2:
        reasons.append(f"multiple CTEs detected ({cte_count})")
    if join_count >= 3:
        reasons.append(f"multiple SQL joins detected ({join_count})")
    if cast_count >= 2:
        reasons.append(f"data type casting logic detected ({cast_count} casts)")
    if any(keyword in text or keyword in raw_html.casefold() or keyword in schema_blob for keyword in ("roas", "cpa", "cost reach", "inflation", "fx", "exchange_rate", "inflation_multiplier")):
        reasons.append("derived KPI or normalization logic detected")
    if sum(1 for keyword in sql_keywords if keyword in raw_html.casefold() or keyword in text) >= 4:
        reasons.append("pipeline-style SQL logic detected")

    if reasons:
        return "analytical_model", dedupe_preserve_order(reasons)
    return "dataset", []


def parse_sql_derived_kpis(raw_html: str, lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    default_logic = {
        "ROAS": "Derived return-on-ad-spend metric calculated from modelled revenue and spend outputs.",
        "CPA": "Derived cost-per-acquisition metric calculated from modelled spend and conversion outputs.",
        "Cost Reach": "Derived efficiency metric that relates spend to reach after model normalization.",
        "Normalized Spend": "Derived spend output adjusted through FX and/or inflation normalization logic.",
    }

    for label, patterns in COMPLEX_MODEL_KPI_HINTS.items():
        for line in lines:
            lowered = line.casefold()
            if any(pattern in lowered for pattern in patterns):
                candidate_logic = line.strip()
                if len(candidate_logic.split()) > 20:
                    candidate_logic = default_logic.get(label, candidate_logic)
                rows.append([label, candidate_logic])
                break

    alias_matches = re.findall(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)", raw_html, flags=re.I)
    alias_lookup = dedupe_preserve_order(alias_matches)
    for label, patterns in COMPLEX_MODEL_KPI_HINTS.items():
        if any(any(pattern.replace(" ", "_") in alias.casefold() for pattern in patterns) for alias in alias_lookup):
            if not any(existing[0] == label for existing in rows):
                matching_alias = next(
                    (
                        alias for alias in alias_lookup
                        if any(pattern.replace(" ", "_") in alias.casefold() for pattern in patterns)
                    ),
                    label,
                )
                rows.append([label, default_logic.get(label, f"Derived in SQL model output as {matching_alias}")])

    if not rows:
        return []

    deduped: list[list[str]] = []
    seen_rows: set[str] = set()
    for name, logic in rows:
        cleaned_logic = re.sub(r"\s+", " ", logic).strip()
        signature = f"{name}|{cleaned_logic}".casefold()
        if signature in seen_rows:
            continue
        seen_rows.add(signature)
        deduped.append([name, cleaned_logic])
    return deduped[:8]


def extract_analytical_field_rows(schema_fields: list[SchemaField], raw_html: str, lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    line_blob = " ".join(lines).casefold()
    for label, hints in ANALYTICAL_FIELD_HINTS.items():
        matching_fields = [
            field_info.name for field_info in schema_fields
            if any(hint.replace(" ", "_") in field_info.name.casefold() or hint in field_info.description.casefold() for hint in hints)
        ]
        if matching_fields:
            rows.append([matching_fields[0], label])
            continue
        if any(hint in line_blob or hint in raw_html.casefold() for hint in hints):
            rows.append([label, f"Derived logic documented in the model using {sentence_list([hint for hint in hints[:2]], fallback='model rules')}"])
    return rows[:6]


def translate_analytical_business_uses(raw_html: str, lines: list[str]) -> list[str]:
    text = f"{raw_html} {' '.join(lines)}".casefold()
    uses: list[str] = []
    if any(token in text for token in ("fx", "exchange rate", "usd", "local currency")):
        uses.append("Normalize media spend across markets using FX-adjusted values so cross-country reporting is comparable.")
    if any(token in text for token in ("inflation", "inflation_multiplier", "real terms")):
        uses.append("Normalize spend across time using inflation-adjusted values to support like-for-like trend analysis.")
    if any(token in text for token in ("roas", "return_on_ad_spend", "cpa", "cost reach")):
        uses.append("Publish derived efficiency KPIs such as ROAS, CPA or cost-reach so stakeholders can evaluate marketing effectiveness without rebuilding the model logic.")
    if raw_html.casefold().count("join") >= 3:
        uses.append("Combine multiple source tables and conformed dimensions into a single analytical model ready for downstream dashboards and performance reviews.")
    return dedupe_preserve_order(uses)[:4]


def score_profile(title: str, lines: list[str], schema_fields: list[SchemaField], profile: str) -> int:
    joined_text = f"{title} {' '.join(lines)}".casefold()
    field_names = " ".join(field_info.name for field_info in schema_fields).casefold()
    rules = PROFILE_RULES[profile]
    score = 0
    score += sum(5 for keyword in rules["strong"] if keyword in joined_text)
    score += sum(3 for keyword in rules["strong"] if keyword in field_names)
    score += sum(2 for keyword in rules["schema"] if keyword in field_names)
    return score


def infer_profile(title: str, lines: list[str], schema_fields: list[SchemaField]) -> str:
    scores = {
        profile: score_profile(title, lines, schema_fields, profile)
        for profile in ("paid_media", "owned_crm", "search_trends", "web_behavior")
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_profile, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < 4 or best_score == second_score == 0:
        return "other"
    return best_profile


def classify_fields(
    profile: str,
    schema_fields: list[SchemaField],
    lines: list[str],
    explicit_kpis: list[list[str]],
    explicit_joins: dict[str, str],
) -> tuple[list[str], list[str]]:
    metrics: list[str] = []
    dimensions: list[str] = []

    field_names = [field_info.name for field_info in schema_fields]
    explicit_kpi_blob = " ".join(" ".join(row) for row in explicit_kpis)
    text_blob = " ".join(lines).casefold()

    for label, patterns in METRIC_PATTERNS:
        if label not in PROFILE_ALLOWED_METRICS.get(profile, PROFILE_ALLOWED_METRICS["other"]):
            continue
        if any(field_matches_patterns(field_name, patterns) for field_name in field_names):
            metrics.append(label)
            continue
        if explicit_kpi_blob and any(pattern.casefold() in explicit_kpi_blob.casefold() for pattern in patterns):
            metrics.append(label)
            continue
        if profile == "search_trends" and label in {"Search Volume", "Share of Search", "Visibility Score"} and any(pattern.casefold() in text_blob for pattern in patterns):
            metrics.append(label)

    for label, patterns in DIMENSION_PATTERNS:
        if label not in PROFILE_ALLOWED_DIMENSIONS.get(profile, PROFILE_ALLOWED_DIMENSIONS["other"]):
            continue
        if any(field_matches_patterns(field_name, patterns) for field_name in field_names):
            dimensions.append(label)
            continue
        if label in explicit_joins:
            dimensions.append(label)
    return dedupe_preserve_order(metrics), dedupe_preserve_order(dimensions)


def infer_refresh_frequency_label(lines: list[str]) -> str:
    text = " ".join(lines).casefold()
    for label in ("daily", "weekly", "monthly", "hourly"):
        if label in text:
            return label.capitalize()
    return "Not specified"


def infer_refresh_type_label(raw_html: str, lines: list[str]) -> str:
    text = f"{raw_html} {' '.join(lines)}".casefold()
    if "incremental" in text:
        return "Incremental"
    if "full" in text:
        return "Full"
    return "Not specified"


def infer_time_range_label(lines: list[str], frequency: str) -> str:
    text = " ".join(lines)
    if "since" in text.casefold():
        match = re.search(r"since\s+([0-9]{4}(?:-[0-9]{2}-[0-9]{2})?)", text, flags=re.I)
        if match:
            suffix = f", refresh {frequency.lower()}" if frequency != "Not specified" else ""
            return f"{match.group(1)} onward{suffix}"
    years = re.findall(r"\b20\d{2}\b", text)
    if years:
        suffix = f", refresh {frequency.lower()}" if frequency != "Not specified" else ""
        return f"Historical coverage is referenced from {years[0]}{suffix}"
    if frequency != "Not specified":
        return f"Not explicitly documented; refresh appears to be {frequency.lower()}."
    return "Not specified"


def infer_geography_label(schema_fields: list[SchemaField], lines: list[str]) -> str:
    text = " ".join(lines).casefold()
    if "global" in text or "worldwide" in text:
        return "Global"
    if "country" in text or any("country" in field_info.name.casefold() or "iso" in field_info.name.casefold() for field_info in schema_fields):
        return "Multi-country; validate exact market coverage from the source feed."
    if "us" in text or "united states" in text:
        return "United States"
    return "Not specified"


def infer_domain_label(profile: str, title: str, combined_text: str) -> str:
    if profile == "paid_media":
        return "Marketing / Paid Media"
    if profile == "owned_crm":
        return "Consumer / CRM"
    if profile == "search_trends":
        return "Marketing / Search / Trends"
    if profile == "web_behavior":
        return "Web / Digital Behavior"
    return infer_data_domain(title, combined_text)


def infer_key_fields(schema_fields: list[SchemaField], title: str) -> list[str]:
    explicit = [
        field_info.name for field_info in schema_fields
        if has_field_token(field_info.name, "id", "number", "code", "key")
    ]
    if explicit:
        return dedupe_preserve_order(explicit)[:5]
    fallback = [
        field_info.name for field_info in schema_fields
        if has_field_token(field_info.name, "date", "brand", "product", "campaign", "country", "tracker")
    ]
    if fallback:
        return dedupe_preserve_order(fallback)[:5]
    return [f"No explicit technical key is documented for {title}; a composite reporting key should be assumed."]


def extract_explicit_grain(lines: list[str]) -> str:
    for line in lines:
        lowered = line.casefold()
        if "one row per" in lowered:
            return line
        if lowered.startswith("grain") and len(line.split()) > 2:
            return line.replace("Grain", "").replace("grain", "").strip(" :")
    return ""


def infer_grain(title: str, profile: str, schema_fields: list[SchemaField], dimensions: list[str], lines: list[str]) -> tuple[str, list[str]]:
    explicit = extract_explicit_grain(lines)
    if explicit:
        notes = [line for line in lines if any(token in line.casefold() for token in ("duplicate", "distinct", "multiple"))]
        return explicit, dedupe_preserve_order(notes)[:3]

    field_blob = " ".join(field_info.name for field_info in schema_fields).casefold()
    notes: list[str] = []
    if profile == "owned_crm":
        if "topic" in field_blob and ("case" in field_blob or "case_number" in field_blob):
            notes.append("Cases may repeat when multiple topics are assigned, so unique case metrics should use distinct case identifiers.")
            return "One row likely represents a case-topic assignment at the case reporting date level.", notes
        return "One row likely represents a consumer case at the reporting date level.", notes
    if profile == "search_trends":
        if "category" in field_blob and "country" in field_blob:
            return "One row likely represents a brand/category/country/time combination for search-demand analysis.", notes
        if "brand" in field_blob and "country" in field_blob:
            return "One row likely represents a brand/country/time combination used for demand or visibility tracking.", notes
    if profile == "paid_media":
        grain_dims = [label for label in ("Date", "Campaign", "Channel", "Placement", "Product", "Brand") if label in dimensions]
        if grain_dims:
            return f"One row likely represents an aggregated paid-media reporting slice by {' + '.join(grain_dims[:5])}.", notes
    if profile == "web_behavior":
        grain_dims = [label for label in ("Date", "Brand", "Product", "Country") if label in dimensions]
        if grain_dims:
            return f"One row likely represents a web-performance reporting slice by {' + '.join(grain_dims[:4])}.", notes
    return "The dataset appears to be stored at a reporting grain defined by its main business dimensions rather than raw event-level records.", notes


def infer_join_mapping(profile: str, schema_fields: list[SchemaField], lines: list[str]) -> dict[str, str]:
    join_priority = JOIN_PRIORITY_BY_PROFILE.get(profile, JOIN_PRIORITY_BY_PROFILE["other"])
    joins: dict[str, str] = {}
    for dimension, patterns in join_priority.items():
        if dimension == "Date":
            preferred_exact = next(
                (
                    field_info.name
                    for field_info in schema_fields
                    if field_info.name.casefold() in {"date", "performance_date", "l1_performance_date", "event_date", "session_date"}
                ),
                "",
            )
            if preferred_exact:
                joins[dimension] = preferred_exact
                continue
        ranked_candidates = sorted(
            schema_fields,
            key=lambda field_info: score_join_candidate(field_info.name, patterns),
            reverse=True,
        )
        if ranked_candidates and score_join_candidate(ranked_candidates[0].name, patterns) >= 8:
            best_name = ranked_candidates[0].name
            best_score = score_join_candidate(best_name, patterns)
            second_name = ""
            second_score = -999
            if len(ranked_candidates) > 1:
                second_name = ranked_candidates[1].name
                second_score = score_join_candidate(second_name, patterns)

            chosen = best_name
            if second_name and best_score - second_score <= 1 and second_score >= 8:
                chosen = f"{best_name} (chosen as primary join; {second_name} is also plausible depending on the reporting cut)"
        else:
            chosen = ""
        if not chosen:
            chosen = "Not specified; no reliable join field could be inferred from the current page."
        joins[dimension] = chosen
    return joins


def join_value_specificity(value: str) -> int:
    cleaned = clean_unspecified(value, "")
    if not cleaned:
        return 0
    lowered = cleaned.casefold()
    if lowered.startswith("not specified"):
        return 0
    score = 1
    if "_" in cleaned:
        score += 5
    if len(cleaned) > 8:
        score += 1
    if "chosen as primary join" in lowered:
        score += 1
    if lowered in {"date", "brand", "product", "country", "tracker", "category", "market"}:
        score -= 3
    return score


def merge_join_mappings(explicit: dict[str, str], inferred: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for dimension in ("Date", "Brand", "Product", "Geography"):
        explicit_value = clean_unspecified(explicit.get(dimension, ""), "")
        inferred_value = clean_unspecified(inferred.get(dimension, ""), "")
        if join_value_specificity(inferred_value) > join_value_specificity(explicit_value):
            merged[dimension] = inferred_value or explicit_value or "Not specified; no reliable join field could be inferred from the current page."
        else:
            merged[dimension] = explicit_value or inferred_value or "Not specified; no reliable join field could be inferred from the current page."
    return merged


def generate_overview(signals: DataDomainSignals, title: str) -> str:
    metric_phrase = sentence_list(signals.metrics, fallback="the documented measures", limit=4).lower()
    dimension_phrase = sentence_list(signals.dimensions, fallback="the documented reporting dimensions", limit=4).lower()
    if signals.model_kind == "analytical_model":
        kpi_phrase = sentence_list([row[0] for row in signals.kpis], fallback="derived reporting outputs", limit=4)
        complexity_phrase = sentence_list(signals.model_complexity_reasons, fallback="multi-step SQL transformations", limit=2).lower()
        return (
            f"This analytical model transforms and combines data from {signals.source_label} into derived reporting outputs, including {kpi_phrase}. "
            f"It should be treated as a pipeline-ready analytics layer rather than a raw source table, and it supports business analysis across {dimension_phrase}. "
            f"The model includes {complexity_phrase}."
        )
    if signals.profile == "paid_media":
        return (
            f"This dataset contains paid media delivery and performance data from {signals.source_label}, including {metric_phrase}. "
            f"It supports campaign, channel and product analysis across {dimension_phrase}, and is suitable for monitoring reach, engagement and media efficiency."
        )
    if signals.profile == "search_trends":
        return (
            f"This dataset contains search and trend data from {signals.source_label}, covering {metric_phrase}. "
            f"It is used to analyse brand demand, competitive movement and market trends across {dimension_phrase}."
        )
    if signals.profile == "owned_crm":
        return (
            f"This dataset contains consumer interaction and service records from {signals.source_label}, including {metric_phrase}. "
            f"It is used to monitor case volume, service performance and consumer issues across {dimension_phrase}."
        )
    if signals.profile == "web_behavior":
        return (
            f"This dataset contains web and digital behavior data from {signals.source_label}, covering {metric_phrase}. "
            f"It supports traffic, engagement and conversion analysis across {dimension_phrase}."
        )
    return (
        f"This dataset contains structured data from {signals.source_label}, with business coverage across {dimension_phrase}. "
        f"Based on the documented fields and logic, it supports analysis of {metric_phrase} at the reporting grain exposed in the source page."
    )


def generate_questions(signals: DataDomainSignals, title: str) -> list[str]:
    explicit_questions = extract_explicit_questions(html_to_lines(signals.preserved_context_html))
    if explicit_questions and signals.model_kind != "analytical_model":
        return explicit_questions

    metrics = set(signals.metrics)
    metric_phrase = sentence_list(signals.metrics, fallback="the documented metrics", limit=4).lower()
    dimensions = sentence_list(signals.dimensions, fallback="its documented business dimensions", limit=4).lower()
    if signals.model_kind == "analytical_model":
        questions = []
        if any(kpi[0] in {"ROAS", "CPA", "Cost Reach", "Normalized Spend"} for kpi in signals.kpis):
            questions.append("How do the model's derived efficiency KPIs change by market, brand or campaign over time?")
        if any(token in " ".join(signals.business_uses).casefold() for token in ("normalize", "inflation", "fx-adjusted", "fx adjusted")):
            questions.append("How do FX and inflation normalization rules change the comparability of spend and performance across markets and time?")
        questions.append("Which downstream business decisions rely on this transformed model rather than on the raw source tables?")
        return dedupe_preserve_order(questions)[:3]

    if signals.profile == "paid_media":
        questions: list[str] = []
        if {"Impressions", "Clicks", "Spend / Cost"} & metrics:
            questions.append(
                f"Which campaigns, channels or placements are delivering the strongest performance across {dimensions}?"
            )
        if "Spend / Cost" in metrics and any(metric in metrics for metric in ("Clicks", "Engagements", "Video Completes", "Add to Carts")):
            questions.append("Where is spend generating the strongest response or efficiency, and where is it underperforming?")
        if any(metric in metrics for metric in ("Video Starts", "Video Completes", "Engagements", "Add to Carts")):
            questions.append("How does post-impression engagement change by brand, campaign objective or media tactic over time?")
        return dedupe_preserve_order(questions)[:3] or [
            "Which campaigns, channels and placements are driving the highest volume and engagement?",
            "How efficiently is spend converting into clicks, engagement or downstream intent signals?",
            "Where do performance patterns differ by brand, product or media tactic over time?",
        ]
    if signals.profile == "search_trends":
        questions: list[str] = []
        if "Search Volume" in metrics or "Share of Search" in metrics:
            questions.append("How is brand demand evolving over time by brand, category or market?")
            questions.append("Which competitors or categories are gaining or losing search interest share?")
        if "Visibility Score" in metrics:
            questions.append("Where is visibility strengthening or weakening across brands, prompts or markets?")
        return dedupe_preserve_order(questions)[:3] or [
            "How is brand demand or visibility trending over time by market and brand?",
            "Which competitors, categories or search topics are gaining or losing share?",
            "Where are there shifts in earned or search interest that should influence marketing decisions?",
        ]
    if signals.profile == "owned_crm":
        questions = []
        if "Cases" in metrics:
            questions.append("How many cases are being created by brand, geography, topic or contact reason over time?")
        if any(metric in metrics for metric in ("Response Time", "Satisfaction", "Sentiment")):
            questions.append("Where are service quality issues emerging in response time, satisfaction or sentiment?")
        questions.append("Which products, brands or topics are driving the highest service workload or escalation risk?")
        return dedupe_preserve_order(questions)[:3]
    if signals.profile == "web_behavior":
        questions = []
        if any(metric in metrics for metric in ("Sessions", "Users", "Conversions")):
            questions.append("How is site traffic and conversion behavior evolving by brand, market and time?")
        questions.append("Which journeys, channels or touchpoints are driving the strongest digital outcomes?")
        questions.append("Where are there meaningful changes in user behavior that explain performance movement?")
        return dedupe_preserve_order(questions)[:3]
    return [
        f"How do {metric_phrase} vary across {dimensions}?",
        f"Which business segments or reporting slices show the most material movement in {title}?",
        f"What reliable analyses can be built from the documented grain, joins and KPI candidates in {title}?",
    ]


def generate_business_uses(signals: DataDomainSignals) -> list[str]:
    explicit_uses = extract_explicit_business_uses(html_to_lines(signals.preserved_context_html))
    if explicit_uses and signals.model_kind != "analytical_model":
        return explicit_uses
    if signals.model_kind == "analytical_model":
        translated_uses = translate_analytical_business_uses(signals.preserved_context_html, html_to_lines(signals.preserved_context_html))
        if translated_uses:
            return translated_uses

    metrics = set(signals.metrics)
    if signals.profile == "paid_media":
        uses = ["Campaign performance tracking by campaign, channel, placement and brand."]
        if "Spend / Cost" in metrics:
            uses.append("Media efficiency analysis using spend-based KPIs such as CPM, CPC or cost per outcome.")
        if any(metric in metrics for metric in ("Engagements", "Video Completes", "Add to Carts")):
            uses.append("Creative and tactic evaluation using engagement, video or intent-response metrics.")
        return dedupe_preserve_order(uses)[:3]
    if signals.profile == "search_trends":
        uses = ["Demand and visibility trend monitoring by brand, market and category."]
        if any(metric in metrics for metric in ("Share of Search", "Visibility Score")):
            uses.append("Competitive benchmarking using share, visibility or ranking-style metrics.")
        uses.append("Early signal detection for changes in brand interest, discoverability or earned momentum.")
        return dedupe_preserve_order(uses)[:3]
    if signals.profile == "owned_crm":
        uses = ["Case volume and service performance monitoring by brand, country and topic."]
        if any(metric in metrics for metric in ("Sentiment", "Satisfaction")):
            uses.append("Consumer experience analysis to prioritise service pain points and issue categories.")
        if "Response Time" in metrics:
            uses.append("Operational reporting on response speed, resolution efficiency and service-level adherence.")
        return dedupe_preserve_order(uses)[:3]
    if signals.profile == "web_behavior":
        uses = ["Traffic and engagement monitoring across brands, markets and digital touchpoints."]
        if "Conversions" in metrics:
            uses.append("Funnel and conversion analysis to identify drop-off and optimization opportunities.")
        uses.append("Digital performance reporting for trend reviews and stakeholder updates.")
        return dedupe_preserve_order(uses)[:3]
    return [
        f"Use this dataset for analysis anchored on {sentence_list(signals.metrics, fallback='the documented metrics').lower()}.",
        f"Use it where reporting needs depend on {sentence_list(signals.dimensions, fallback='its documented business dimensions').lower()}.",
        "Validate business definitions against the documented grain and transformation logic before using it in KPI reporting.",
    ]


def generate_inferred_kpis(
    profile: str,
    model_kind: str,
    explicit_kpis: list[list[str]],
    schema_fields: list[SchemaField],
    raw_html: str,
    lines: list[str],
) -> list[list[str]]:
    meaningful_explicit_kpis = [
        row for row in explicit_kpis
        if row and clean_unspecified(row[0], "") and clean_unspecified(row[0], "").casefold() != "not specified"
    ]
    if meaningful_explicit_kpis:
        return meaningful_explicit_kpis
    if explicit_kpis and model_kind != "analytical_model":
        return explicit_kpis
    if model_kind == "analytical_model":
        sql_kpis = parse_sql_derived_kpis(raw_html, lines)
        if sql_kpis:
            return sql_kpis

    field_lookup = {field_info.name.casefold(): field_info.name for field_info in schema_fields}
    rows: list[list[str]] = []

    def field_like(*tokens: str) -> str | None:
        for token in tokens:
            for normalized_name, original_name in field_lookup.items():
                if token in normalized_name:
                    return original_name
        return None

    impressions = field_like("impression")
    clicks = field_like("click")
    cost = field_like("final_cost", "media_cost", "dynamic_cost", "cost", "spend")
    engagements = field_like("engagement")
    video_starts = field_like("video_start")
    video_completes = field_like("video_complete")
    add_to_carts = field_like("add_to_cart")
    volume = field_like("volume")
    share = field_like("percentage", "share")
    cases = field_like("case_number", "case")
    response_time = field_like("hours_to_first_respond", "response")
    visibility = field_like("visibility")

    if profile == "paid_media":
        if impressions:
            rows.append(["Impressions", f"SUM({impressions})"])
        if clicks:
            rows.append(["Clicks", f"SUM({clicks})"])
        if impressions and clicks:
            rows.append(["CTR (inferred)", f"SUM({clicks}) / NULLIF(SUM({impressions}), 0)"])
        if cost:
            rows.append(["Spend / Cost", f"SUM({cost})"])
        if cost and impressions:
            rows.append(["CPM (inferred)", f"(SUM({cost}) / NULLIF(SUM({impressions}), 0)) * 1000"])
        if cost and clicks:
            rows.append(["CPC (inferred)", f"SUM({cost}) / NULLIF(SUM({clicks}), 0)"])
        if engagements and impressions:
            rows.append(["Engagement Rate (inferred)", f"SUM({engagements}) / NULLIF(SUM({impressions}), 0)"])
        if video_starts and video_completes:
            rows.append(["Video Completion Rate (inferred)", f"SUM({video_completes}) / NULLIF(SUM({video_starts}), 0)"])
        if add_to_carts and clicks:
            rows.append(["Add-to-Cart Rate (inferred)", f"SUM({add_to_carts}) / NULLIF(SUM({clicks}), 0)"])
    elif profile == "owned_crm":
        if cases:
            rows.append(["Total Cases", f"COUNT(DISTINCT {cases})"])
        if response_time:
            rows.append(["Average Response Time", f"AVG({response_time})"])
    elif profile == "search_trends":
        if volume:
            rows.append(["Search Volume", f"SUM({volume})"])
        if share:
            rows.append(["Share of Search", f"AVG({share})"])
        if visibility:
            rows.append(["Visibility Score", f"MAX({visibility}) or AVG({visibility}) depending on reporting grain"])
    elif profile == "web_behavior":
        sessions = field_like("session")
        users = field_like("user", "active_user")
        conversions = field_like("conversion")
        if sessions:
            rows.append(["Sessions", f"SUM({sessions})"])
        if users:
            rows.append(["Users", f"SUM({users})"])
        if conversions:
            rows.append(["Conversions", f"SUM({conversions})"])
        if conversions and sessions:
            rows.append(["Conversion Rate (inferred)", f"SUM({conversions}) / NULLIF(SUM({sessions}), 0)"])
    else:
        if impressions:
            rows.append(["Impressions", f"SUM({impressions})"])
        if clicks:
            rows.append(["Clicks", f"SUM({clicks})"])
        if cost:
            rows.append(["Spend / Cost", f"SUM({cost})"])
        if volume:
            rows.append(["Search Volume", f"SUM({volume})"])
        if cases:
            rows.append(["Total Cases", f"COUNT(DISTINCT {cases})"])
        if response_time:
            rows.append(["Average Response Time", f"AVG({response_time})"])

    return rows[:8] or [["Not specified", "No reliable KPI could be inferred from the current page."]]


def score_field(field_info: SchemaField) -> int:
    name = field_info.name.casefold()
    score = 0
    if has_field_token(field_info.name, "id", "number", "code", "key"):
        score += 25
    if has_field_token(field_info.name, "date", "brand", "product", "campaign", "channel", "country", "market"):
        score += 20
    if any(token in name for token in ("impression", "click", "cost", "engagement", "volume", "share", "response", "sentiment", "satisfaction", "visibility")):
        score += 22
    if "deprecated" in field_info.description.casefold() or "new column" in field_info.description.casefold():
        score += 18
    if field_info.description:
        score += 5
    return score


def important_field_description(field_info: SchemaField) -> str:
    name = field_info.name.casefold()
    if field_info.description:
        return field_info.description
    if has_field_token(field_info.name, "id", "number", "code", "key"):
        return "Key identifier used to preserve business grain or support joins."
    if has_field_token(field_info.name, "date", "month", "week"):
        return "Primary time dimension used for reporting and trend analysis."
    if has_field_token(field_info.name, "brand"):
        return "Brand dimension used to segment reporting outputs."
    if has_field_token(field_info.name, "product"):
        return "Product-level dimension used for attribution and drill-down."
    if has_field_token(field_info.name, "campaign", "channel", "placement", "site"):
        return "Delivery dimension that explains where performance is coming from."
    if any(token in name for token in ("impression", "click", "cost", "engagement", "volume", "share", "visibility", "sentiment", "satisfaction")):
        return "Core metric field that drives KPI calculation."
    return "Business-relevant field selected from the documented schema."


def extract_logic_points(raw_html: str, lines: list[str], schema_fields: list[SchemaField]) -> tuple[list[str], list[str]]:
    logic_points: list[str] = []
    limitations: list[str] = []
    for line in lines:
        lowered = line.casefold()
        if "deprecated" in lowered:
            logic_points.append(line)
            limitations.append(line)
        if "new column" in lowered or lowered.startswith("added:") or lowered.startswith("removed column"):
            logic_points.append(line)
        if "format has changed" in lowered:
            logic_points.append(line)
            limitations.append(line)
        if any(token in lowered for token in ("null", "upper case", "harmonis", "standardized", "raw layer", "silver layer", "gold layer")):
            logic_points.append(line)
        if "distinct" in lowered and ("count" in lowered or "case" in lowered):
            limitations.append(line)

    case_blocks = re.findall(r"CASE\b.*?(?:END|$)", raw_html, flags=re.I | re.S)
    for block in case_blocks[:3]:
        cleaned = re.sub(r"\s+", " ", strip_tags(block)).strip()
        if cleaned:
            logic_points.append(cleaned)
            if "cost" in cleaned.casefold():
                limitations.append("Cost logic depends on conditional derivation, so rate-type handling should be validated for comparability.")

    for field_info in schema_fields:
        desc = field_info.description.casefold()
        if "deprecated" in desc:
            limitations.append(f"{field_info.name} is deprecated or no longer expected in the source feed.")
        if "always with null" in desc or "always null" in desc:
            limitations.append(f"{field_info.name} is documented as always null and may not be analytically reliable.")
        if field_info.type_name.upper() == "STRING" and any(token in field_info.name.casefold() for token in ("impression", "click", "cost", "volume", "engagement")):
            limitations.append(f"{field_info.name} is typed as STRING in the documentation, so numeric casting rules should be validated.")

    return dedupe_preserve_order(logic_points)[:8], dedupe_preserve_order(limitations)[:8]


def build_signal_package(title: str, raw_html: str, lines: list[str]) -> DataDomainSignals:
    schema_fields = parse_schema_fields(raw_html, lines)
    profile = infer_profile(title, lines, schema_fields)
    model_kind, model_complexity_reasons = infer_model_kind(raw_html, lines, schema_fields)
    source_tables = dedupe_preserve_order(re.findall(r"\b[a-zA-Z0-9_-]+\.[a-zA-Z0-9_\-{}]+\.[a-zA-Z0-9_\-{}]+\b", ' '.join(lines)))[:3]
    explicit_kpis = parse_explicit_kpis(raw_html)
    explicit_joins = parse_explicit_joins(raw_html, schema_fields)
    if model_kind == "analytical_model" and not explicit_joins:
        explicit_joins = parse_analytical_join_summary(lines, schema_fields)
    metrics, dimensions = classify_fields(profile, schema_fields, lines, explicit_kpis, explicit_joins)
    frequency = infer_refresh_frequency_label(lines)
    refresh_type = infer_refresh_type_label(raw_html, lines)
    grain, grain_notes = infer_grain(title, profile, schema_fields, dimensions, lines)
    inferred_joins = infer_join_mapping(profile, schema_fields, lines)
    joins = explicit_joins if model_kind == "analytical_model" and explicit_joins else merge_join_mappings(explicit_joins, inferred_joins)
    logic_points, limitations = extract_logic_points(raw_html, lines, schema_fields)

    signals = DataDomainSignals(
        profile=profile,
        source_label=infer_source_label(title, lines),
        model_kind=model_kind,
        source_tables=source_tables,
        schema_fields=schema_fields,
        metrics=metrics,
        dimensions=dimensions,
        key_fields=infer_key_fields(schema_fields, title),
        grain=grain,
        grain_notes=grain_notes,
        joins=joins,
        logic_points=logic_points,
        limitations=limitations,
        domain=clean_unspecified(infer_domain_label(profile, title, " ".join(lines))),
        geography=clean_unspecified(infer_geography_label(schema_fields, lines)),
        time_range=clean_unspecified(infer_time_range_label(lines, frequency)),
        refresh_frequency=frequency,
        refresh_type=refresh_type,
        model_complexity_reasons=model_complexity_reasons,
        preserved_context_html=raw_html,
    )
    signals.kpis = generate_inferred_kpis(profile, model_kind, explicit_kpis, schema_fields, raw_html, lines)
    signals.business_uses = generate_business_uses(signals)
    signals.questions = generate_questions(signals, title)
    ranked_fields = sorted(schema_fields, key=score_field, reverse=True)
    ranked_field_rows = [
        [field_info.name, important_field_description(field_info)]
        for field_info in ranked_fields[:8]
    ]
    if model_kind == "analytical_model":
        ranked_field_rows = extract_analytical_field_rows(schema_fields, raw_html, lines) + ranked_field_rows
    deduped_rows: list[list[str]] = []
    seen_field_names: set[str] = set()
    for field_name, description in ranked_field_rows:
        normalized = field_name.casefold()
        if normalized in seen_field_names:
            continue
        seen_field_names.add(normalized)
        deduped_rows.append([field_name, description])
    signals.important_fields = deduped_rows[:8] or [["Not specified", "The current page does not expose a stable field list."]]
    allowed_metrics = PROFILE_ALLOWED_METRICS.get(profile, PROFILE_ALLOWED_METRICS["other"])
    signals.metrics = [metric for metric in signals.metrics if metric in allowed_metrics]
    allowed_dimensions = PROFILE_ALLOWED_DIMENSIONS.get(profile, PROFILE_ALLOWED_DIMENSIONS["other"])
    signals.dimensions = [dimension for dimension in signals.dimensions if dimension in allowed_dimensions]
    return signals


def find_data_domains_root(client: ConfluenceClient, space: dict[str, Any]) -> tuple[dict[str, Any], str]:
    space_key = str(space.get("key"))
    candidates = client.search_content(
        f'space = "{space_key}" AND title = "Data Documentation"',
        limit=20,
        expand="ancestors",
    )
    if not candidates:
        raise ConfluenceError('No page titled "Data Documentation" was found in the target space.')

    required_tokens = ("analytics portal", "execution phase", "data documentation")
    for candidate in candidates:
        ancestors = [str(item.get("title", "")) for item in candidate.get("ancestors", [])]
        ancestor_blob = " > ".join(ancestors).casefold()
        if all(token in ancestor_blob for token in required_tokens):
            route = " > ".join(ancestors + [str(candidate.get("title", ""))])
            return candidate, route

    raise ConfluenceError(
        'Found "Data Documentation" pages, but none matched the route under Analytics Portal > Execution Phase > Data Documentation.'
    )


def descendant_leaf_pages(root_id: str, by_parent: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stack = list(sorted(by_parent.get(root_id, []), key=lambda page: str(page.get("title", "")).casefold(), reverse=True))
    while stack:
        page = stack.pop()
        page_id = str(page.get("id"))
        children = sorted(by_parent.get(page_id, []), key=lambda child: str(child.get("title", "")).casefold(), reverse=True)
        if children:
            stack.extend(children)
        else:
            results.append(page)
    return results


def is_data_domain_standardized(body_html: str) -> bool:
    headings = set(find_headings(body_html))
    return all(heading in headings for heading in DATA_DOMAIN_REQUIRED_HEADINGS)


def build_data_domain_body(page: dict[str, Any]) -> tuple[str, list[str]]:
    title = str(page.get("title", "")).strip() or "Data Source"
    raw_html = page.get("body", {}).get("storage", {}).get("value", "")
    lines = html_to_lines(raw_html)
    signals = build_signal_package(title, raw_html, lines)

    source_tables_block = list_to_bullets(
        signals.source_tables,
        "Not specified",
        limit=5,
    )
    kpi_rows = signals.kpis or [["Not specified", "No reliable KPI could be inferred from the current page."]]
    join_rows = [
        [dimension, clean_unspecified(field_name, "Not specified")]
        for dimension, field_name in signals.joins.items()
    ] or [
        ["Date", "Not specified"],
        ["Brand", "Not specified"],
        ["Product", "Not specified"],
        ["Geography", "Not specified"],
    ]
    grain_notes = signals.grain_notes or ["No explicit duplicate or row-explosion warning was documented beyond the inferred grain."]
    logic_lines = signals.logic_points or ["No explicit raw-to-gold transformation steps were documented beyond the inferred schema behavior."]
    limitations = signals.limitations or ["No material data limitations were explicitly documented in the current page."]

    # Business rule: inferred content is useful for recovery, but it must remain visibly
    # marked so data owners know which documentation still needs human confirmation.
    changes = [
        "template headings applied",
        "documentation rebuilt using inferred schema, KPI, grain and join signals",
        "existing technical context preserved under Notes / Limitations",
    ]
    if signals.model_kind == "analytical_model":
        changes.append("classified as analytical model and documented as a transformation pipeline")
    if signals.source_tables:
        changes.append("source tables identified")
    if signals.kpis and signals.kpis[0][0] != "Not specified":
        changes.append("dataset-specific KPIs documented")
    if signals.grain:
        changes.append("reporting grain and key strategy documented")
    if signals.logic_points:
        changes.append("business logic and schema-change notes summarized")

    body = [
        "<h1>1. Overview</h1>",
        f"<p>{escape(generate_overview(signals, title))}</p>",
        "<p><strong>Source table:</strong></p>",
        source_tables_block,
        "<p><strong>Main questions it helps answer:</strong></p>",
        list_to_bullets(signals.questions, "Not specified", limit=5),
        "<h1>2. Scope</h1>",
        (
            f"<p><strong>Domain:</strong> {escape(clean_unspecified(signals.domain))}<br/>"
            f"<strong>Geography:</strong> {escape(clean_unspecified(signals.geography))}<br/>"
            f"<strong>Time range:</strong> {escape(clean_unspecified(signals.time_range))}</p>"
        ),
        "<h1>3. Business Use</h1>",
        "<p>This dataset is mainly used for:</p>",
        list_to_bullets(signals.business_uses, "Not specified", limit=5),
        "<h1>4. KPIs</h1>",
        render_html_table(["KPI", "Logic"], kpi_rows),
        "<p>Derived KPI logic should be validated against production SQL before being reused in certified reporting.</p>",
        "<h1>5. Grain & Keys</h1>",
        f"<p><strong>Grain:</strong><br/>{escape(clean_unspecified(signals.grain))}</p>",
        "<p><strong>Key fields:</strong></p>",
        list_to_bullets(signals.key_fields, "Not specified", limit=5),
        "<p><strong>Notes:</strong></p>",
        list_to_bullets(grain_notes, "Not specified", limit=5),
        "<h1>6. Joins</h1>",
        render_html_table(["Dimension", "Field"], join_rows),
        "<h1>7. Important Fields</h1>",
        render_html_table(["Field", "Description"], signals.important_fields),
        "<p>Only the most analytically relevant fields are listed here.</p>",
        "<h1>8. Data Logic (Raw + Silver + Gold)</h1>",
        "<p>Data logic below summarizes the transformations, schema changes and business rules that materially affect analysis.</p>",
        "<p><strong>Business logic applied:</strong></p>",
        list_to_bullets(logic_lines, "Not specified", limit=8),
        "<p>The dataset should be treated as analytics-ready only after validating these transformations in the active pipeline.</p>",
        "<h1>9. Refresh</h1>",
        (
            f"<p><strong>Frequency:</strong> {escape(clean_unspecified(signals.refresh_frequency))}<br/>"
            f"<strong>Type:</strong> {escape(clean_unspecified(signals.refresh_type))}</p>"
        ),
        "<h1>10. Notes / Limitations</h1>",
        list_to_bullets(limitations, "Not specified", limit=8),
        "<p><strong>Preserved source context from the previous page version:</strong></p>",
        raw_html if raw_html.strip() else "<p>No original body content was available.</p>",
    ]
    return "\n".join(body), dedupe_preserve_order(changes)


def standardize_data_domains(
    client: ConfluenceClient,
    *,
    space_identifier: str,
    page_title: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    space = client.find_space(space_identifier)
    root_page, route = find_data_domains_root(client, space)
    descendants = client.search_content(
        f'space = "{space.get("key")}" AND ancestor = {root_page.get("id")}',
        limit=FULL_SPACE_FETCH_LIMIT,
        expand="body.storage,version,ancestors",
    )

    by_parent: dict[str, list[dict[str, Any]]] = {}
    eligible_descendants: list[dict[str, Any]] = []
    excluded_count = 0
    for page in descendants:
        if not path_contains(page, DATA_DOMAIN_INCLUDED_NODE):
            excluded_count += 1
            continue
        if path_contains(page, DATA_DOMAIN_EXCLUDED_NODE):
            excluded_count += 1
            continue
        ancestors = page.get("ancestors", [])
        parent_id = str(ancestors[-1]["id"]) if ancestors else ""
        page["parentId"] = parent_id
        by_parent.setdefault(parent_id, []).append(page)
        eligible_descendants.append(page)

    leaf_pages = descendant_leaf_pages(str(root_page["id"]), by_parent)
    if page_title:
        normalized_target = normalized_title(page_title)
        leaf_pages = [
            page for page in leaf_pages
            if normalized_title(str(page.get("title", ""))) == normalized_target
        ]

    processed: list[dict[str, Any]] = []
    updated_count = 0
    skipped_count = 0

    for page in leaf_pages:
        title = str(page.get("title", "")).strip()
        page_id = str(page.get("id"))
        page_type = str(page.get("type", "page")).casefold()
        current_body = page.get("body", {}).get("storage", {}).get("value", "")

        if page_type != "page":
            excluded_count += 1
            processed.append(
                {
                    "page_id": page_id,
                    "title": title,
                    "status": "excluded",
                    "changes": [f"skipped because content type is {page_type}, not page"],
                    "sections": [],
                }
            )
            continue

        if is_data_domain_standardized(current_body):
            processed.append(
                {
                    "page_id": page_id,
                    "title": title,
                    "status": "skipped",
                    "changes": ["already follows the required template"],
                    "sections": [],
                }
            )
            skipped_count += 1
            continue

        new_body, changes = build_data_domain_body(page)
        sections = [
            "Overview",
            "Scope",
            "Business Use",
            "KPIs",
            "Grain & Keys",
            "Joins",
            "Important Fields",
            "Data Logic (Raw + Silver + Gold)",
            "Refresh",
            "Notes / Limitations",
        ]

        if not dry_run:
            client.update_page(page_id=page_id, title=title, body=new_body)
            updated_count += 1

        processed.append(
            {
                "page_id": page_id,
                "title": title,
                "status": "dry-run" if dry_run else "updated",
                "changes": changes,
                "sections": sections,
            }
        )

    return {
        "space_name": space.get("name"),
        "space_key": space.get("key"),
        "route": route,
        "dry_run": dry_run,
        "pages_found": len(leaf_pages),
        "pages_processed": len(processed),
        "pages_updated": updated_count,
        "pages_skipped": skipped_count,
        "pages_excluded": excluded_count,
        "pages": processed,
    }


@dataclass
class PageIssue:
    page_id: str
    title: str
    problems: list[str]
    recommended_action: str
    last_updated: str | None


def normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.casefold()).strip()


def strip_known_mirror_prefix(title: str) -> str:
    normalized = normalized_title(title)
    for prefix in ("discovery fase - ", "analytics portal - "):
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip()
    return normalized


def compact_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalized_title(title)).strip()


def split_leading_variant(title: str) -> tuple[str, str] | None:
    match = re.match(r"^([a-z]{2,4})\s*-\s*(.+)$", normalized_title(title))
    if not match:
        return None
    variant, remainder = match.groups()
    if variant not in INTENTIONAL_VARIANT_CODES:
        return None
    return variant, compact_title(remainder)


def split_trailing_variant(title: str) -> tuple[str, str] | None:
    match = re.match(r"^(.+?)\s*-\s*([a-z]{2,4})$", normalized_title(title))
    if not match:
        return None
    remainder, variant = match.groups()
    if variant not in INTENTIONAL_VARIANT_CODES:
        return None
    return compact_title(remainder), variant


def is_domain_variant_pair(title_a: str, title_b: str) -> bool:
    leading_a = split_leading_variant(title_a)
    leading_b = split_leading_variant(title_b)
    if leading_a and leading_b and leading_a[0] != leading_b[0] and leading_a[1] == leading_b[1]:
        return True

    trailing_a = split_trailing_variant(title_a)
    trailing_b = split_trailing_variant(title_b)
    if trailing_a and trailing_b and trailing_a[1] != trailing_b[1] and trailing_a[0] == trailing_b[0]:
        return True

    return False


def is_use_case_reference_pair(title_a: str, title_b: str) -> bool:
    normalized_a = normalized_title(title_a)
    normalized_b = normalized_title(title_b)
    return (
        f"using {normalized_b}" in normalized_a
        or f"with {normalized_b}" in normalized_a
        or f"using {normalized_a}" in normalized_b
        or f"with {normalized_a}" in normalized_b
    )


def is_data_summary_vs_detail_pair(title_a: str, title_b: str) -> bool:
    normalized_a = normalized_title(title_a)
    normalized_b = normalized_title(title_b)

    def generic_data_base(title: str) -> str | None:
        match = re.match(r"^(.+?)\s+data$", title)
        return compact_title(match.group(1)) if match else None

    def detailed_base(title: str) -> str | None:
        match = re.match(r"^(.+?)\s*-\s*(.+)$", title)
        return compact_title(match.group(1)) if match else None

    base_a = generic_data_base(normalized_a)
    base_b = generic_data_base(normalized_b)
    detail_a = detailed_base(normalized_a)
    detail_b = detailed_base(normalized_b)

    return (
        (base_a is not None and detail_b == base_a)
        or (base_b is not None and detail_a == base_b)
    )


def is_intentional_duplicate_pair(title_a: str, title_b: str) -> bool:
    raw_pair = frozenset((title_a.strip(), title_b.strip()))
    if raw_pair in INTENTIONAL_DUPLICATE_TITLE_PAIRS:
        return True

    normalized_pair = frozenset((normalized_title(title_a), normalized_title(title_b)))
    configured_pairs = {
        frozenset(normalized_title(item) for item in pair)
        for pair in INTENTIONAL_DUPLICATE_TITLE_PAIRS
    }
    if normalized_pair in configured_pairs:
        return True

    stripped_a = strip_known_mirror_prefix(title_a)
    stripped_b = strip_known_mirror_prefix(title_b)
    if stripped_a != normalized_title(title_a) and stripped_b != normalized_title(title_b) and stripped_a == stripped_b:
        return True

    if is_domain_variant_pair(title_a, title_b):
        return True

    if is_use_case_reference_pair(title_a, title_b):
        return True

    if is_data_summary_vs_detail_pair(title_a, title_b):
        return True

    for suffix in INTENTIONAL_DETAIL_SUFFIXES:
        if stripped_a.endswith(suffix) and stripped_a[: -len(suffix)].strip() == stripped_b:
            return True
        if stripped_b.endswith(suffix) and stripped_b[: -len(suffix)].strip() == stripped_a:
            return True

    return False


def audit_pages(
    pages: list[dict[str, Any]],
    stale_days: int,
    min_words: int,
    max_paragraph_words: int,
) -> list[PageIssue]:
    issues: list[PageIssue] = []
    now = datetime.now(timezone.utc)
    seen_signatures: list[tuple[str, str, str, str]] = []

    for page in pages:
        page_id = str(page.get("id", ""))
        title = str(page.get("title", "")).strip() or f"Untitled {page_id}"
        updated_at = (
            parse_iso_datetime(page.get("version", {}).get("createdAt"))
            or parse_iso_datetime(page.get("version", {}).get("when"))
            or parse_iso_datetime(page.get("updatedAt"))
            or parse_iso_datetime(page.get("createdAt"))
        )
        body_value = (
            page.get("body", {})
            .get("storage", {})
            .get("value", "")
        )
        plain_text = strip_tags(body_value)
        headings = find_headings(body_value)
        words = plain_text.split()
        problems: list[str] = []
        action = "review"

        if updated_at and updated_at < now - timedelta(days=stale_days):
            problems.append("stale")
            action = "refresh"

        missing_sections = [section for section in REQUIRED_SECTIONS if section not in headings]
        if missing_sections:
            problems.append(f"missing-sections:{', '.join(missing_sections)}")
            action = "rewrite"

        if len(words) < min_words:
            problems.append("thin-content")
            action = "rewrite"

        paragraphs = [
            strip_tags(fragment)
            for fragment in re.findall(r"<p\b[^>]*>(.*?)</p>", body_value, flags=re.I | re.S)
        ]
        long_paragraphs = [p for p in paragraphs if len(p.split()) > max_paragraph_words]
        if long_paragraphs:
            problems.append("unclear-structure")
            action = "rewrite"

        title_signature = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        body_signature = " ".join(words[:120]).casefold()
        duplicate_found = False
        for other_id, other_title_sig, other_body_sig, other_title in seen_signatures:
            if is_intentional_duplicate_pair(title, other_title):
                continue
            if title_signature and title_signature == other_title_sig:
                duplicate_found = True
            elif title_signature and SequenceMatcher(None, title_signature, other_title_sig).ratio() > 0.92:
                duplicate_found = True
            elif body_signature and other_body_sig and SequenceMatcher(None, body_signature, other_body_sig).ratio() > 0.96:
                duplicate_found = True
            if duplicate_found:
                problems.append(f"possible-duplicate:{other_id}")
                action = "consolidate"
                break
        seen_signatures.append((page_id, title_signature, body_signature, title))

        if problems:
            issues.append(
                PageIssue(
                    page_id=page_id,
                    title=title,
                    problems=problems,
                    recommended_action=action,
                    last_updated=updated_at.isoformat() if updated_at else None,
                )
            )
    return issues


def issue_has_duplicate(issue: PageIssue | dict[str, Any]) -> bool:
    problems = issue.problems if isinstance(issue, PageIssue) else issue.get("problems", [])
    return any(problem.startswith("possible-duplicate") for problem in problems)


def issue_has_structure_problem(issue: PageIssue | dict[str, Any]) -> bool:
    problems = issue.problems if isinstance(issue, PageIssue) else issue.get("problems", [])
    return any(
        problem.startswith("missing-sections") or problem in {"thin-content", "unclear-structure"}
        for problem in problems
    )


def issue_has_stale(issue: PageIssue | dict[str, Any]) -> bool:
    problems = issue.problems if isinstance(issue, PageIssue) else issue.get("problems", [])
    return "stale" in problems


def primary_problem_label(issue: PageIssue | dict[str, Any]) -> str:
    if issue_has_duplicate(issue):
        return "duplicada / redundante"
    if issue_has_structure_problem(issue):
        return "mal estructurada"
    if issue_has_stale(issue):
        return "desactualizada"
    return "requiere revisión"


def recommended_action_label(issue: PageIssue | dict[str, Any]) -> str:
    if issue_has_duplicate(issue):
        return "Consolidar con la página canónica"
    if issue_has_structure_problem(issue):
        return "Reescribir con la plantilla estándar"
    if issue_has_stale(issue):
        return "Actualizar contenido, fechas y enlaces"
    return "Revisar manualmente"


def issue_impact_score(issue: PageIssue | dict[str, Any]) -> int:
    problems = issue.problems if isinstance(issue, PageIssue) else issue.get("problems", [])
    score = 0
    if issue_has_duplicate(issue):
        score += 100
    if issue_has_structure_problem(issue):
        score += 70
    if issue_has_stale(issue):
        score += 30
    if "thin-content" in problems:
        score += 15
    if "unclear-structure" in problems:
        score += 10
    for problem in problems:
        if problem.startswith("missing-sections:"):
            missing = [item.strip() for item in problem.split(":", 1)[1].split(",") if item.strip()]
            score += min(len(missing), 4) * 8
    return score


def health_status(problem_pages: int, pages_scanned: int) -> str:
    if pages_scanned == 0 or problem_pages == 0:
        return "bien"
    ratio = problem_pages / pages_scanned
    if ratio <= 0.25:
        return "bien"
    if ratio <= 0.6:
        return "medio"
    return "mal"


def build_audit_report(
    *,
    space: dict[str, Any],
    pages_scanned: int,
    stale_days: int,
    issues: list[PageIssue],
    verbose: bool,
) -> dict[str, Any]:
    sorted_issues = sorted(
        issues,
        key=lambda issue: (
            issue_impact_score(issue),
            issue.last_updated or "",
            issue.title.casefold(),
        ),
        reverse=True,
    )

    grouped = {
        "stale": [issue for issue in sorted_issues if issue_has_stale(issue)],
        "structure": [issue for issue in sorted_issues if issue_has_structure_problem(issue)],
        "duplicate": [issue for issue in sorted_issues if issue_has_duplicate(issue)],
    }

    priority_counts = [
        ("mal estructuradas", len(grouped["structure"])),
        ("desactualizadas", len(grouped["stale"])),
        ("duplicadas / redundantes", len(grouped["duplicate"])),
    ]
    priority_counts = [item for item in priority_counts if item[1] > 0]
    priority_counts.sort(key=lambda item: item[1], reverse=True)

    report = {
        "space_name": space.get("name"),
        "space_key": space.get("key"),
        "pages_scanned": pages_scanned,
        "problem_pages": len(issues),
        "stale_days": stale_days,
        "health_status": health_status(len(issues), pages_scanned),
        "top_problems": [
            {"label": label, "count": count}
            for label, count in priority_counts[:3]
        ],
        "top_pages": [
            {
                "page_id": issue.page_id,
                "title": issue.title,
                "problem": primary_problem_label(issue),
                "recommended_action": recommended_action_label(issue),
                "impact_score": issue_impact_score(issue),
                "last_updated": issue.last_updated,
            }
            for issue in sorted_issues[:8]
        ],
        "groups": {
            "stale": {
                "label": "🔴 Desactualizadas",
                "count": len(grouped["stale"]),
                "examples": [issue.title for issue in grouped["stale"][:3]],
                "action": "Actualizar contenido, fechas y enlaces clave.",
            },
            "structure": {
                "label": "🟠 Mal estructuradas",
                "count": len(grouped["structure"]),
                "examples": [issue.title for issue in grouped["structure"][:3]],
                "action": "Reescribir con la plantilla estándar y bullets claros.",
            },
            "duplicate": {
                "label": "🟡 Duplicadas / redundantes",
                "count": len(grouped["duplicate"]),
                "examples": [issue.title for issue in grouped["duplicate"][:3]],
                "action": "Consolidar y dejar una sola página canónica.",
            },
        },
    }

    if verbose:
        report["issues"] = [
            {
                "page_id": issue.page_id,
                "title": issue.title,
                "problems": issue.problems,
                "recommended_action": issue.recommended_action,
                "last_updated": issue.last_updated,
            }
            for issue in sorted_issues
        ]

    return report


def print_output(data: Any, fmt: str, *, verbose: bool = False) -> None:
    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if isinstance(data, list) and data and isinstance(data[0], dict) and "name" in data[0]:
        print("| Space | Key | ID |")
        print("|---|---|---|")
        for space in data:
            print(f"| {space.get('name', '')} | {space.get('key', '')} | {space.get('id', '')} |")
        return

    if isinstance(data, dict) and "top_pages" in data and "groups" in data:
        print(f"# Audit report for {data['space_name']}")
        print()
        print(f"- Estado general: {str(data.get('health_status', 'medio')).upper()}")
        print(f"- Páginas analizadas: {data['pages_scanned']}")
        print(f"- Páginas con problemas: {data['problem_pages']}")
        top_problems = data.get("top_problems", [])
        if top_problems:
            summary = ", ".join(f"{item['label']} ({item['count']})" for item in top_problems)
            print(f"- Problemas prioritarios: {summary}")
        else:
            print("- Problemas prioritarios: ninguno")
        print()

        print("## Top páginas críticas")
        print()
        for index, page in enumerate(data.get("top_pages", []), start=1):
            print(
                f"{index}. Página: {page['title']} | "
                f"Problema: {page['problem']} | "
                f"Acción recomendada: {page['recommended_action']}"
            )
        if not data.get("top_pages"):
            print()
            print("Sin páginas críticas en el rango analizado.")
        print()

        group_order = ("stale", "structure", "duplicate")
        for group_key in group_order:
            group = data.get("groups", {}).get(group_key)
            if not group or group.get("count", 0) == 0:
                continue
            print(f"## {group['label']} ({group['count']} páginas)")
            examples = group.get("examples", [])
            if examples:
                print(f"- Ejemplos: {', '.join(examples)}")
            print(f"- Acción tipo: {group['action']}")
            print()

        if verbose:
            print("## Detalle completo")
            print()
            for issue in data["issues"]:
                print(f"- {issue['title']} ({issue['page_id']})")
                print(f"  Problemas: {', '.join(issue['problems'])}")
                print(f"  Acción recomendada: {issue['recommended_action']}")
                if issue.get("last_updated"):
                    print(f"  Última edición: {issue['last_updated']}")
                print()
        return

    if isinstance(data, dict) and "pages_processed" in data and "route" in data:
        print("# Data Documentation standardization")
        print()
        print(f"- Modo: {'DRY RUN' if data.get('dry_run') else 'APPLY'}")
        print(f"- Ruta: {data['route']}")
        print(f"- Páginas encontradas: {data['pages_found']}")
        print(f"- Páginas procesadas: {data['pages_processed']}")
        print(f"- Páginas actualizadas: {data['pages_updated']}")
        print(f"- Páginas omitidas: {data['pages_skipped']}")
        print(f"- Páginas excluidas por estar fuera de Data Documentation o dentro de Integration Documentation: {data.get('pages_excluded', 0)}")
        print()
        for index, page in enumerate(data.get("pages", []), start=1):
            change_summary = ", ".join(page.get("changes", [])[:3])
            sections_summary = ", ".join(page.get("sections", [])[:5]) if page.get("sections") else "sin cambios"
            print(
                f"{index}. Página: {page['title']} | "
                f"Acción: {page['status']} | "
                f"Cambios: {change_summary} | "
                f"Secciones completadas o mejoradas: {sections_summary}"
            )
        return

    print(json.dumps(data, indent=2, ensure_ascii=False))


def require_client(args: argparse.Namespace) -> ConfluenceClient:
    env_values = load_env_file(DEFAULT_ENV_FILE)
    token = read_token(Path(args.token_file))
    return ConfluenceClient(
        base_url=resolved_setting(args.base_url, "CONFLUENCE_BASE_URL", env_values),
        cloud_id=args.cloud_id,
        token=token,
        auth_mode=args.auth_mode,
        email=resolved_setting(args.email, "CONFLUENCE_EMAIL", env_values),
    )


def main() -> int:
    args = parse_args()
    try:
        if args.command == "render-template":
            result = {
                "title": args.title,
                "body": render_page_body(args),
            }
            print_output(result, args.format)
            return 0

        client = require_client(args)

        if args.command == "list-spaces":
            spaces = client.list_spaces(limit=args.limit)
            print_output(spaces, args.format)
            return 0

        if args.command == "audit-space":
            space = client.find_space(args.space)
            pages = client.list_pages(str(space["id"]), limit=args.limit)
            issues = audit_pages(
                pages,
                stale_days=args.stale_days,
                min_words=args.min_words,
                max_paragraph_words=args.max_paragraph_words,
            )
            result = build_audit_report(
                space=space,
                pages_scanned=len(pages),
                stale_days=args.stale_days,
                issues=issues,
                verbose=args.verbose,
            )
            print_output(result, args.format, verbose=args.verbose)
            return 0

        if args.command == "create-page":
            space = client.find_space(args.space)
            created = client.create_page(
                space_key=str(space["key"]),
                parent_id=args.parent_id,
                title=args.title,
                body=render_page_body(args),
            )
            print_output(created, args.format)
            return 0

        if args.command == "update-page":
            updated = client.update_page(
                page_id=args.page_id,
                title=args.title,
                body=render_page_body(args),
            )
            print_output(updated, args.format)
            return 0

        if args.command == "fix-docs":
            result = standardize_data_domains(
                client,
                space_identifier=args.space,
                page_title=args.page,
                dry_run=args.dry_run,
            )
            print_output(result, args.format)
            return 0

        raise ConfluenceError(f"Unsupported command: {args.command}")
    except (ConfluenceError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
