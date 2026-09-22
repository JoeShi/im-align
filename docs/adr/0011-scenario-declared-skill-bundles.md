# Declare Skill Bundle Sources in Scenarios

Status: accepted.

A Scenario is the self-contained declaration of one alignment case: requirement, user brief, Spec Root, expected artifacts, and the entry Alignment Skill. The last piece was not actually self-contained. A Scenario named only a bare Skill (e.g. `skill: grill-with-docs`); the real Skill Bundle — source archive URL, pinned tag, `npx skills` installer version, and the explicit Skill closure — lived in a separate global manifest (`tests/e2e/skill-bundles.yaml`), and `load_bundle_for_skill` reverse-looked up the bundle by testing whether the Scenario's Skill name appeared in some bundle's `skills` list. That indirection had three defects: the Scenario-to-bundle association was guessed by name membership rather than declared, so two Scenarios naming the same Skill could not use different sources or versions; the acquisition metadata was divorced from the case definition that needs it, so a Scenario could not be read and reproduced on its own; and the pinned versions were global, so no two Scenarios could exercise different versions of the same Skill. L3/L4 real-backend runs need the bundle installed before the Bridge Session starts (the fake backend used by L2 ignores the field entirely, just as L3/L4 ignore the L2 transcript).

## Considered Options

- Keep the global manifest and name-membership lookup: rejected — two sources of truth with a guessed join, name collisions across Scenarios, and globally pinned versions.
- Encode the source as a CLI-style string (`fission-ai/openspec --skill openspec-propose`): rejected as a configuration shape — it parses like a command but cannot structurally carry the pin and installer metadata (`ref`, `cli` version) that reproducibility requires.
- Put a structured Skill mapping in each Scenario and delete the global manifest (chosen): one fact per case, versions pinned per case, no registry to drift.

## Decision

`scenario.yaml` declares the whole bundle; `skill:` becomes a mapping:

```yaml
skill:
  name: grill-with-docs        # entry Skill, forwarded to `bridge start --skill`
  source: mattpocock/skills    # repo shorthand or full archive URL
  ref: v1.2.3                  # tag or full SHA of the archive
  cli: "1.5.9"                 # `npx skills@<cli>` installer version
  bundle:                      # explicit Skill closure, entry Skill first
    - grill-with-docs
    - grilling
    - domain-modeling
```

- `tests/e2e/skill-bundles.yaml` is deleted. No global manifest, no reverse lookup.
- `skill_bundle.py` stops resolving by name: `install_skill_bundle(workspace, skill_mapping)` validates that `name` is a member of `bundle` (the ADR-0009 closure lesson is enforced structurally) and runs the same `npx skills add <source>` install for every supported Agent Backend path.
- `smoke_runner.py` passes the Scenario's mapping straight through; the Bridge still receives only the entry Skill name.
- One shape only: the legacy bare-string `skill:` form is rejected by the Scenario schema with a clear error. One in-tree Scenario exists; a loud migration error is better than silent dual-shape support.

## Consequences

- A Scenario directory plus this repo is sufficient to reproduce an L3/L4 run — no sidecar manifest to consult.
- Adding a Scenario never touches shared files, so Scenario PRs stay conflict-free.
- Scenarios sharing one upstream bundle duplicate the source/ref/cli lines; accepted while the count is small (YAML anchors or a template step can be introduced later without changing the schema).
- `docs/e2e-harness-design.md` and `docs/test/test-layers.md` describe the per-Scenario declaration; the CI smoke workflow comment no longer points at the deleted manifest.
- L2 continues to ignore the mapping: the fake backend never installs a bundle, so the field is inert there by design (symmetric to how L3/L4 ignore the L2 transcript).
