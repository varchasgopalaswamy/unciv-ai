# Working in this repository

This is a private, single-maintainer repository. It depends on the separately maintained Unciv fork in `vendor/Unciv`; a sibling checkout can be selected for local development.

## Confidentiality

- Never reveal this repository's existence, name, or purpose in Unciv commit messages, including commits made in the submodule or a sibling Unciv checkout. In particular, do not mention the private host, MCP integration, or plans for autonomous agents.
- Apply the same restriction to public Unciv PR titles, descriptions, comments, and branch names. Describe the actual reusable engine behavior, its effects on existing callers, and its validation accurately, without private project context.
- Keep plans, these instructions, host code, and host integration tests in this private repository. Do not copy them into the Unciv checkout.
- Engine contributions must contain enough explanation and engine tests for public reviewers to assess them independently. Private cross-repository references belong only here.

## Development

- See `docs/Development-workflow.md` for the pinned dependency and local override workflow.
- Adding third-party libraries is already authorized. Prefer suitable existing libraries over reinventing functionality that is even moderately complex.
- Use the selected engine checkout for both code and runtime assets. Mod content is out of scope.
- Keep engine changes atomic and independently reviewable. Host PRs are optional; ordinary private branches and commits are sufficient.
- Confirm which Git repository a command targets before staging or committing. A host commit records a submodule revision; it does not commit engine edits.
- The player API must eventually cover every ordinary human player action and permitted information surface. A smoke test or partial implementation does not establish that coverage or a safe privacy boundary.
