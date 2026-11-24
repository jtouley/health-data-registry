"""Helpers: one ready-made starter per step a clinician goes through, from setup to the pitch.

Each helper is an MCP prompt (it shows in Claude's prompt menu once the connector is added) and the same
text is published in docs/helpers.md as a copy-paste version for apps that don't show prompts. The text is
written for doctors: plain words, one step at a time, and never asks for patient information or passwords.
`make docs` (scripts/write_helpers_doc.py) regenerates docs/helpers.md from this file; a test checks they match.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Helper:
    name: str
    title: str
    step: str
    description: str
    text: str  # may contain {placeholders} named in `args`
    args: tuple[str, ...] = ()

    def render(self, **values: str) -> str:
        filled = {a: (values.get(a) or f"[{a.replace('_', ' ')}]") for a in self.args}
        return self.text.format(**filled)


RULES = (
    "Rules for this whole conversation: cite every number to the tool it came from, with its source and date; "
    "never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that "
    "observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me "
    "for, or accept, patient information."
)

HELPERS: tuple[Helper, ...] = (
    Helper(
        name="start_here",
        title="Start here: check my tools work",
        step="Check it works",
        description="Tests each connected tool and says in plain words which work and which are missing.",
        text=(
            "Please check which of my research tools are working in this chat. Try each one with a tiny test and "
            "tell me in plain words, one line each, whether it works:\n"
            "1. This public health data tool: run list_sources.\n"
            "2. The IHME GBD connector: ask it for the top causes of death in Michigan.\n"
            "3. PubMed: search for one recent paper on C. difficile.\n"
            "4. Any others I have connected (Data Commons, OpenEvidence).\n"
            "For any tool that is missing, give me the exact steps to add it, one step at a time, and remind me "
            "that a tool added during a chat only appears in a new chat. Don't use technical words without "
            "explaining them in one line.\n\n" + RULES
        ),
    ),
    Helper(
        name="set_up_my_project",
        title="Set up my Claude Project",
        step="Set up the Claude Project",
        description="Writes the Project instructions to paste, with the guardrails, and says where to paste them.",
        text=(
            "Help me set up a Claude Project for my research. Write out the Project instructions for me to paste, "
            "then tell me exactly where to paste them (click by click). The instructions must say:\n"
            "- Use the IHME GBD connector for modelled burden, this public health data tool for observed and local "
            "data and datasets, PubMed for papers, Data Commons for harmonised statistics and OpenEvidence for "
            "clinical evidence, if I have them.\n"
            "- Cite every number to the tool it came from, with its source and date. Never fill a number from "
            "memory or the news.\n"
            "- Report GBD's 95% uncertainty intervals; observed counts have none, so say so.\n"
            "- Flag provisional, suppressed or partial-geography numbers (for example, Out-Wayne excludes Detroit).\n"
            "- Don't compare age-standardised rates that use different standard populations.\n"
            "- Population patterns are hypotheses, not causes. No patient information in any chat.\n"
            "Keep it short enough to paste in one go."
        ),
    ),
    Helper(
        name="spot_a_pattern",
        title="Spot a pattern",
        step="Spot a pattern",
        description="For a condition and place, runs GBD then get_series and shows what changed and what the data can't say.",
        args=("condition", "place"),
        text=(
            "I want to see whether something is changing for {condition} in {place}.\n"
            "1. Use the IHME GBD connector for the modelled trend (with uncertainty ranges) up to 2023.\n"
            "2. Use this public health data tool's get_series for what was actually reported, including years "
            "after 2023 if available, and get_aligned_with_gbd to line the two up.\n"
            "3. Show one small table side by side, then say in three sentences what changed, whether the change "
            "is bigger than GBD's uncertainty range, and what the data can't tell us (case definition changes, "
            "provisional years, missing places).\n"
            "4. Run list_caveats for {condition} and include anything that matters.\n\n" + RULES
        ),
    ),
    Helper(
        name="check_its_new",
        title="Check it's new",
        step="Check it's new",
        description="Searches PubMed, OpenEvidence if connected, and find_prior_work, then says what's been done and where the gap is.",
        args=("idea",),
        text=(
            "Has anyone already studied this? My idea: {idea}.\n"
            "Search PubMed (and OpenEvidence if I have it), then this public health data tool's find_prior_work "
            "for abstracts, case reports and health department reports PubMed misses. List what has been done "
            "(title, year, venue, link), then say in two sentences where the gap is. If a tool isn't connected or "
            "its index isn't built yet, say so plainly instead of guessing.\n\n" + RULES
        ),
    ),
    Helper(
        name="find_data_for_my_question",
        title="Find data for my question",
        step="Find data for my question",
        description="Takes the question in the doctor's words and returns the datasets that fit, what each can't answer and how to get it.",
        args=("question",),
        text=(
            "My research question, in my own words: {question}\n"
            "Use this public health data tool's suggest_datasets and search_datasets, then describe_dataset for the "
            "best two or three. For each, tell me in plain words: what one row is, the years and places it covers, "
            "what it can't answer and why, and how to cite it. Then tell me which parts of my question need my "
            "hospital's own data, which stays in approved systems and never comes into this chat.\n\n" + RULES
        ),
    ),
    Helper(
        name="draft_my_pitch",
        title="Draft my pitch",
        step="Draft the pitch",
        description="Builds a one-page summary with every number's source, case definition and citation, ready for a mentor.",
        args=("topic",),
        text=(
            "Draft a one-page project pitch for a mentor on: {topic}.\n"
            "Sections: the question; what the data shows (a small table); why it matters; what's already been "
            "published; the data I would use and its limits; next steps. Every number must carry its source, the "
            "date it was fetched, and its case definition; use get_provenance from this public health data tool "
            "for that. Mark anything provisional. End with a citation list. Keep it to one page.\n\n" + RULES
        ),
    ),
    Helper(
        name="help_me_fix_it",
        title="Something's wrong",
        step="Something's wrong",
        description="Asks what they see, matches it to known problems, and gives the fix one step at a time.",
        args=("what_i_see",),
        text=(
            "Something isn't working. What I see: {what_i_see}\n"
            "Match it to these known problems and give me the fix one step at a time, checking with me after each:\n"
            "- The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat.\n"
            "- It says a source has no data yet: whoever runs the server needs to fetch it once; tell me who to "
            "ask and what to say.\n"
            "- It says a place or year isn't covered: run describe_dataset to see what is covered.\n"
            "- It refused a query: the message says why; ask the same question in plain words instead.\n"
            "- The app asks for a password or my login: stop; this tool never needs one.\n"
            "If none match, ask me one question at a time until we find it. Never ask for patient information."
        ),
    ),
    Helper(
        name="walk_me_through_install",
        title="Walk me through an install",
        step="Optional tools that need an install",
        description="Step-by-step help in the Claude Desktop app for the optional tools that need an install.",
        args=("tool",),
        text=(
            "Walk me through installing {tool} for the Claude Desktop app. I'm a doctor, not a programmer.\n"
            "Give me one step at a time, tell me what I should see after it, and wait for me to say it worked "
            "before the next step. Explain any technical word in one line. Never ask me for a password or to "
            "paste a secret into the chat. If a step is blocked on a work computer, suggest I ask my "
            "institution's IT team, and tell me what to ask them for. Remind me to quit and reopen Claude Desktop "
            "and start a new chat at the end."
        ),
    ),
)


def get(name: str) -> Helper:
    for h in HELPERS:
        if h.name == name:
            return h
    raise KeyError(name)


def markdown() -> str:
    """docs/helpers.md: the copy-paste versions, generated from HELPERS so the two never drift."""
    out = [
        "# Helpers: copy-paste versions",
        "",
        "<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->",
        "",
        "If your Claude app shows this tool's prompts in its menu, pick a helper there. If it doesn't, copy the "
        "text under a helper, paste it into a new message, and replace anything in [square brackets] with your "
        "own words. Never paste patient information.",
        "",
    ]
    for h in HELPERS:
        out += [
            f"## {h.title}",
            "",
            f"*Step: {h.step}.* {h.description}",
            "",
            "```text",
            h.render(),
            "```",
            "",
        ]
    return "\n".join(out)
