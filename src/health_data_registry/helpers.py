"""Workflows: the clinician's research story, step by step, from checking the tools to the pitch.

One record per workflow is the single source of truth for three outputs:

- the MCP prompts that server.py serves (each shows in Claude's prompt menu once the connector is added);
- docs/helpers.md, the copy-paste versions for apps that don't show prompts;
- the Claude Code skills in the fellow's project template, template/.claude/skills/<name>/SKILL.md.

`make docs` (scripts/write_template.py) writes the last two; tests fail if any of them drift from these records.
The text is written for doctors: plain words, one step at a time, and never asks for patient information or
passwords.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

Artifact = Literal["none", "hypothesis_card", "opportunity_brief", "pitch"]
Surface = Literal["prompt", "doc", "skill"]

# Tools a workflow may name before the server has them. Every other tool must be in the server's tool list.
COMING_TOOLS: frozenset[str] = frozenset({"find_research_opportunities"})

SKILLS_DIR = "template/.claude/skills"
DOC_PATH = "docs/helpers.md"

RULES = (
    "Rules for this whole conversation: cite every number to the tool it came from, with its source and date; "
    "never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that "
    "observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me "
    "for, or accept, patient information."
)

ARTIFACT_TEXT: dict[Artifact, str] = {
    "none": "",
    "hypothesis_card": (
        "What we end with: a hypothesis card. The hypothesis in one sentence, the data that supports it (each "
        "number with its source and date), what's already published, the confounders, what the data can't say, "
        "and a verdict: test-ready, revise or drop."
    ),
    "opportunity_brief": (
        "What we end with: an opportunity brief. Three to five candidate questions, each with the signal behind "
        "it (with source and date), the data that could answer it, and what's already published."
    ),
    "pitch": (
        "What we end with: the piece I asked for (a one-page pitch, an IDWeek abstract or a poster outline), with "
        "every number's source, date and case definition, and a citation list."
    ),
}


@dataclass(frozen=True)
class Step:
    """One plain-words step; `tools` are the MCP tool names of this server that the step uses."""

    text: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    name: str  # skill name (lowercase, hyphens); the MCP prompt name is the same with underscores
    title: str
    description: str
    argument_hint: str
    intro: str  # may contain the {placeholder} named in `args`
    steps: tuple[Step, ...]
    artifact: Artifact = "none"
    args: tuple[str, ...] = ()
    surfaces: frozenset[Surface] = field(default_factory=lambda: frozenset({"prompt", "doc", "skill"}))
    rules: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", self.name) or len(self.name) > 64:
            raise ValueError(
                f"workflow name {self.name!r}: lowercase letters, digits and single hyphens only"
            )
        if len(self.args) > 1:
            raise ValueError(f"{self.name}: a skill takes its input as one free-text argument")
        if "skill" in self.surfaces and not self.rules:
            raise ValueError(f"{self.name}: every skill ends with RULES")

    @property
    def prompt_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def tools(self) -> tuple[str, ...]:
        """Every tool the workflow names, in step order, without repeats."""
        return tuple(dict.fromkeys(t for s in self.steps for t in s.tools))

    def render(self, **values: str) -> str:
        """The prompt text: intro, numbered steps, what we end with, then the rules."""
        filled = {a: (values.get(a) or f"[{a.replace('_', ' ')}]") for a in self.args}
        return self._body(self.intro.format(**filled))

    def skill_body(self) -> str:
        """The same text for a Claude Code skill: the argument comes from what's typed after the command."""
        intro = self.intro.format(**dict.fromkeys(self.args, "$ARGUMENTS"))
        if self.args:
            intro += "\n(If nothing was typed after the command, ask me for it in one short question first.)"
        return self._body(intro)

    def _body(self, intro: str) -> str:
        parts = [intro.rstrip()]
        if self.steps:
            parts.append("\n".join(f"{i}. {s.text}" for i, s in enumerate(self.steps, 1)))
        if self.artifact != "none":
            parts.append(ARTIFACT_TEXT[self.artifact])
        if self.rules:
            parts.append(RULES)
        return "\n\n".join(parts)


WORKFLOWS: tuple[Workflow, ...] = (
    Workflow(
        name="start-here",
        title="Start here: check my tools work",
        description=(
            "Tests each connected research tool and says in plain words which work and which are missing. "
            "Use first, in a new chat or project."
        ),
        argument_hint="",
        intro=(
            "Please check which of my research tools are working in this chat. Try each one with a tiny test and "
            "tell me in plain words, one line each, whether it works:"
        ),
        steps=(
            Step("This public health data tool: run list_sources.", ("list_sources",)),
            Step("The IHME GBD connector: ask it for the top causes of death in Michigan."),
            Step("PubMed: search for one recent paper on C. difficile."),
            Step("Any others I have connected (Data Commons, OpenEvidence)."),
            Step(
                "For any tool that is missing, give me the exact steps to add it, one step at a time, and remind "
                "me that a tool added during a chat only appears in a new chat. Don't use technical words without "
                "explaining them in one line."
            ),
        ),
    ),
    Workflow(
        name="research-story",
        title="Build my research story",
        description=(
            "The entry point for a research project: asks whether you already have an area or want one found, "
            "then routes you through scoping or finding opportunities, refining a hypothesis and building the "
            "story."
        ),
        argument_hint="[your area, or leave blank]",
        args=("area",),
        intro="I want to build a research story. My area, if I have one: {area}",
        steps=(
            Step(
                "Ask me one question: do I already have an area, or do I want help finding one? Don't go on "
                "until I answer."
            ),
            Step("If I have an area, follow the scope-an-area workflow with it (path A)."),
            Step("If I want one found, follow the find-opportunities workflow (path B)."),
            Step(
                "Then take the idea I choose through the refine-hypothesis workflow until it's test-ready or dropped."
            ),
            Step("Then follow the build-story workflow for the piece I need."),
            Step("Before each change of step, tell me in one line where we are and what comes next."),
        ),
    ),
    Workflow(
        name="scope-an-area",
        title="Scope my area",
        description=(
            "Path A: takes an area or question in your own words and returns the datasets that fit, what each "
            "can't answer, and the questions worth refining."
        ),
        argument_hint="[your area or question]",
        args=("area",),
        intro="My area or research question, in my own words: {area}",
        steps=(
            Step(
                "Use this public health data tool's suggest_datasets and search_datasets, then describe_dataset "
                "for the best two or three.",
                ("suggest_datasets", "search_datasets", "describe_dataset"),
            ),
            Step(
                "For each, tell me in plain words: what one row is, the years and places it covers, what it can't "
                "answer and why, and how to cite it. Run list_caveats for anything that matters.",
                ("list_caveats",),
            ),
            Step(
                "Tell me which parts of my question need my hospital's own data, which stays in approved systems "
                "and never comes into this chat."
            ),
            Step("[Placeholder: narrowing the area to two or three questions worth refining.]"),
        ),
    ),
    Workflow(
        name="find-opportunities",
        title="Find research opportunities",
        description=(
            "Path B: for a fellow without an area yet, looks for signals in the public data worth a study and "
            "returns a short opportunity brief."
        ),
        argument_hint="[an interest, a place or leave blank]",
        args=("interest",),
        intro="Help me find a research opportunity. Anything I'm drawn to (a pathogen, a place, a group): {interest}",
        steps=(
            Step(
                "Run find_research_opportunities for my interest. It is coming and may not be in this server "
                "yet; if it isn't, say so plainly and use suggest_datasets and list_sources instead.",
                ("find_research_opportunities", "suggest_datasets", "list_sources"),
            ),
            Step("[Placeholder: checking each candidate signal against GBD and the reported numbers.]"),
            Step("[Placeholder: ranking the candidates and asking me which one to refine.]"),
        ),
        artifact="opportunity_brief",
    ),
    Workflow(
        name="refine-hypothesis",
        title="Refine my hypothesis",
        description=(
            "The refining loop: states the hypothesis, checks the data and whether it's new, names the "
            "confounders and what the data can't say, and revises until it's test-ready, needs revising, or "
            "should be dropped."
        ),
        argument_hint="[your idea in a sentence]",
        args=("idea",),
        intro="My idea: {idea}. Help me turn it into a hypothesis I can test, one round at a time.",
        steps=(
            Step("State it as one testable sentence: who, where, when, and what changed. Check it with me."),
            Step(
                "Check the data: the IHME GBD connector for the modelled trend (with uncertainty ranges) up to "
                "2023; this public health data tool's get_series for what was actually reported, including years "
                "after 2023 if available, and get_aligned_with_gbd to line the two up. Show one small table side "
                "by side and say whether the change is bigger than GBD's uncertainty range.",
                ("get_series", "get_aligned_with_gbd"),
            ),
            Step(
                "Check it's new: search PubMed (and OpenEvidence if I have it), then this public health data "
                "tool's find_prior_work for abstracts, case reports and health department reports PubMed misses. "
                "List what has been done (title, year, venue, link) and say where the gap is. If a tool isn't "
                "connected or its index isn't built yet, say so plainly instead of guessing.",
                ("find_prior_work",),
            ),
            Step(
                "Name the confounders and what the data can't say (case definition changes, provisional years, "
                "missing places); run list_caveats and include anything that matters.",
                ("list_caveats",),
            ),
            Step(
                "Revise the sentence and go round again until we agree it is test-ready, needs another revision, "
                "or should be dropped."
            ),
        ),
        artifact="hypothesis_card",
    ),
    Workflow(
        name="build-story",
        title="Build my story",
        description=(
            "Turns a refined hypothesis into a one-page pitch, an IDWeek abstract or a poster outline; for a case "
            "report, writes only the population-context paragraph."
        ),
        argument_hint="[pitch, abstract, poster or case report: and the topic]",
        args=("topic",),
        intro="Help me build my story on: {topic}",
        steps=(
            Step(
                "Ask me which piece I need: a one-page pitch, an IDWeek abstract, a poster outline or a case report."
            ),
            Step(
                "For a pitch, use these sections: the question; what the data shows (a small table); why it "
                "matters; what's already been published; the data I would use and its limits; next steps. Keep it "
                "to one page. [Placeholder: abstract and poster structures.]"
            ),
            Step(
                "Every number must carry its source, the date it was fetched, and its case definition; use "
                "get_provenance from this public health data tool for that. Mark anything provisional.",
                ("get_provenance",),
            ),
            Step(
                "For a case report, write only the population-context paragraph and leave the case itself as "
                '"[your case, written outside this chat]". Never ask about the patient.'
            ),
            Step("End with a citation list."),
        ),
        artifact="pitch",
    ),
    Workflow(
        name="help-me-fix-it",
        title="Something's wrong",
        description="Asks what you see, matches it to known problems, and gives the fix one step at a time.",
        argument_hint="[what you see]",
        args=("what_i_see",),
        intro=(
            "Something isn't working. What I see: {what_i_see}\n"
            "Match it to these known problems and give me the fix one step at a time, checking with me after each:"
        ),
        steps=(
            Step(
                "The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat."
            ),
            Step(
                "It says a source has no data yet: whoever runs the server needs to fetch it once; tell me who to "
                "ask and what to say."
            ),
            Step(
                "It says a place or year isn't covered: run describe_dataset to see what is covered.",
                ("describe_dataset",),
            ),
            Step("It refused a query: the message says why; ask the same question in plain words instead."),
            Step("The app asks for a password or my login: stop; this tool never needs one."),
            Step(
                "If none match, ask me one question at a time until we find it. Never ask for patient information."
            ),
        ),
    ),
    Workflow(
        name="walk-me-through-install",
        title="Walk me through an install",
        description="Step-by-step help in the Claude Desktop app for the optional tools that need an install.",
        argument_hint="[the tool to install]",
        args=("tool",),
        intro=(
            "Walk me through installing {tool} for the Claude Desktop app. I'm a doctor, not a programmer.\n"
            "Give me one step at a time, tell me what I should see after it, and wait for me to say it worked "
            "before the next step. Explain any technical word in one line. Never ask me for a password or to "
            "paste a secret into the chat. If a step is blocked on a work computer, suggest I ask my "
            "institution's IT team, and tell me what to ask them for. Remind me to quit and reopen Claude Desktop "
            "and start a new chat at the end."
        ),
        steps=(),
        surfaces=frozenset({"prompt", "doc"}),
        rules=False,
    ),
    Workflow(
        name="set-up-my-project",
        title="Set up my Claude Project",
        description="Writes the Project instructions to paste, with the guardrails, and says where to paste them.",
        argument_hint="",
        intro=(
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
        steps=(),
        surfaces=frozenset(
            {"prompt", "doc"}
        ),  # Claude chat only: a Claude Code project has the template instead
        rules=False,
    ),
)

PROMPTS: tuple[Workflow, ...] = tuple(w for w in WORKFLOWS if "prompt" in w.surfaces)
SKILLS: tuple[Workflow, ...] = tuple(w for w in WORKFLOWS if "skill" in w.surfaces)


def get(name: str) -> Workflow:
    """A workflow by its skill name or its MCP prompt name."""
    for w in WORKFLOWS:
        if name in (w.name, w.prompt_name):
            return w
    raise KeyError(name)


def skill_md(w: Workflow) -> str:
    """template/.claude/skills/<name>/SKILL.md for one workflow. Frontmatter values are JSON strings, valid YAML."""
    front = [f"name: {w.name}", f"description: {json.dumps(w.description)}"]
    if w.argument_hint:
        front.append(f"argument-hint: {json.dumps(w.argument_hint)}")
    return "\n".join(
        [
            "---",
            *front,
            "---",
            "",
            "<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->",
            "",
            f"# {w.title}",
            "",
            w.skill_body(),
            "",
        ]
    )


def markdown() -> str:
    """docs/helpers.md: the copy-paste versions, generated from WORKFLOWS so the two never drift."""
    out = [
        "# Helpers: copy-paste versions",
        "",
        "<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->",
        "",
        "If your Claude app shows this tool's prompts in its menu, pick a helper there. In a Claude Code project "
        "made from the template, type `/` and the helper's name. If neither works, copy the text under a helper, "
        "paste it into a new message, and replace anything in [square brackets] with your own words. Never paste "
        "patient information.",
        "",
    ]
    for w in WORKFLOWS:
        if "doc" not in w.surfaces:
            continue
        where = f"`/{w.name}` in Claude Code" if "skill" in w.surfaces else "Claude chat only"
        out += [f"## {w.title}", "", f"*{where}.* {w.description}", "", "```text", w.render(), "```", ""]
    return "\n".join(out)


def generated_files() -> dict[str, str]:
    """Every generated file, by its path from the repo root."""
    files = {f"{SKILLS_DIR}/{w.name}/SKILL.md": skill_md(w) for w in SKILLS}
    files[DOC_PATH] = markdown()
    return files
