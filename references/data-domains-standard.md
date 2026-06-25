# Data Documentation Standard

Use this reference when standardizing pages under:

`Analytics Portal > Execution Phase > Data Documentation > Data Documentation`

## Goal

Convert inconsistent data-source pages into a single documentation format without losing useful content.

## Required Structure

Every page must use this section order:

1. `Overview`
2. `Scope`
3. `Business Use`
4. `KPIs`
5. `Grain & Keys`
6. `Joins`
7. `Important Fields`
8. `Data Logic (Raw + Silver + Gold)`
9. `Refresh`
10. `Notes / Limitations`

## Rules

- Keep the page title as the data-source name.
- Reorganize existing content into the template.
- Work as a senior analytics engineering / BI documentarian, not as a formatter.
- Infer analytical purpose, KPI candidates, joins, grain, and business use from the schema, SQL logic, field names, and technical notes when the page provides enough evidence.
- Detect when the page documents an analytical model with transformations, multiple joins, or derived KPI logic, and document it as a pipeline rather than as a simple source table.
- Do not invent critical facts.
- Use `Not specified` only when a section cannot be inferred responsibly.
- Avoid generic filler such as "used for analytics and reporting" or "helps business decisions".
- Preserve original useful context when the current page contains details that do not map cleanly.
- Prefer specific analytical explanations over literal restatement of field names.
- For analytical models, prioritize derived KPIs, explicit joins, normalized outputs, FX or inflation logic, and the business value of the transformation.
- Prefer a dry run before applying live updates.
- Avoid deleting existing tables, examples, or technical notes if they still add value.
- Only use this standard for pages inside `Data Documentation`.
- Do not apply this standard to pages under `Integration Documentation`.

## Expected Command

Preview:

```bash
python3 scripts/confluence_manager.py fix-docs --space DOCS --dry-run
```

Apply:

```bash
python3 scripts/confluence_manager.py fix-docs --space DOCS
```
