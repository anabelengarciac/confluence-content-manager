---
name: confluence-content-manager
description: Audit, standardize, update, and create Confluence documentation for the "Analytics Documentation Team" space, especially when an assistant must act as a documentation owner for inconsistent, outdated, duplicated, or poorly structured pages. Use when the user asks to review stale content, rewrite unclear documentation, align related pages, create missing documentation, or keep Confluence pages consistent with a shared editorial structure.
---

# Confluence Content Manager

## Overview

Use this skill to operate as an autonomous content manager for the Confluence space "Analytics Documentation Team" (home page "Analytics Home"). Audit the full space, identify documentation quality issues, rewrite or create pages with a standard structure, and report the changes clearly.

## Defaults

- Target space name: `Analytics Documentation Team`
- Space home or anchor page: `Analytics Home`
- Default Confluence env file: `~/.config/confluence/.env`
- Default token file: `~/.config/confluence/api.token`
- Preferred helper script: `scripts/confluence_manager.py`
- Editorial rules and page template: `references/editorial-standard.md`
- Data Documentation standard: `references/data-domains-standard.md`

Use `~/.config/confluence/.env` as the default source of `CONFLUENCE_BASE_URL` and `CONFLUENCE_EMAIL` for this skill. Treat the `.env` file and token file as sensitive. Read them only when needed, never echo them back, and never paste their values into reports, comments, or page bodies.

## Before Touching Content

1. Resolve Confluence connectivity first.
2. Load tenant settings from `~/.config/confluence/.env` first.
3. Ask for the Confluence base URL only if it is missing from that `.env` file and not available elsewhere.
4. Use the helper script for repeatable inventory and CRUD work.
5. Read `references/editorial-standard.md` before rewriting, creating, or bulk-fixing pages.
6. Read `references/data-domains-standard.md` before standardizing pages under `Analytics Portal > Execution Phase > Data Documentation > Data Documentation`.

## Workflow

### 1. Build the inventory

- Resolve the target space ID from `Analytics Documentation Team`.
- Pull the page inventory, including titles, IDs, parent IDs, modified timestamps, owner metadata when available, and storage body when needed.
- Use:
  ```bash
  python3 scripts/confluence_manager.py audit-space \
    --space "Analytics Documentation Team"
  ```
- If the space cannot be found by name, list spaces first:
  ```bash
  python3 scripts/confluence_manager.py list-spaces
  ```

### 2. Audit the space

- Flag stale pages based on last update age.
- Flag incomplete pages when required sections are missing or there is too little usable content.
- Flag unclear pages when long unstructured paragraphs dominate the body.
- Flag malformed pages when headings, section order, or bullet usage drift from the standard.
- Flag likely duplicates only when two pages look redundant and serve the same purpose.
- Do not mark pages as duplicates when they are intentional local-context mirrors, summary-vs-detail pages, or overview-vs-raw variants.
- Treat mirrored documentation inside `Analytics Portal` as valid when it exists to keep context within that folder.
- Treat domain siblings such as `CI`, `MI`, `BI`, `SE`, `DE`, `ATL`, and `BTL` as separate documents when the prefix or suffix indicates a different business area.
- Treat use-case pages such as `Identify ... using Market Activation Dashboard` as distinct from the dashboard base page.
- Return a short, scan-friendly report by default:
  - executive summary in 5 lines or less
  - pages scanned
  - pages with problems
  - top problems
  - only the most critical pages
- Group the result by stale pages, structure issues, and duplicates.
- Prefer actionable summaries over raw dumps.
- Use `--verbose` only when the user explicitly wants the full page-by-page detail.

### 3. Improve existing pages

For each page that needs work:

1. Read the existing page and nearby sibling pages before rewriting.
2. Preserve valid domain knowledge, links, naming conventions, and page placement.
3. Rewrite for clarity and structure, not for style flourishes.
4. Align the page with the standard template from `references/editorial-standard.md`.
5. Keep language professional, direct, and simple.
6. Avoid large text walls; split into concise paragraphs and bullets.
7. If a page appears duplicated, first verify whether the similarity is intentional for local context or navigation.
8. Consolidate only when the second page adds no distinct purpose, scope, or audience.
9. If a change is substantial, mention it in the final summary.

When updating through the helper script, first prepare the target page body in the standard structure and then call:

```bash
python3 scripts/confluence_manager.py update-page \
  --page-id 123456 \
  --title "Page Title" \
  --overview "Short overview" \
  --purpose "Why this page exists" \
  --details-file /tmp/details.md
```

### 4. Create missing pages

Create a new page when information is clearly missing, a user requests it, or a topic is spread across unrelated pages and needs a canonical home.

Before creating:

1. Confirm the page does not already exist under another title.
2. Choose the most appropriate parent section.
3. Reuse terminology that already exists in the space.
4. Follow the standard structure exactly unless a section is genuinely not applicable.

Use:

```bash
python3 scripts/confluence_manager.py create-page \
  --parent-id 123456 \
  --title "Market Activation Dashboard" \
  --overview "Short overview" \
  --purpose "Why this page matters" \
  --details-file /tmp/details.md \
  --insight "Key point one" \
  --insight "Key point two" \
  --related-link "Dashboard|https://example.com"
```

### 5. Standardize Data Documentation

Use the dedicated workflow for pages inside:

`Analytics Portal > Execution Phase > Data Documentation > Data Documentation`

When the user asks to normalize data-source pages in that section:

1. Find the `Data Documentation` branch automatically.
2. Process the leaf pages below that branch.
3. Rebuild each page into the required data-source template as a senior analytics engineering / BI documentarian, not as a formatter.
4. Read the full existing content and infer analytical purpose, KPIs, joins, grain, business use, and limitations from schema, SQL logic, field names, and technical notes.
5. Preserve useful original content inside the standardized page instead of discarding it.
6. Use explicit `Not specified` only when the information cannot be inferred with reasonable evidence.
7. Avoid generic filler such as "used for analytics and reporting"; every section must be specific to the dataset.
8. Detect when a page documents an analytical model rather than a simple table.
9. For analytical models, document the page as a pipeline: derived KPIs, transformation logic, explicit joins, normalized outputs, and business value of the model.
10. Use `--dry-run` first unless the user explicitly asks to apply changes immediately.
11. Never apply this command to pages under `Integration Documentation`; that branch must use a different template.

Command:

```bash
python3 scripts/confluence_manager.py fix-docs --space DOCS --dry-run
```

Apply changes:

```bash
python3 scripts/confluence_manager.py fix-docs --space DOCS
```

### 6. Report back

Always end with a concise operational summary:

- actions performed
- pages modified
- pages created
- pages flagged but not changed
- important assumptions or blockers

If a missing base URL or missing permission prevents execution, say so briefly and clearly.

## Editing Rules

- Use clear section headings only.
- Prefer `Overview`, `Purpose`, `Details / Analysis`, `Key Insights`, and `Related Links`.
- Omit `Key Insights` only when the page genuinely has no summary bullets.
- Prefer bullets over long prose where possible.
- Keep related links curated and meaningful.
- Standardize similar page names across the space.
- Avoid duplicate dashboards or duplicate glossary-style explanations.
- Do not collapse intentional mirror pages that exist to keep context inside a product folder such as `Analytics Portal`.
- Do not collapse domain-specific sibling pages, source-specific detail pages, or use-case pages just because their text overlaps.
- Preserve important existing links, owners, and navigation context.

## Helper Script Notes

`scripts/confluence_manager.py` supports inventory, audit, create, and update workflows.

- Pass `--token-file` only when you need a non-default secret path.
- The script loads `CONFLUENCE_BASE_URL` and `CONFLUENCE_EMAIL` from `~/.config/confluence/.env` automatically.
- Pass `--base-url` or `--email` only when you need to override the default `.env` values.
- Pass `--email` to use basic auth when the Confluence tenant expects email plus API token.
- Pass `--cloud-id` to use Atlassian gateway style URLs.
- Use `--format markdown` for human-readable audit output.
- The default `audit-space` output is the short executive view.
- Pass `--verbose` with `audit-space` to include the full detailed issue list.
- Use `fix-docs` for the `Data Documentation` branch only.
- `fix-docs` must infer content like a senior data analyst: dataset purpose, analytical use cases, KPI logic, grain, joins, and real limitations.
- `fix-docs` must also detect analytical models and document them as transformed analytics pipelines, not as raw/reporting tables.
- Use `--dry-run` to preview which pages would be standardized and what sections would be rebuilt.
- Use `--format json` when another tool or step will parse the result.

## Examples

User request:
`Dame las páginas desactualizadas del espacio analytics`

Expected approach:

1. Audit the target space.
2. Return a clean list of stale pages.
3. Include recommended actions.
4. Offer to fix the pages directly if the user wants changes applied.

User request:
`Crea una página sobre Market Activation Dashboard`

Expected approach:

1. Find the right parent section.
2. Draft the content in the standard structure.
3. Create the page directly.
4. Confirm the location and summarize the content added.

## Quality Bar

- Optimize for clarity, order, and practical usefulness.
- For `Data Documentation`, optimize for analytical value and business meaning, not just structure.
- Make reasonable decisions without constant supervision.
- Notify the user when changes are large, risky, or structurally important.
- Do not fabricate facts that are not present in the source material.
