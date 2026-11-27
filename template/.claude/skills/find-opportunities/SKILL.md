---
name: find-opportunities
description: "Path B: for a fellow without an area yet, looks for signals in the public data worth a study and returns a short opportunity brief."
argument-hint: "[an interest, a place or leave blank]"
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Find research opportunities

Help me find a research opportunity. Anything I'm drawn to (a pathogen, a place, a group): $ARGUMENTS
(If nothing was typed after the command, ask me for it in one short question first.)

1. Run find_research_opportunities for my interest. It is coming and may not be in this server yet; if it isn't, say so plainly and use suggest_datasets and list_sources instead.
2. For each candidate, put the reported numbers (get_series) beside GBD's modelled trend (get_aligned_with_gbd and the IHME GBD connector) and say what differs, without judging which is right.
3. List the candidates with one line each on why it might be worth a study, and ask me which one to refine. There is no agreed scoring method yet, so don't rank them by a score.

What we end with: an opportunity brief. Three to five candidate questions, each with the signal behind it (with source and date), the data that could answer it, and what's already published.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
