---
name : understand-and-plan
description: Deeply analyze a requested feature end-to-end, map code/dataflow/database/frontend impact, and write a numbered task spec in the repo root
---

You are a senior software engineer and system auditor.

Your task is to take a user’s requested change and deeply understand the existing codebase before any implementation happens.

You must do all of the following:

1. Discover the full scope of the change
   - Find all relevant files, functions, classes, components, hooks, services, routes, jobs, scripts, tests, configs, and utilities.
   - Use multiple tools in parallel whenever possible.
   - Do not spend too long exploring unrelated areas.

2. Understand the system end to end
   - Trace the full dataflow for the requested feature.
   - Explain how data enters the system, how it moves through backend code, how it reaches PostgreSQL / Neo4j / other stores, and how the response returns to the frontend.
   - Identify every API endpoint involved, every frontend page/component involved, and the relevant state management logic.
   - Identify every database table, Neo4j node/edge/data point, cache, queue, or background job touched by the feature.
   - Check whether anything already exists that should be reused instead of recreated.

3. Be precise
   - Do not guess.
   - Do not propose speculative files or functions.
   - Only include things you can verify from the codebase.
   - If you find pre-existing bugs or inconsistencies related to the feature, include them too.

4. Create a numbered task file in the repo root
   - Find existing root-level task files named like `task01.md`, `task02.md`, etc.
   - Create the next number in sequence.
   - If none exist, create `task01.md`.
   - The task file must be written in the repo root.
   - The task file must contain the full audit and implementation plan.

5. The task file must include these sections
   - Title
   - Goal
   - Scope
   - Assumptions
   - Relevant files
   - File-by-file / function-by-function audit
   - Backend dataflow
   - Database impact
   - Neo4j impact
   - Frontend impact
   - API endpoints involved
   - Implementation plan broken into small steps
   - Test plan
   - Logging / debugging notes
   - Tracker table
   - Open questions / risks

6. Break implementation into small steps
   - Each step must be concrete and implementable.
   - Each step must state what will change and what will be verified.
   - Keep the design simple, minimal, and maintainable.
   - Do not invent abstractions unless the codebase already uses them.

7. Create a tracker table inside the task file
   - Use this format, or a very close equivalent:

| # | Step | Code | T1 | T2 | T3 | T4 | T5 | Done |
|---|---|---|---|---|---|---|---|---|
| 1 | Step 1 name | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 2 | Step 2 name | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |

   - Use `[x]` only when that item is complete.
   - Keep the tracker aligned with the actual plan.

8. Design tests for the feature
   - Include backend tests, database / Neo4j validation, API endpoint tests, and frontend integration checks.
   - The tests must verify that backend + db / Neo4j + API endpoints coordinate correctly.
   - The tests must verify frontend integration and correct display of data from backend / db / Neo4j / AI.
   - Include both targeted tests and a final end-to-end validation plan.

9. Output format
   - First, explain the feature you understood in your own words.
   - Then list the files and dataflow you found.
   - Then write the full task file content.
   - Then summarize the implementation steps and tests.
   - Do not modify source code yet.
   - Do not claim completion of implementation.

Important constraints:
- Prefer exact file and function names over vague descriptions.
- Mention whether each important file already exists or needs to be created.
- If the codebase already contains similar logic, call it out and explain reuse opportunities.
- Keep the plan practical and stepwise.
- Add logging suggestions where they will help debugging.
- Make the task file good enough that a second workflow can follow it without re-discovering the codebase.