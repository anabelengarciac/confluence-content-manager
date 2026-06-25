# Analytics Documentation Team Editorial Standard

Use this reference whenever you rewrite, normalize, or create Confluence pages in the "Analytics Documentation Team" space.

## Standard Page Structure

Every page should follow this order:

1. Title
2. Overview
3. Purpose
4. Details / Analysis
5. Key Insights
6. Related Links

## Section Guidance

### Overview

- Explain the page in 2 to 4 sentences.
- State what the page covers and who should use it.
- Avoid jargon if a simpler wording works.

### Purpose

- State why the page exists.
- Mention the decision, workflow, dashboard, or process it supports.
- Keep it brief.

### Details / Analysis

- Organize the main content into subsections with `h2` or `h3`.
- Use bullets, small tables, or short paragraphs.
- Prefer explicit labels such as `Data source`, `Refresh cadence`, `Owner`, `Definitions`, `How to use`, `Known limitations`.

### Key Insights

- Include only when the page benefits from a short takeaway list.
- Use clear bullets.
- Keep them concrete and decision-oriented.

### Related Links

- Add links to dashboards, source pages, JIRA tickets, repositories, or adjacent Confluence pages.
- Keep link labels meaningful.
- Remove broken or duplicate-looking links when you can verify they are obsolete.

## Formatting Rules

- Use clear headings.
- Avoid long paragraphs.
- Prefer bullets over dense prose.
- Keep tone professional and simple.
- Reuse existing terminology consistently.
- Standardize names across similar pages.

## Audit Heuristics

Use these heuristics when deciding whether a page needs attention:

- `stale`: not updated in the configured threshold window
- `missing-sections`: missing one or more required sections
- `unclear`: too much dense prose, too little structure, or unclear purpose
- `thin-content`: very little useful content beyond a title and a few lines
- `possible-duplicate`: strong title or body similarity with another page
- `taxonomy-drift`: naming or hierarchy inconsistent with neighboring pages

Do not treat all similar pages as duplicates. A page can be intentionally similar when:

- it mirrors core project context inside a specific folder such as `Analytics Portal`
- it is an overview page while another page is a lower-level `Raw` or `Overview` document
- it is a domain-specific sibling such as `CI`, `MI`, `BI`, `SE`, `DE`, `ATL`, or `BTL`
- it is a use-case page built on top of a base dashboard or product page
- it exists for a different audience, workflow step, or navigation path

Only mark `possible-duplicate` when the two pages appear to serve the same purpose and one of them adds no distinct value.

## Recommended Actions

- `refresh`: update facts, dates, links, or owners
- `rewrite`: keep the topic, improve clarity and structure
- `consolidate`: merge overlapping pages and retain one canonical page
- `relocate`: move the page under a more appropriate parent
- `create`: add a missing page in the right section

## Safe Editing Rules

- Preserve page intent unless the user asks for a stronger rewrite.
- Preserve valid links and owner information.
- Preserve important historical context when it explains current decisions.
- Remove redundant filler text.
- Do not invent metrics, owners, or process details.
