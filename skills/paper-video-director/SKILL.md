---
name: paper-video-director
description: Turn research papers and their author projects into evidence-linked Chinese explainer videos, with project discovery, story development, claim-matched visuals, configurable narration, and rendered-output review. Use for paper-video production or improving the research-video workflow in 视频工坊.
---

# Paper Video Director

Produce an explanation a non-specialist can follow, balancing readability, depth and viewing pace. For research, use **常见方法、具体问题、本文解决什么、方法怎样起作用、效果如何** as coverage questions, not a fixed order or equal time allocation. Give enough prior context to understand the selected contribution; merge or reorder material to suit the source, audience and runtime. `problem_solution` is optional. 视频工坊 also accepts articles, supplied scripts and media: follow their own purpose and structure, and preserve supplied narration unless the user requests rewriting.

Read [the production contract](references/production.md) before researching or writing. 视频工坊 loads the same contract for its Codex and API backends; its input-routing section also covers articles, supplied scripts and media. Read [the review rubric](references/review.md) when checking plans or final renders.

Use locally available paper-lookup and PDF skills for identifier resolution and difficult PDF extraction when needed; their availability is not assumed by the standalone program. Never claim an installed skill was executed merely because its principles were included in a prompt.

In this repository run `studio_server.py` for the interface. Research, sources, narration, plans and reviews belong in isolated `jobs/` directories. Keep application source separate from private inputs, generated media, credentials and local environments. Preserve existing user projects when changing the program.

The program's deterministic checks, source manifest and rendered-video reviews are the stable contract. Model providers are interchangeable only if they support the required images and structured output. A schema passing does not establish scientific accuracy. Keep example-based regressions across provider changes; do not promise identical quality across models.

Use public author materials and record attribution and reuse status. Do not execute retrieved repositories. Missing pages, failed searches, inaccessible videos and unknown media rights remain explicit gaps. Do not infer reproduction success from reading code. Natural speech means specific writing, meaningful pauses and suitable pacing; it does not mean concealing required disclosures.
