# AI EXECUTION CONTRACT

Read this file first.

## Operator rules
- Terminal-first.
- Output exact commands in order.
- No GUI/manual steps unless impossible.
- Fix root cause before enhancement.
- Preserve working logic unless replacing it with a clearly better full solution.
- Keep it direct and compact.

## Required behavior
- Continue from MANUAL FOLLOW-UP first.
- Then inspect AUTO SNAPSHOT.
- Run the fastest valid validation/build/test.
- Fix the real blocker.
- End with the next concrete step.

## Repo
- Name: trading-bot
- Path: C:\Users\jvben\Desktop\LIVE-projects\trading-bot

## AUTO SNAPSHOT
- Git: yes
- Branch: main-clean
- Dirty files: 14
- Changed:
  -  M .github/copilot-instructions.md
  -  M AGENTS.md
  -  M CLAUDE.md
  -  M GEMINI.md
  -  M openclaw_v1/server.py
  -  M openclaw_v1/shared/persistence/state.py
  -  M openclaw_v1/spot_aggro/api/routes.py
  -  M openclaw_v1/spot_aggro/engine.py
  -  M openclaw_v1/spot_aggro/governance/pre_trade_gov.py
  -  M openclaw_v1/spot_aggro/tests/test_phase11n9r_mobile_overlay.py
  -  M web/ops/index.html
  - ?? openclaw_v1/spot_aggro/governance/contradiction_freeze.py
  - ?? openclaw_v1/spot_aggro/governance/economic_truth_gov.py
  - ?? openclaw_v1/spot_aggro/tests/test_phase11n9y_gate_enforcement.py
- Stack: Node
- Scripts:
  - npm run android:assemble
  - npm run android:assemble:third-party
  - npm run android:bundle:release
  - npm run android:format
  - npm run android:install
  - npm run android:install:third-party
  - npm run android:lint
  - npm run android:lint:android
  - npm run android:run
  - npm run android:run:third-party
  - npm run android:test
  - npm run android:test:integration
- Stack: Python (pyproject.toml)
- Deploy: Cloudflare/Wrangler
- CI: GitHub Actions present
- Recent files:
  -  M .github/copilot-instructions.md
  -  M AGENTS.md
  -  M CLAUDE.md
  -  M GEMINI.md
  -  M openclaw_v1/server.py
  -  M openclaw_v1/shared/persistence/state.py
  -  M openclaw_v1/spot_aggro/api/routes.py
  -  M openclaw_v1/spot_aggro/engine.py
  -  M openclaw_v1/spot_aggro/governance/pre_trade_gov.py
  -  M openclaw_v1/spot_aggro/tests/test_phase11n9r_mobile_overlay.py
  -  M web/ops/index.html
  - ?? openclaw_v1/spot_aggro/governance/contradiction_freeze.py

## MANUAL FOLLOW-UP
# CURRENT FOLLOW-UP

Status: auto
Objective: continue from the current repo state
Next step: inspect changed files, run the fastest valid validation, fix the root cause, then continue
Constraints: terminal-first, minimal steps, full implementation

## Execution order
1. Read MANUAL FOLLOW-UP.
2. Inspect the changed/recent files.
3. Run the smallest command that proves the blocker.
4. Repair it.
5. Re-run validation.
6. Continue to the next blocker without restarting discovery.

