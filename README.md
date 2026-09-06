# unciv-ai

A planned headless host and MCP adapter for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository currently contains planning documents; the host and adapter have not been implemented.

- [Headless implementation path](docs/Headless-player-agent-path.md): the recommended approach, repository responsibilities, and initial atomic steps.
- [Player API review](docs/Player-agent-API-review.md): the action and information inventory and privacy requirements.
- [Original implementation plan](docs/Player-agent-implementation-plan.md): the broader View-based backlog and linked PR boundaries. The headless path identifies which dependencies can be avoided.

Source links in these documents expect an Unciv checkout at `../Unciv` beside this repository. Each document records the revision it reviewed.
