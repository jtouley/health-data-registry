---
name: help-me-fix-it
description: "Asks what you see, matches it to known problems, and gives the fix one step at a time."
argument-hint: "[what you see]"
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Something's wrong

Something isn't working. What I see: $ARGUMENTS
Match it to these known problems and give me the fix one step at a time, checking with me after each:
(If nothing was typed after the command, ask me for it in one short question first.)

1. The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat.
2. It says the open data hub has no published release yet: the hub's first data release isn't out. Nothing is wrong on my side; try again later, or ask a WHO question, which is read live.
3. It says a place or year isn't covered: run describe_dataset to see what is covered.
4. It refused a query: the message says why; ask the same question in plain words instead.
5. The app asks for a password or my login: stop; this tool never needs one.
6. If none match, ask me one question at a time until we find it. Never ask for patient information.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
