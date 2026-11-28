---
name: choose-format
description: "Asks a few short questions about your project and returns which output fits now (conference abstract or poster, research letter or brief report, full original manuscript, or viewpoint or perspective), how settled that is, and what would change it."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Which format should I aim for?

Help me decide which output to aim for with this project: [project, from my message]
Don't guess my answers from that sentence: ask me each question below, one at a time, and wait for my answer.
(If my message doesn't say it, ask me for it in one short question first.)

1. Ask what kind of work it is, and offer exactly these answers: empirical (new numbers from data), methods or standard (a method or standard others can reuse), argument (a position, not new data) or case (one case or a small series).
2. Ask how far along the data is: idea, preliminary counts, frozen dataset or adjudicated (frozen, with clinician adjudication done).
3. Ask how many worked examples the piece needs to show in full, and whether a reader needs a methods section to trust the numbers (yes or no).
4. Ask whether I have a target conference and, if so, how many weeks remain before its deadline. This is optional.
5. Ask which matters most to me, in order, from: speed, citable record, reach, effort. This is optional too; without it every criterion counts the same, and reach is not scored yet.
6. Run choose_output_format with my answers. Show the chosen format, each format's probability of ranking first, the confidence and the review advice. Say that confidence means how settled the choice is, not whether it is right, and that this is a decision under my stated priorities, not a prediction that the work will be published. If it says too close to call, show what would change it first.
7. List the criteria it couldn't use and why, and the rules still awaiting the maintainer's review. If I named a conference, run get_publication_outlook for it and show how often its abstracts became full papers.
8. Never use this to rank ideas, fellows or projects. Suggest I take the result to my mentor, especially if the advice says to discuss it.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
