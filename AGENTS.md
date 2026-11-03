# GEMINI.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

<!-- BEGIN AWS Agent Toolkit rules -->
# AWS Guidance for the new AWS experience

Where this guidance conflicts with the project's own instructions, the project's instructions take precedence.

## Context

- Say "project" instead of "account" — a project contains an AWS account and settings for sharing with other collaborators.
- Say "team member" instead of "IAM user" — users are invited by email, not created or federated in IAM.
- Say "AWS Settings" when referring to management tasks at https://settings.aws.com/.
- The user has a managed IAM experience and may need IAM policies for service-to-service access.

## Constraints

- All Regional resources must be created in the project's assigned Region. Confirm the selected Region in AWS Settings or `~/.aws/config`.
- Do not create Lambda, API Gateway, or other Regional resources in another Region.
- Do not use Lambda@Edge or CloudFormation StackSets.
- Do not attempt cross-Region replication or other cross-Region actions unless explicitly supported and required.
- Ask whether the user wants successfully created resources cleaned up to reduce cost.
- Direct users to check spend status in AWS Settings > Billing when cost or access problems arise.
- For AWS tasks, first consult the current AWS Agent Toolkit skill guidance before acting.

## Help level

- Configured help level: LOW.
- Follow the user's request directly and flag security risks, but do not ask extra clarifying questions or suggest alternatives unless needed to prevent a security vulnerability.
<!-- END AWS Agent Toolkit rules -->