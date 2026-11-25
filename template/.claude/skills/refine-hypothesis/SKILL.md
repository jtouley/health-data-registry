---
name: refine-hypothesis
description: "The refining loop: states the hypothesis, checks the data and whether it's new, names the confounders and what the data can't say, and revises until it's test-ready, needs revising, or should be dropped."
argument-hint: "[your idea in a sentence]"
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Refine my hypothesis

My idea: $ARGUMENTS. Help me turn it into a hypothesis I can test, one round at a time.
(If nothing was typed after the command, ask me for it in one short question first.)

1. State it as one testable sentence: who, where, when, and what changed. Check it with me.
2. Check the data: the IHME GBD connector for the modelled trend (with uncertainty ranges) up to 2023; this public health data tool's get_series for what was actually reported, including years after 2023 if available, and get_aligned_with_gbd to line the two up. Show one small table side by side and say whether the change is bigger than GBD's uncertainty range.
3. Check it's new: search PubMed (and OpenEvidence if I have it), then this public health data tool's find_prior_work for CROI conference abstracts PubMed misses (it covers CROI only). List what has been done (title, year, venue, link) and say where the gap is. If a tool isn't connected, its index isn't loaded, or a year isn't fully indexed, say so plainly instead of guessing.
4. Name the confounders and what the data can't say (case definition changes, provisional years, missing places); run list_caveats and include anything that matters.
5. Revise the sentence and go round again until we agree it is test-ready, needs another revision, or should be dropped.

What we end with: a hypothesis card. The hypothesis in one sentence, the data that supports it (each number with its source and date), what's already published, the confounders, what the data can't say, and a verdict: test-ready, revise or drop.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
