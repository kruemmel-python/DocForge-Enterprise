# DocForge Enterprise v0.5 Profile & Transparency Model

DocForge Enterprise v0.5 introduces three documentation profiles and explicit work estimation.

## Profiles

| Profile | Behavior | Intended use |
|---|---|---|
| `quick` | single-pass final documentation, no LLM module reduction | samples, smoke tests, small projects |
| `balanced` | reduced chapter plan, normal file/module reductions | normal projects |
| `enterprise` | full chapter plan, one LLM call per chapter | large enterprise documentation runs |

## Why this matters

A small project with three files may produce more than twenty model requests in full Enterprise mode because the pipeline performs:

1. shard analysis
2. file reduction
3. module reduction
4. chapter rendering
5. retrieval embeddings before shard and chapter prompts

`quick` reduces this dramatically by using one final rendering call and skipping LLM module reduction.

## CLI examples

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --embedded-mycelia --force-rebuild
```

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile balanced --embedded-mycelia --force-rebuild
```

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --embedded-mycelia --force-rebuild
```

## Estimate-only mode

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

This writes an output document containing the estimated work without calling LM Studio.

## Custom chapters

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --chapters "Executive Summary,Systemüberblick,Sicherheitsbetrachtung" --embedded-mycelia --force-rebuild
```

## WebGUI

The WebGUI exposes:

- execution mode
- documentation profile
- custom chapter list
- single-pass final rendering
- module reduction toggle
- estimate-only mode
- model names
- timeout budgets
- worker limits
