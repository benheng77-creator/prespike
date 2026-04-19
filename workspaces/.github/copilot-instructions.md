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
- Name: workspaces
- Path: C:\Users\jvben\Desktop\LIVE-projects\workspaces

## AUTO SNAPSHOT
- Git: yes
- Branch: HEAD
- Dirty files: 90
- Changed:
  - ?? ../../../.adal/
  - ?? ../../../.agents/
  - ?? ../../../.aider.chat.history.md
  - ?? ../../../.aider.input.history
  - ?? ../../../.aider/
  - ?? ../../../.augment/
  - ?? ../../../.bob/
  - ?? ../../../.cache/
  - ?? ../../../.cagent/
  - ?? ../../../.clasprc.json
  - ?? ../../../.claude.json
  - ?? ../../../.claude/
  - ?? ../../../.cline/
  - ?? ../../../.codebuddy/
  - ?? ../../../.codeium/
  - ?? ../../../.codex/
  - ?? ../../../.commandcode/
  - ?? ../../../.config/
  - ?? ../../../.continue/
  - ?? ../../../.copilot/
- CI: GitHub Actions present
- Recent files:
  - ?? ../../../.adal/
  - ?? ../../../.agents/
  - ?? ../../../.aider.chat.history.md
  - ?? ../../../.aider.input.history
  - ?? ../../../.aider/
  - ?? ../../../.augment/
  - ?? ../../../.bob/
  - ?? ../../../.cache/
  - ?? ../../../.cagent/
  - ?? ../../../.clasprc.json
  - ?? ../../../.claude.json
  - ?? ../../../.claude/

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

