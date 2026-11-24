---
name: scope-an-area
description: "Path A: takes an area or question in your own words and returns the datasets that fit, what each can't answer, and the questions worth refining."
argument-hint: "[your area or question]"
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Scope my area

My area or research question, in my own words: $ARGUMENTS
(If nothing was typed after the command, ask me for it in one short question first.)

1. Use this public health data tool's suggest_datasets and search_datasets, then describe_dataset for the best two or three.
2. For each, tell me in plain words: what one row is, the years and places it covers, what it can't answer and why, and how to cite it. Run list_caveats for anything that matters.
3. Tell me which parts of my question need my hospital's own data, which stays in approved systems and never comes into this chat.
4. [Placeholder: narrowing the area to two or three questions worth refining.]

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
