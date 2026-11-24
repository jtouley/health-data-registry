---
name: find-opportunities
description: "Path B: for a fellow without an area yet, looks for signals in the public data worth a study and returns a short opportunity brief."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Find research opportunities

Help me find a research opportunity. Anything I'm drawn to (a pathogen, a place, a group): [interest, from my message]
(If my message doesn't say it, ask me for it in one short question first.)

1. Run find_research_opportunities for my interest. It is coming and may not be in this server yet; if it isn't, say so plainly and use suggest_datasets and list_sources instead.
2. [Placeholder: checking each candidate signal against GBD and the reported numbers.]
3. [Placeholder: ranking the candidates and asking me which one to refine.]

What we end with: an opportunity brief. Three to five candidate questions, each with the signal behind it (with source and date), the data that could answer it, and what's already published.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
