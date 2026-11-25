---
name: help-me-fix-it
description: "Asks what you see, matches it to known problems, and gives the fix one step at a time."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Something's wrong

Something isn't working. What I see: [what i see, from my message]
Match it to these known problems and give me the fix one step at a time, checking with me after each:
(If my message doesn't say it, ask me for it in one short question first.)

1. The tool isn't in my chat: tools added during a chat only appear in a new chat; start a new chat.
2. It says the open data hub could not be reached: the hub didn't answer just then. Check my internet connection and try again in a few minutes, or ask a WHO question, which is read live from WHO.
3. It says the open data hub has no published release yet: the hub this tool is set to read has never published any data, so trying again in a few minutes won't help. Nothing was guessed. Ask a WHO question meanwhile, and tell whoever installed it or report it on GitHub.
4. It says a place or year isn't covered: run describe_dataset to see what is covered.
5. It refused a query: the message says why; ask the same question in plain words instead.
6. It says 'Page 1 of N': nothing is wrong, the answer is long. Ask for every page with the Next line it gives, and save all the rows to one CSV file or artifact. If a later page says the data changed, start again from page 1.
7. The app asks for a password or my login: stop; this tool never needs one.
8. If none match, ask me one question at a time until we find it. Never ask for patient information.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
