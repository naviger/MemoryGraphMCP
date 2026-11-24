# Project memory

Use the running MemoryGraphMCP as the development memory for this repository. Use the existing `MemoryGraphMCP` Project node and its linked source Repository. Keep decisions about dependencies or other projects under their own scope.

- Before changing architecture or starting an existing follow-up, consult the [MemoryGraphMCP Project Overview](http://127.0.0.1:3200/projects/230a0395ec1b86f503063b1e1dfdf9503e9309c5eb6054046fcc5e1cc566f97a) or the corresponding graph tools.
- Record material design decisions as ADRs with rationale, consequences, and evidence. Link them to the project. Use proposed status for unresolved decisions. Use explicit effective dates when established and preserve supersession history.
- Create or update linked Task nodes for unfinished work. Close a task after verifying the result. Reuse existing records and stable keys when possible.
- Link implementation and test evidence only when the source supports the relationship. Capture concise conversation summaries when they add useful context.
- Before finishing a development change, verify that relevant memory updates appear under the project. If the running store is unavailable, report which updates are pending.

## Development workflow

Use an interactive Codex session for implementation, testing, and review. Use
MemoryGraphMCP to read project context and record the outcome. The task panel’s
agent execution workflow is optional.

- Work on a focused feature branch based on current `origin/main`. Inspect existing local edits before branching or integrating upstream. Use a separate worktree when needed to preserve the user's checkout.
- Before changing architecture or starting a recorded follow-up, call MemoryGraphMCP's `get_project_guidance` with project key `230a0395ec1b86f503063b1e1dfdf9503e9309c5eb6054046fcc5e1cc566f97a`. Read the published global and project guidance, relevant ADRs, and constraint tests. Use the Project Overview if the tool is unavailable.
- Run the checks that cover the change and report their results. For backend changes use `python -m pytest`. For UI changes run `npm run build` from `review-ui` and verify the affected flow. Run `git diff --check` before committing.
- Review and stage only files and hunks belonging to the task. Commit the verified changes on the feature branch. Keep credentials, runtime data, and unrelated local edits out of the commit.
- Record the verified outcome on the project task. Include the reviewed commit, checks actually run and their results, outstanding findings, and links to relevant ADRs or evidence. Keep the task open if work remains and link any follow-up.
- Get explicit approval before pushing a branch or publishing a pull request. In the approval request, include the branch, commit, proposed PR scope, and checks. Use a separate PR for each independently reviewable change.
