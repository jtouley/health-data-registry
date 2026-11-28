# Helpers: copy-paste versions

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

If your Claude app shows this tool's prompts in its menu, pick a helper there. In a Claude Code project made from the template, type `/` and the helper's name. If neither works, copy the text under a helper, paste it into a new message, and replace anything in [square brackets] with your own words. Never paste patient information.

## Start here: check my tools work

*`/start-here` in Claude Code.* Tests each connected research tool and says in plain words which work and which are missing. Use first, in a new chat or project.

```text
Please check which of my research tools are working in this chat. Try each one with a tiny test and tell me in plain words, one line each, whether it works:

1. This public health data tool: run list_sources.
2. The IHME GBD connector: ask it for the top causes of death in Michigan.
3. PubMed: search for one recent paper on C. difficile.
4. Any others I have connected (Data Commons, OpenEvidence).
5. For any tool that is missing, give me the exact steps to add it, one step at a time, and remind me that a tool added during a chat only appears in a new chat. Don't use technical words without explaining them in one line.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Build my research story

*`/research-story` in Claude Code.* The entry point for a research project: asks whether you already have an area or want one found, then routes you through scoping or finding opportunities, refining a hypothesis and building the story.

```text
I want to build a research story. My area, if I have one: [area]

1. Ask me one question: do I already have an area, or do I want help finding one? Don't go on until I answer.
2. If I have an area, follow the scope-an-area workflow with it (path A).
3. If I want one found, follow the find-opportunities workflow (path B).
4. Then take the idea I choose through the refine-hypothesis workflow until it's test-ready or dropped.
5. Then follow the build-story workflow for the piece I need.
6. Before each change of step, tell me in one line where we are and what comes next.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Scope my area

*`/scope-an-area` in Claude Code.* Path A: takes an area or question in your own words and returns the datasets that fit, what each can't answer, and the questions worth refining.

```text
My area or research question, in my own words: [area]

1. Use this public health data tool's suggest_datasets and search_datasets, then describe_dataset for the best two or three.
2. For each, tell me in plain words: what one row is, the years and places it covers, what it can't answer and why, and how to cite it. Run list_caveats for anything that matters.
3. Tell me which parts of my question need my hospital's own data, which stays in approved systems and never comes into this chat.
4. Suggest two or three questions in this area worth refining, each in one sentence, and ask me which to take to Refine my hypothesis. A worked method for choosing them isn't written yet, so say why you picked each one.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Find research opportunities

*`/find-opportunities` in Claude Code.* Path B: for a fellow without an area yet, looks for signals in the public data worth a study and returns a short opportunity brief.

```text
Help me find a research opportunity. Anything I'm drawn to (a pathogen, a place, a group): [interest]

1. Run find_research_opportunities for my interest. It is coming and may not be in this server yet; if it isn't, say so plainly and use suggest_datasets and list_sources instead.
2. For each candidate, put the reported numbers (get_series) beside GBD's modelled trend (get_aligned_with_gbd and the IHME GBD connector) and say what differs, without judging which is right.
3. List the candidates with one line each on why it might be worth a study, and ask me which one to refine. There is no agreed scoring method yet, so don't rank them by a score.

What we end with: an opportunity brief. Three to five candidate questions, each with the signal behind it (with source and date), the data that could answer it, and what's already published.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Refine my hypothesis

*`/refine-hypothesis` in Claude Code.* The refining loop: states the hypothesis, checks the data and whether it's new, names the confounders and what the data can't say, and revises until it's test-ready, needs revising, or should be dropped.

```text
My idea: [idea]. Help me turn it into a hypothesis I can test, one round at a time.

1. State it as one testable sentence: who, where, when, and what changed. Check it with me.
2. Check the data: the IHME GBD connector for the modelled trend (with uncertainty ranges) up to 2023; this public health data tool's get_series for what was actually reported, including years after 2023 if available, and get_aligned_with_gbd to line the two up. Show one small table side by side and say whether the change is bigger than GBD's uncertainty range. If an answer says 'Page 1 of N', fetch every page and save the rows to one CSV file or artifact first, then build the small table from that file.
3. Check it's new: search PubMed (and OpenEvidence if I have it), then this public health data tool's find_prior_work for abstracts, case reports and health department reports PubMed misses. List what has been done (title, year, venue, link) and say where the gap is. If a tool isn't connected or its index isn't built yet, say so plainly instead of guessing.
4. Name the confounders and what the data can't say (case definition changes, provisional years, missing places); run list_caveats and include anything that matters.
5. Revise the sentence and go round again until we agree it is test-ready, needs another revision, or should be dropped.

What we end with: a hypothesis card. The hypothesis in one sentence, the data that supports it (each number with its source and date), what's already published, the confounders, what the data can't say, and a verdict: test-ready, revise or drop.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Write my pitch or abstract

*`/build-story` in Claude Code.* Turns a refined hypothesis into a one-page pitch, an IDWeek abstract or a poster outline; for a case report, writes only the population-context paragraph.

```text
Help me build my story on: [topic]

1. Ask me which piece I need: a one-page pitch, an IDWeek abstract, a poster outline or a case report.
2. For a pitch, use these sections: the question; what the data shows (a small table); why it matters; what's already been published; the data I would use and its limits; next steps. Keep it to one page. Abstract and poster templates aren't written yet: for those, say so and draft the pitch only, which I can adapt.
3. Every number must carry its source, the date it was fetched, and its case definition; use get_provenance from this public health data tool for that. Mark anything provisional.
4. For a case report, write only the population-context paragraph and leave the case itself as "[your case, written outside this chat]". Never ask about the patient.
5. End with a citation list.

What we end with: the piece I asked for (a one-page pitch, an IDWeek abstract or a poster outline), with every number's source, date and case definition, and a citation list.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Which format should I aim for?

*`/choose-format` in Claude Code.* Asks five short questions about your project and returns which output fits now (abstract or poster, research letter, full manuscript or viewpoint), how settled that is, and what would change it.

```text
Help me decide which output to aim for with this project: [project]
Don't guess my answers from that sentence: ask me each question below, one at a time, and wait for my answer.

1. Ask what kind of work it is, and offer exactly these answers: empirical (new numbers from data), methods or standard (a method or standard others can reuse), argument (a position, not new data) or case (one case or a small series).
2. Ask how far along the data is: idea, preliminary counts, frozen dataset or adjudicated (frozen, with clinician adjudication done).
3. Ask how many worked examples the piece needs to show in full, and whether a reader needs a methods section to trust the numbers (yes or no).
4. Ask whether I have a target conference and how many weeks remain before its deadline, and which matters most to me, in order, from: speed, citable record, reach, effort. Both are optional.
5. Run choose_output_format with my answers. Show the chosen format, each format's probability of ranking first, the confidence and the review advice. Say that confidence means how settled the choice is, not whether it is right, and that this is a decision under my stated priorities, not a prediction that the work will be published. If it says too close to call, show what would change it first.
6. List the criteria it couldn't use and why, and the rules still awaiting the maintainer's review. If I named a conference, run get_publication_outlook for it and show how often its abstracts became full papers.
7. Never use this to rank ideas, fellows or projects. Suggest I take the result to my mentor, especially if the advice says to discuss it.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Something's wrong

*`/help-me-fix-it` in Claude Code.* Asks what you see, matches it to known problems, and gives the fix one step at a time.

```text
Something isn't working. What I see: [what i see]
Match it to these known problems and give me the fix one step at a time, checking with me after each:

1. The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat.
2. It says the open data hub could not be reached: the hub didn't answer just then. Check my internet connection and try again in a few minutes, or ask a WHO question, which is read live from WHO.
3. It says the open data hub has no published release yet: the hub this tool is set to read has never published any data, so trying again in a few minutes won't help. Nothing was guessed. Ask a WHO question meanwhile, and tell whoever installed it or report it on GitHub.
4. It says a place or year isn't covered: run describe_dataset to see what is covered.
5. It refused a query: the message says why; ask the same question in plain words instead.
6. It says 'Page 1 of N': nothing is wrong, the answer is long. Ask for every page with the Next line it gives, and save all the rows to one CSV file or artifact. If a later page says the data changed, start again from page 1.
7. The app asks for a password or my login: stop; this tool never needs one.
8. If none match, ask me one question at a time until we find it. Never ask for patient information.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
```

## Walk me through an install

*Claude chat only.* Step-by-step help installing a tool in the Claude Desktop app, including this one.

```text
Walk me through installing [tool] for the Claude Desktop app. I'm a doctor, not a programmer.
Give me one step at a time, tell me what I should see after it, and wait for me to say it worked before the next step. Explain any technical word in one line. Never ask me for a password or to paste a secret into the chat. If a step is blocked on a work computer, suggest I ask my institution's IT team, and tell me what to ask them for. Remind me to quit and reopen Claude Desktop and start a new chat at the end.
```

## Set up my Claude Project

*Claude chat only.* Writes the Project instructions to paste, with the guardrails, and says where to paste them.

```text
Help me set up a Claude Project for my research. Write out the Project instructions for me to paste, then tell me exactly where to paste them (click by click). The instructions must say:
- Use the IHME GBD connector for modelled burden, this public health data tool for observed and local data and datasets, PubMed for papers, Data Commons for harmonised statistics and OpenEvidence for clinical evidence, if I have them.
- Cite every number to the tool it came from, with its source and date. Never fill a number from memory or the news.
- Report GBD's 95% uncertainty intervals; observed counts have none, so say so.
- Flag provisional, suppressed or partial-geography numbers (for example, Out-Wayne excludes Detroit).
- Don't compare age-standardised rates that use different standard populations.
- Population patterns are hypotheses, not causes. No patient information in any chat.
Keep it short enough to paste in one go.
```
