---
name: research-story
description: "The entry point for a research project: asks whether you already have an area or want one found, then routes you through scoping or finding opportunities, refining a hypothesis and building the story."
argument-hint: "[your area, or leave blank]"
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Build my research story

I want to build a research story. My area, if I have one: $ARGUMENTS
(If nothing was typed after the command, ask me for it in one short question first.)

1. Ask me one question: do I already have an area, or do I want help finding one? Don't go on until I answer.
2. If I have an area, follow the scope-an-area workflow with it (path A).
3. If I want one found, follow the find-opportunities workflow (path B).
4. Then take the idea I choose through the refine-hypothesis workflow until it's test-ready or dropped.
5. Then follow the build-story workflow for the piece I need.
6. Before each change of step, tell me in one line where we are and what comes next.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; when an answer comes in pages, fetch every page and save all the rows to one CSV file or artifact rather than pasting them into the chat; and never ask me for, or accept, patient information.
