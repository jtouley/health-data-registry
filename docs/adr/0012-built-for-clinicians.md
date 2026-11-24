# 0012. Built for clinicians: helpers as MCP prompts, with copy-paste docs

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Built for clinicians"; "README plan"; "Setting up the tools alongside (for clinicians)"; "Spec changes"
  (audience)

## Context

The users are doctors, not developers. They already use Claude, but a terminal, a config file or an error code
stops most of them. The fellows' guide showed the flow works when each step is spelled out. Some Claude apps may
not show a connector's prompts: whether Claude on the web and on mobile do is not yet checked.

## Decision

- **Writing rules** for everything a clinician reads (README, setup guide, tool descriptions, helpers, error
  messages): plain words, a technical term explained in one line or avoided; one action per step, then what they
  should see, then what to do if they don't; a screenshot for every click in the setup guides; every message in
  clinical words with a next step, never a stack trace or an HTTP code; never put patient information in the chat.
- **The main path needs no terminal.** Anything that does is marked optional and a helper walks through it. Until
  the server is hosted ([ADR 0011](0011-local-stdio-first.md)) adding this tool is itself a one-time install, and
  the README says so plainly.
- **One helper per step,** shipped as MCP prompts: `start_here`, `set_up_my_project`, `spot_a_pattern`,
  `check_its_new`, `find_data_for_my_question`, `draft_my_pitch`, `help_me_fix_it`, `walk_me_through_install`.
- **Copy-paste versions** of the same helpers are in [docs/helpers.md](../helpers.md), generated from
  `src/health_data_registry/helpers.py` by `make docs`; a test fails if they differ.
- **Separate audiences.** The README and [docs/setup.md](../setup.md) are for clinicians; developer material is in
  [docs/dev/](../dev/README.md) and `CONTRIBUTING.md`.
- **Release gate:** before each release, two or three clinicians with no technical background start from the README,
  get set up and finish one fellow's-path question, unaided, while we watch. Anywhere they get stuck is fixed
  before release. The setup target is under 15 minutes.

## Consequences

- `server.py` turns any unexpected failure into a plain-words refusal; tool descriptions are written for the model a
  doctor is talking to.
- Helper text has one source, so the menu version and the copy-paste version never drift.
- Screenshots must be refreshed each release.
- The one-time install is the weakest step until hosting exists, and the usability gate will measure it.
