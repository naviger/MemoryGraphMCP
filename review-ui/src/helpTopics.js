export const helpTopics = {
  home: {
    group: 'Get started', title: 'Home and project scope',
    brief: 'Open a project from the table. Its counts lead to the work behind each number.',
    sections: [
      ['Choose a project', 'Use the project menu in the header to set the scope for Projects, Graph, Ask the Graph, and Search Memory. Choose All projects when you want to work across the graph.'],
      ['Read the Home table', 'Open tasks and review counts lead to their project tabs. Code indexed is the last successful source index. Created and last edited may fall back to the earliest and latest known records.'],
      ['Check CI health', 'Red means a recorded build or test failure. Yellow means warnings or an older tested commit. Green requires a recorded passing build and test run with no warnings. Unknown means the evidence is incomplete.'],
    ], related: ['projects', 'testing', 'account'],
  },
  projects: {
    group: 'Get started', title: 'Project Overview',
    brief: 'See what belongs to this project and use its tabs to explore decisions, source code, memory, or open work.',
    sections: [
      ['Project or repository?', 'A project describes an initiative and its work. A source repository is a codebase. A project can include several repositories, while decisions, goals, and tasks can belong to the project directly.'],
      ['Find project records', 'Search within project filters the inventory. Select a result to preview it in the inspector. Use Open full record for evidence and Explore relationships to see where it fits in the graph.'],
      ['Tracker activity', 'The Overview shows tracker configuration and recently processed events. Open a linked task to see its current status. A stale event is recorded for traceability and leaves the task unchanged.'],
    ], related: ['develop-with-codex', 'decisions', 'code', 'review', 'effectiveness'],
  },
  'develop-with-codex': {
    group: 'Get started', title: 'Develop with Codex',
    brief: 'Use Codex for implementation and review, and record verified outcomes in MemoryGraphMCP.',
    sections: [
      ['Bring the task into Codex', 'In Project Overview, open Tasks and take the task link and acceptance criteria into a Codex session in the source repository. Read AGENTS.md and call get_project_guidance for the selected project. Follow the relevant ADR, constraint, and test citations.'],
      ['Implement and review in Codex', 'Work on a focused branch, run the relevant checks, and review the exact diff or commit. Publishing requires explicit approval. Open Execution history on a task to inspect past agent runs.'],
      ['Record the verified outcome', 'Return to the task and select Update state. When verified, choose Completed and record the commit, checks and results, any unresolved findings, and follow-up links. Link relevant ADRs or evidence through the graph. If work remains, keep the task open and record the blocker in a linked observation.'],
    ], related: ['projects', 'guidance', 'source-control'],
  },
  account: {
    group: 'Get started', title: 'Account and access',
    brief: 'Your account shows the role granted by your sign-in provider. Use the avatar controls to crop and reposition an image.',
    sections: [
      ['Open your account', 'Select your name at the bottom of the sidebar and open Account details. Readers can browse. Writers can make review changes and save project guidance drafts. Admins can publish guidance and manage Global Guidance.'],
      ['Edit your avatar', 'Upload a JPEG, PNG, or WebP up to 10 MB. Drag the preview or adjust the position sliders and zoom. Save the crop when it looks right. The image is stored per account in this browser.'],
      ['Resolve missing permissions', 'Check the Access field. Your identity provider controls the role. After a role change, sign out and sign back in to get a new session.'],
    ], related: ['guidance', 'home'],
  },
  decisions: {
    group: 'Explore knowledge', title: 'Architecture decisions',
    brief: 'Inspect proposals, accepted decisions, replacements, and their evidence. Open Review to work through potential conflicts.',
    sections: [
      ['Read a decision', 'An ADR records what was decided, why, the consequences, and its date. Proposed decisions await acceptance or rejection. A superseded decision can still explain historical behavior.'],
      ['Use validity dates', 'The effective date is inclusive and the expiration date is exclusive. Review deadlines and confidence history track how recently evidence was checked.'],
      ['Follow implementation', 'Open an ADR to see linked services, source files, or tests. Missing links mean the graph has no recorded relationship for that item.'],
    ], related: ['review', 'records', 'graph'],
  },
  code: {
    group: 'Explore knowledge', title: 'Repository and code inventory',
    brief: 'Expand a repository and file to see its symbols. Search for a class, function, or method by name.',
    sections: [
      ['Browse by file', 'The Code tab groups indexed entities by source repository and file. Classes contain their methods. Top-level functions appear under their file.'],
      ['Trace a symbol', 'Open a class, function, endpoint, or test in the inspector. Choose Explore relationships to center Graph on that entity and inspect nearby decisions and dependencies.'],
      ['Handle missing or stale results', 'The inventory reflects the most recent successful index. Check the index date on Home. Older identifiers can still have historical evidence even after a re-index marks their graph entities stale.'],
    ], related: ['graph', 'model', 'source-control'],
  },
  graph: {
    group: 'Explore knowledge', title: 'Graph exploration',
    brief: 'Choose a center node, adjust hops, and select a node or edge to see its details.',
    sections: [
      ['Center the graph', 'Find a node by name, then choose it to set the center. You can also start in a record or inspector and select Explore relationships.'],
      ['Limit the view', 'Hops are the number of edges away from the center. Start with one or two for direct relationships. Use the category and type checkboxes to reduce clutter.'],
      ['Inspect the evidence', 'Select a node to see its details or an edge to see its direction, connected records, confidence, and source. Follow the source before treating an inferred relationship as confirmed.'],
    ], related: ['model', 'records', 'ask'],
  },
  model: {
    group: 'Explore knowledge', title: 'Graph metamodel',
    brief: 'The metamodel lists the entity types the graph stores and the relationships that can connect them.',
    sections: [
      ['Nodes', 'A node represents a typed entity such as a Project, ADR, Service, Repository, SourceFile, or TestSuite. Source repositories represent codebases and persistence repositories represent software components.'],
      ['Edges', 'Edges express relationships such as a service implementing a decision or a test validating a constraint. The inventory below lists the forward and inverse names supported by the running server.'],
      ['Evidence and confidence', 'Edges carry a source, confidence, creation time, and update time. Suggested links appear in review as evidence for consideration. Confirmed links appear in graph traversal.'],
    ], related: ['graph', 'code'],
  },
  ask: {
    group: 'Explore knowledge', title: 'Ask the Graph',
    brief: 'Ask a focused question, choose an as-of date, and follow the citations behind the answer.',
    sections: [
      ['Ask about a named subject', 'Choose a project and name a service, decision, or constraint. For example, ask “What authentication strategy applied on October 1?” Use the date picker to set the day.'],
      ['Check the answer', 'Open the citations beside each finding. Conflicts and gaps identify where the stored evidence needs review or where historical code coverage is incomplete.'],
      ['Choose another view when needed', 'Use Search Memory to find excerpts or Graph to inspect relationships directly. A dated answer describes what the stored evidence supports at that time.'],
    ], related: ['search', 'graph', 'decisions'],
  },
  search: {
    group: 'Explore knowledge', title: 'Search Memory',
    brief: 'Find relevant excerpts with document type and as-of filters. Advanced options explain the search mode and embedding profile.',
    sections: [
      ['Narrow the results', 'Enter a phrase, choose document types, and set an optional date. Each match shows its source and validity dates. Open the result to inspect the evidence or the complete record.'],
      ['Pick a retrieval mode', 'Hybrid combines term and vector matching. Semantic uses stored vector similarity. Check Advanced options to see the mode and whether retrieval fell back when vectors were unavailable or incompatible.'],
      ['Investigate a related record', 'Open Similar records from an eligible detail page for semantic matches. These suggestions describe similarity and do not create graph edges.'],
    ], related: ['ask', 'records', 'token-usage'],
  },
  records: {
    group: 'Explore knowledge', title: 'Record details and the inspector',
    brief: 'Read validity dates, cited evidence, and related records. The inspector gives a quick preview while keeping your place.',
    sections: [
      ['Read the history', 'Record details show the validity window, revalidation history, citations, and changes to confidence where available. Evidence may be a source reference, file, or validation artifact.'],
      ['Explore linked records', 'Open the linked decisions, services, source files, and tests. Explore relationships opens the graph centered on the selected item. Use Back to return to the prior view.'],
      ['Inspect embedding diagnostics', 'Expand diagnostics to check the stored provider, model, version, and dimensions against the active search profile. Similar records loads vector matches on demand.'],
    ], related: ['review', 'search', 'graph'],
  },
  memory: {
    group: 'Explore knowledge', title: 'Project memory',
    brief: 'Browse stored documents associated with this project, including their source and validity dates.',
    sections: [
      ['Find what the project knows', 'Use the project Memory tab to browse its linked and source-associated documents. Apply the project search or an as-of date to narrow the list.'],
      ['Inspect a document', 'Select a record for a preview, then open the full record for validation evidence. Use Search Memory for relevance ranking across text and vectors.'],
    ], related: ['projects', 'search', 'records'],
  },
  review: {
    group: 'Maintain and verify', title: 'Review, revalidation, and ADR conflicts',
    brief: 'Review overdue records and competing decisions. Use cited evidence to revalidate a record or explain a conflict resolution.',
    sections: [
      ['Revalidate a record', 'Open an overdue validation and inspect the evidence. A writer can cite new evidence and optionally update the confidence score. The prior score remains in history, and the validity window stays the same.'],
      ['Resolve an overlapping decision', 'Read both ADRs, their effective dates and overlap, and the evidence. Choose supersede when one replaces the other or coexist when both apply. Enter the reason so later readers can understand the outcome.'],
      ['Review suggested links', 'Automated observations can suggest links to ADRs, goals, and other entities. Review the confidence and citation on each suggestion before relying on the connection.'],
    ], related: ['decisions', 'records', 'effectiveness'],
  },
  testing: {
    group: 'Maintain and verify', title: 'CI testing and fix workflow',
    brief: 'Review the latest CI attempt, last passing run, and recurring failures. Follow the approved workflow for a proposed fix.',
    sections: [
      ['Inspect a run', 'Testing shows the commit, build and test outcomes, warnings, and pass, fail, and skip counts. Open history to compare earlier attempts. The last passing run anchors the most recent verified result.'],
      ['Investigate a failure', 'Expand the test or warning to see its evidence. Linked ADRs and constraints identify relevant architecture requirements. GitHub holds the complete logs.'],
      ['Follow the fix steps', 'Suggest fix proposes an edit. Draft fix produces an isolated diff and checks it. Writers approve applying the verified patch to a branch, publishing a PR, and merging. The workflow waits for passing CI on the merge commit before branch cleanup.'],
    ], related: ['source-control', 'effectiveness'],
  },
  'source-control': {
    group: 'Maintain and verify', title: 'Source Control',
    brief: 'Browse pull requests and commits for a linked repository and open their CI evidence.',
    sections: [
      ['Filter your history', 'Choose a repository within the current project. Pull Requests is the default. Switch to Commits for the default-branch history.'],
      ['Connect changes to testing', 'CI evidence links by commit. Open the run to see the exact attempt. A branch cleanup record appears when the Testing workflow recorded that action.'],
      ['Understand missing entries', 'This read-only view depends on the configured GitHub connection and linked repositories. Actions such as applying or publishing fixes live in Testing.'],
    ], related: ['testing', 'code'],
  },
  guidance: {
    group: 'Maintain and verify', title: 'Global and Project Guidance',
    brief: 'Save proposed instructions as a draft. An admin publishes the saved revision so coding agents can retrieve it.',
    sections: [
      ['Choose the right scope', 'Global Guidance sets deployment-wide instructions. Use the Guidance tab inside a project for local rules such as branch policy or which tests to run.'],
      ['Draft and publish', 'Admins manage Global Guidance. Project writers and admins can save a project draft. An admin approves and publishes the saved revision. Version history records what was published and by whom.'],
      ['Read guidance from an agent', 'AGENTS.md is the repository entry point. Use get_project_guidance to retrieve current global and project instructions, accepted ADRs, and active constraints. The tool exposes published guidance.'],
    ], related: ['develop-with-codex', 'account', 'projects'],
  },
  'token-usage': {
    group: 'Measure outcomes', title: 'Token usage',
    brief: 'Compare estimated context size and savings. Negative savings means the request used more tokens than its document baseline.',
    sections: [
      ['Choose a scope', 'Use a project tab for its own requests or open Token usage from the sidebar for session, project, or lifetime summaries. Project comparison uses the selected project as its primary example.'],
      ['Read estimates', 'Query and context counts are model-independent estimates. Savings compares delivered context with comparable full-document evidence. A blank means there was no baseline, and a negative value means extra tokens.'],
      ['Interpret reported outcomes', 'Response tokens, completions, and retries require client reports. Retry comparisons use only groups with reported outcomes. Expand the detailed measurements to inspect individual receipts.'],
    ], related: ['effectiveness', 'ask', 'search'],
  },
  effectiveness: {
    group: 'Measure outcomes', title: 'Engineering effectiveness',
    brief: 'Compare delivery activity, traceability, graph health, and estimated cost across scopes.',
    sections: [
      ['Set a comparison', 'Pick a project, linked repository, or date range. Use a second scope to compare against a prior period or another repository. Dated activity changes with the filter. Inventory and health are current snapshots.'],
      ['Read the scores', 'Memory looks at documented architecture records. Traceability measures service-to-ADR and constraint-to-test coverage. Validation measures scheduled records that are current. Scores show how many eligible records support them.'],
      ['Check samples and assumptions', 'First-pass success uses attributed changes with reported or matched CI evidence. Estimated cost uses comparable token measurements and the input price you provide. Engineering hours are cited estimates, with their reported confidence.'],
    ], related: ['token-usage', 'testing', 'review'],
  },
};

export function helpHref(topic) {
  return topic ? `/help/${topic}` : '/help';
}

export const projectHelpTopics = {
  Overview: 'projects', Guidance: 'guidance', Decisions: 'decisions', Code: 'code',
  Testing: 'testing', 'Source Control': 'source-control', Memory: 'memory', Review: 'review',
  'Token usage': 'token-usage', Effectiveness: 'effectiveness',
};
