# Helpers: copy-paste versions

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

If your Claude app shows this tool's prompts in its menu, pick a helper there. If it doesn't, copy the text under a helper, paste it into a new message, and replace anything in [square brackets] with your own words. Never paste patient information.

## Start here: check my tools work

*Step: Check it works.* Tests each connected tool and says in plain words which work and which are missing.

```text
Please check which of my research tools are working in this chat. Try each one with a tiny test and tell me in plain words, one line each, whether it works:
1. This public health data tool: run list_sources.
2. The IHME GBD connector: ask it for the top causes of death in Michigan.
3. PubMed: search for one recent paper on C. difficile.
4. Any others I have connected (Data Commons, OpenEvidence).
For any tool that is missing, give me the exact steps to add it, one step at a time, and remind me that a tool added during a chat only appears in a new chat. Don't use technical words without explaining them in one line.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
```

## Set up my Claude Project

*Step: Set up the Claude Project.* Writes the Project instructions to paste, with the guardrails, and says where to paste them.

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

## Spot a pattern

*Step: Spot a pattern.* For a condition and place, runs GBD then get_series and shows what changed and what the data can't say.

```text
I want to see whether something is changing for [condition] in [place].
1. Use the IHME GBD connector for the modelled trend (with uncertainty ranges) up to 2023.
2. Use this public health data tool's get_series for what was actually reported, including years after 2023 if available, and get_aligned_with_gbd to line the two up.
3. Show one small table side by side, then say in three sentences what changed, whether the change is bigger than GBD's uncertainty range, and what the data can't tell us (case definition changes, provisional years, missing places).
4. Run list_caveats for [condition] and include anything that matters.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
```

## Check it's new

*Step: Check it's new.* Searches PubMed, OpenEvidence if connected, and find_prior_work, then says what's been done and where the gap is.

```text
Has anyone already studied this? My idea: [idea].
Search PubMed (and OpenEvidence if I have it), then this public health data tool's find_prior_work for abstracts, case reports and health department reports PubMed misses. List what has been done (title, year, venue, link), then say in two sentences where the gap is. If a tool isn't connected or its index isn't built yet, say so plainly instead of guessing.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
```

## Find data for my question

*Step: Find data for my question.* Takes the question in the doctor's words and returns the datasets that fit, what each can't answer and how to get it.

```text
My research question, in my own words: [question]
Use this public health data tool's suggest_datasets and search_datasets, then describe_dataset for the best two or three. For each, tell me in plain words: what one row is, the years and places it covers, what it can't answer and why, and how to cite it. Then tell me which parts of my question need my hospital's own data, which stays in approved systems and never comes into this chat.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
```

## Draft my pitch

*Step: Draft the pitch.* Builds a one-page summary with every number's source, case definition and citation, ready for a mentor.

```text
Draft a one-page project pitch for a mentor on: [topic].
Sections: the question; what the data shows (a small table); why it matters; what's already been published; the data I would use and its limits; next steps. Every number must carry its source, the date it was fetched, and its case definition; use get_provenance from this public health data tool for that. Mark anything provisional. End with a citation list. Keep it to one page.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
```

## Something's wrong

*Step: Something's wrong.* Asks what they see, matches it to known problems, and gives the fix one step at a time.

```text
Something isn't working. What I see: [what i see]
Match it to these known problems and give me the fix one step at a time, checking with me after each:
- The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat.
- It says a source has no data yet: whoever runs the server needs to fetch it once; tell me who to ask and what to say.
- It says a place or year isn't covered: run describe_dataset to see what is covered.
- It refused a query: the message says why; ask the same question in plain words instead.
- The app asks for a password or my login: stop; this tool never needs one.
If none match, ask me one question at a time until we find it. Never ask for patient information.
```

## Walk me through an install

*Step: Optional tools that need an install.* Step-by-step help in the Claude Desktop app for the optional tools that need an install.

```text
Walk me through installing [tool] for the Claude Desktop app. I'm a doctor, not a programmer.
Give me one step at a time, tell me what I should see after it, and wait for me to say it worked before the next step. Explain any technical word in one line. Never ask me for a password or to paste a secret into the chat. If a step is blocked on a work computer, suggest I ask my institution's IT team, and tell me what to ask them for. Remind me to quit and reopen Claude Desktop and start a new chat at the end.
```
