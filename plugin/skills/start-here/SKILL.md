---
name: start-here
description: "Tests each connected research tool and says in plain words which work and which are missing. Use first, in a new chat or project."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Start here: check my tools work

Please check which of my research tools are working in this chat. Try each one with a tiny test and tell me in plain words, one line each, whether it works:

1. This public health data tool: run list_sources.
2. The IHME GBD connector: ask it for the top causes of death in Michigan.
3. PubMed: search for one recent paper on C. difficile.
4. Any others I have connected (Data Commons, OpenEvidence).
5. For any tool that is missing, give me the exact steps to add it, one step at a time, and remind me that a tool added during a chat only appears in a new chat. Don't use technical words without explaining them in one line.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
