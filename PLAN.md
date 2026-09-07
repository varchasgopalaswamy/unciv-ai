# Shared player API for autonomous and human Unciv play

  ## Summary

  Build a player-bound API that exposes everything an ordinary human player may inspect or do, using structured observations and validated commands. Progressively migrate the existing
  desktop UI to that same API.

  Keep Unciv’s implemented mechanics, correcting fairness defects where necessary. Complete coverage of bundled Vanilla and Gods & Kings remains the final objective; the first milestone is
  a small Vanilla duel playable by two Codex agents or a human and a Codex agent.

  ## Architecture and contracts

  - Shared engine layer: Put reusable player queries, action eligibility, and game operations in the Unciv fork. Keep orchestration, authentication, MCP, agent isolation, and integration
    tests in this private repository.

  - Authoritative execution: Serialize observations, commands, turn transitions, and saves through one game executor. Extract this ownership from the current headless bootstrap so both
    headless and desktop launches can use it. Run one authoritative game per process.

  - Player identity: Bind identity when establishing a session. Commands cannot select another acting civilization. Keep agent-controlled major civilizations in human-controlled engine
    slots so built-in AI does not take their turns.

  - Observations: Return immutable, player-filtered data, including inspectable details, tooltips, previews, notifications, and public rules. Support bulk queries without requiring UI
    navigation. Preserve displayed precision and distinguish current visibility from remembered information.

  - Commands: Expose gameplay operations rather than clicks. Share eligibility and execution with UI callers, including mandatory choices and human-accessible automation. Revalidate
    against current state; advertised options are not authorization.

  - Consistency: Extend the prototype’s epochs and retry receipts with snapshot revisions and reconnect-stable request deduplication. Use stable player-scoped entity references and
    expiring decision references. Errors and option discovery must not disclose hidden state.

  - Human UI: Migrate queries and commands together for each supported slice. Keep rendering, navigation, and presentation preferences local. Existing View classes are candidates for
    reuse, but require auditing before serving as information boundaries.

  ## Implementation milestones

  1. Establish the coverage contract. Create a private inventory mapping ordinary screens, tooltips, previews, notifications, and actions to API coverage, shared engine behavior, and
     tests. Include diplomacy, city management, units, research, policies, great people, religion, espionage, victories, and turn blockers. Record suspected leaks separately from
     intentional public information.

  2. Complete the shared execution foundation. Generalize the existing session facade and runtime; migrate the current prototype operations into shared engine behavior. Add capability
     discovery, pending decisions, turn readiness, validated end-turn, and match lifecycle control. Separate operator-only create/load/save/pause functions from player tools.

  3. Deliver the Vanilla duel. Default to two major civilizations, Ancient start, Quick speed, a small land map, no city-states, barbarians, ruins, or mods, and domination plus time
     victory. Implement exploration, settlement, economy, production, research, policies, units, improvements, combat, promotions, diplomacy between the players, triggered choices, and
     victory handling. Migrate corresponding desktop UI paths alongside them. Reject unsupported configurations at launch; unexpected unsupported decisions pause with operator diagnostics.

  4. Integrate Codex and recovery. Build a Kotlin supervisor that launches one isolated Codex client per player, resumes its explicit conversation ID each turn, and preserves private
     notes. Use noninteractive execution and the authenticated MCP endpoint; these integration points are supported by Codex execution documentation and MCP configuration.

     Use separate rootless Podman containers with private storage and explicit network access to model services and the game endpoint. Exclude host saves, repositories, operator
     credentials, other players’ storage, inherited plugins, and unrelated tools. Configure model and execution limits per player. Determine turn completion from host state, not agent
     prose.

  5. Expand to complete parity. Remove first-milestone restrictions through additional vertical slices, completing Vanilla before Gods & Kings additions. Every slice includes API behavior,
     UI integration, privacy tests, and inventory updates. Add player-addressed communication without exposing private conversations. Completion requires closing every inventory entry, not
     merely demonstrating successful matches.

  ## Validation and recovery

  - Compare UI and API observations and resulting game state on equivalent fixtures, controlling randomness where necessary.
  - Test paired game states that differ only in hidden information: player-visible responses, errors, previews, and available options must remain equivalent.
  - Exercise foreign references, expired decisions, stale snapshots, duplicate requests, reconnects, out-of-turn commands, elimination, and every mandatory decision reachable in supported
    setups.

  - Verify containers cannot access host saves, other players’ files or credentials, or operator endpoints.
  - Persist consistent game checkpoints and command receipts together. Test process termination around command acknowledgement; reconnect must never duplicate a committed action.
  - On uncertain mutation failure, halt and restore the last consistent checkpoint. On agent timeout or budget exhaustion, pause without automatically ending its turn or substituting AI.
  - Require automated scripted match tests, a complete Codex-versus-Codex match, and a human-versus-Codex desktop acceptance run. Keep model-dependent matches outside routine CI.

  ## Defaults and boundaries

  - Local Linux deployment, sequential turns, one controller per civilization, and one game per host process initially.
  - Human participation uses the existing desktop UI with an embedded authoritative host; remote desktop networking is deferred.
  - Agents may retain previously observed information and compute from it. The host supplies no undisclosed strategic analysis or hidden numerical detail.
  - Operator logs may contain full state but remain inaccessible to players. Player histories contain only permitted observations.
  - Engine contributions remain atomic and independently reviewable, with public descriptions and tests that contain no private project context.
