---
name: refine-hypothesis
description: "The refining loop: states the hypothesis, checks the data and whether it's new, names the confounders and what the data can't say, and revises until it's test-ready, needs revising, or should be dropped."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Refine my hypothesis

My idea: [idea, from my message]. Help me turn it into a hypothesis I can test, one round at a time.
(If my message doesn't say it, ask me for it in one short question first.)

1. State it as one testable sentence: who, where, when, and what changed. Check it with me.
2. Check the data: the IHME GBD connector for the modelled trend (with uncertainty ranges) up to 2023; this public health data tool's get_series for what was actually reported, including years after 2023 if available, and get_aligned_with_gbd to line the two up. Show one small table side by side and say whether the change is bigger than GBD's uncertainty range. If an answer says 'Page 1 of N', fetch every page and save the rows to one CSV file or artifact first, then build the small table from that file.
3. Check it's new: search PubMed (and OpenEvidence if I have it), then this public health data tool's find_prior_work for abstracts, case reports and health department reports PubMed misses. List what has been done (title, year, venue, link) and say where the gap is. If a tool isn't connected, say so plainly instead of guessing. find_prior_work says how fully each conference year is indexed: a year still being indexed is not 'no match'; say so.
4. Name the confounders and what the data can't say (case definition changes, provisional years, missing places); run list_caveats and include anything that matters.
5. Revise the sentence and go round again until we agree it is test-ready, needs another revision, or should be dropped.

What we end with: a hypothesis card. The hypothesis in one sentence, the data that supports it (each number with its source and date), what's already published, the confounders, what the data can't say, and a verdict: test-ready, revise or drop.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
