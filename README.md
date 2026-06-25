# Confluence Content Manager Skill

![Status](https://img.shields.io/badge/status-showcase_ready-2ea44f)
![Domain](https://img.shields.io/badge/domain-knowledge_operations-0969da)
![Stack](https://img.shields.io/badge/stack-python_%7C_confluence_api_%7C_docs_automation-6f42c1)

An AI-assisted documentation operations skill for auditing, standardizing, creating, and maintaining Confluence spaces at scale. It combines editorial judgment with API automation so documentation quality can be measured and improved systematically.

## Why It Matters

Documentation debt slows analytics teams down: stale pages, duplicated context, unclear ownership, and inconsistent templates make it harder to trust data products. This skill turns that problem into a repeatable workflow with inventory, quality checks, rewrite guidance, and controlled page updates.

| Business value | Technical value |
| --- | --- |
| Faster onboarding and cleaner knowledge bases | Confluence API client for inventory, audit, create, and update operations |
| More trustworthy analytics documentation | Template validation and stale-page detection |
| Less manual documentation maintenance | Repeatable CLI workflows for space audits and page fixes |
| Better governance without heavy process | Editorial standards encoded as reusable references |

## What It Can Do

- Inventory a Confluence space and collect page metadata.
- Identify stale, incomplete, malformed, unclear, or likely duplicate pages.
- Apply a consistent editorial template while preserving useful context.
- Create new documentation pages from structured inputs.
- Update existing pages through controlled API calls.
- Standardize analytics documentation branches using domain-specific rules.

## Workflow

```mermaid
flowchart TD
    A["Confluence space"] --> B["Inventory pages"]
    B --> C["Audit structure, freshness, clarity"]
    C --> D["Prioritize issues"]
    D --> E["Rewrite or create page content"]
    E --> F["Preview changes"]
    F --> G["Apply controlled update"]
```

## Repository Structure

```text
.
|-- SKILL.md
|-- agents/openai.yaml
|-- references/
|   |-- data-domains-standard.md
|   `-- editorial-standard.md
`-- scripts/confluence_manager.py
```

## Example Commands

```bash
python3 scripts/confluence_manager.py list-spaces
python3 scripts/confluence_manager.py audit-space --space "Analytics Documentation Team"
python3 scripts/confluence_manager.py fix-docs --space DOCS --dry-run
python3 scripts/confluence_manager.py update-page --page-id 123456 --body-file /tmp/page.md
```

## Design Principles

- Preserve valid business knowledge before rewriting.
- Separate audit, preview, and write operations.
- Prefer structured editorial standards over one-off edits.
- Treat credentials and page content as sensitive operational data.
- Report documentation health in a format leaders can scan quickly.

## Skills Demonstrated

`Confluence REST API`  -  `documentation governance`  -  `Python automation`  -  `content operations`  -  `knowledge management`  -  `AI-assisted editing`  -  `workflow design`

## Security

This is a sanitized showcase repository. It contains no Confluence tenant URLs, tokens, emails, or internal page identifiers. Local credentials are expected through environment files outside the repo.
